# coding=utf-8
# Copyright 2023-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers.utils.import_utils import is_torch_fx_proxy

from peft.utils.config import PeftConfig, PeftType
from peft.utils.other import _freeze_adapter, _get_submodules


def llama_rotate_half(x: torch.Tensor) -> torch.Tensor:
    """
    Rotate half the hidden dims of the input.

    This function was duplicated verbatim from:
    https://github.com/huggingface/transformers/blob/1de8ce9ee1191ba761a593ac15d9ccbf5851bfc5/src/transformers/models/llama/modeling_llama.py#L126

    This was done to eliminate the Llama transformers implementation as a dependency of this file. Note that some other
    functions were also adapted from the transformers implementation but were modified.
    """
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2 :]
    return torch.cat((-x2, x1), dim=-1)


def llama_apply_rotary_pos_emb(q, cos, sin, position_ids):
    """
    Apply rotary position embedding to query states in the Llama model.

    This function was adapted from:
    https://github.com/huggingface/transformers/blob/1de8ce9ee1191ba761a593ac15d9ccbf5851bfc5/src/transformers/models/llama/modeling_llama.py#L133

    It was modified to remove unnecessary processing of key states.
    """
    gather_indices = position_ids[:, None, :, None]  # [bs, 1, seq_len, 1]
    gather_indices = gather_indices.repeat(1, cos.shape[1], 1, cos.shape[3])
    cos = torch.gather(cos.repeat(gather_indices.shape[0], 1, 1, 1), 2, gather_indices)
    sin = torch.gather(sin.repeat(gather_indices.shape[0], 1, 1, 1), 2, gather_indices)
    q_embed = (q * cos) + (llama_rotate_half(q) * sin)
    return q_embed


def llama_compute_query_states(model: nn.Module, **kwargs) -> torch.Tensor:
    """
    Compute query states for Llama models specifically.

    They need to be recomputed as the forward() method of the original LlamaModel in the transformers library does not
    return them. See the related discussion in the PR: https://github.com/huggingface/peft/pull/268
    """
    hidden_states = kwargs.get("hidden_states")
    position_ids = kwargs.get("position_ids")
    past_key_value = kwargs.get("past_key_value")
    bsz, q_len, _ = hidden_states.size()
    query_states = model.q_proj(hidden_states).view(bsz, q_len, model.num_heads, model.head_dim).transpose(1, 2)
    value_states = model.v_proj(hidden_states).view(bsz, q_len, model.num_heads, model.head_dim).transpose(1, 2)

    seq_len = q_len
    if past_key_value is not None:
        seq_len += past_key_value[0].shape[-2]
    cos, sin = model.rotary_emb(value_states, seq_len=seq_len)

    return llama_apply_rotary_pos_emb(query_states, cos, sin, position_ids)


def gpt_neox_rotate_half(x: torch.Tensor):
    return llama_rotate_half(x)


def gpt_neox_apply_rotary_pos_emb(q, cos, sin, position_ids):
    return llama_apply_rotary_pos_emb(q, cos, sin, position_ids)


def gpt_neox_compute_query_states(model: nn.Module, **kwargs):
    hidden_states = kwargs.get("hidden_states")
    position_ids = kwargs.get("position_ids")
    past_key_value = kwargs.get("layer_past")
    bsz, q_len, _ = hidden_states.size()

    qkv = model.query_key_value(hidden_states)
    query_states, _, value_states = qkv.split(qkv.shape[2] // 3, dim=2)
    query_states = query_states.view(bsz, q_len, model.num_attention_heads, model.head_size).transpose(1, 2)
    value_states = value_states.view(bsz, q_len, model.num_attention_heads, model.head_size).transpose(1, 2)

    query_rot = query_states[..., : model.rotary_ndims]
    query_pass = query_states[..., model.rotary_ndims:]

    seq_len = q_len
    if past_key_value is not None:
        seq_len += past_key_value[0].shape[-2]
    cos, sin = model.rotary_emb(value_states, seq_len=seq_len)

    query = gpt_neox_apply_rotary_pos_emb(query_rot, cos, sin, position_ids)
    query = torch.cat((query, query_pass), dim=-1)

    return query


def gptj_create_sinusoidal_positions(num_pos: int, dim: int) -> torch.Tensor:
    inv_freq = 1.0 / (10000 ** (torch.arange(0, dim, 2) / dim))
    sinusoid_inp = torch.einsum("i , j -> i j", torch.arange(num_pos, dtype=torch.float), inv_freq).float()
    return torch.cat((torch.sin(sinusoid_inp), torch.cos(sinusoid_inp)), dim=1)


@torch.fx.wrap
def gptj_get_embed_positions(embed_positions, position_ids):
    return embed_positions.to(position_ids.device).repeat(position_ids.shape[0], 1, 1)


def gptj_rotate_every_two(x: torch.Tensor) -> torch.Tensor:
    x1 = x[:, :, :, ::2]
    x2 = x[:, :, :, 1::2]
    x = torch.stack((-x2, x1), dim=-1)
    return x.flatten(-2)  # in einsum notation: rearrange(x, '... d j -> ... (d j)')


def gptj_apply_rotary_pos_emb(tensor: torch.Tensor, sin: torch.Tensor, cos: torch.Tensor) -> torch.Tensor:
    sin = torch.repeat_interleave(sin[:, :, None, :], 2, 3)
    cos = torch.repeat_interleave(cos[:, :, None, :], 2, 3)
    return (tensor * cos) + (gptj_rotate_every_two(tensor) * sin)


def gptj_compute_query_states(model: nn.Module, **kwargs):
    hidden_states = kwargs.get("hidden_states")
    position_ids = kwargs.get("position_ids")
    bsz, q_len, _ = hidden_states.size()

    query = model.q_proj(hidden_states)
    query = model._split_heads(query, model.num_attention_heads, model.head_dim, True)

    if is_torch_fx_proxy(position_ids):
        # The logic to conditionally copy to GPU could not be traced, so we do this
        # every time in the torch.fx case
        embed_positions = gptj_get_embed_positions(model.embed_positions, position_ids)
    else:
        embed_positions = model._get_embed_positions(position_ids)

    repeated_position_ids = position_ids.unsqueeze(-1).repeat(1, 1, embed_positions.shape[-1])
    sincos = torch.gather(embed_positions, 1, repeated_position_ids)
    sin, cos = torch.split(sincos, sincos.shape[-1] // 2, dim=-1)

    if model.rotary_dim is not None:
        q_rot = query[:, :, :, : model.rotary_dim]
        q_pass = query[:, :, :, model.rotary_dim:]

        q_rot = gptj_apply_rotary_pos_emb(q_rot, sin, cos)

        query = torch.cat([q_rot, q_pass], dim=-1)
    else:
        query = gptj_apply_rotary_pos_emb(query, sin, cos)

    query = query.permute(0, 2, 1, 3)

    return query


def moss_create_sinusoidal_positions(num_pos: int, dim: int) -> torch.Tensor:
    return gptj_create_sinusoidal_positions(num_pos, dim)


def moss_rotate_every_two(x: torch.Tensor) -> torch.Tensor:
    return gptj_rotate_every_two(x)


def moss_apply_rotary_pos_emb(tensor: torch.Tensor, sin: torch.Tensor, cos: torch.Tensor) -> torch.Tensor:
    return gptj_apply_rotary_pos_emb(tensor, sin, cos)


def moss_compute_query_states(model: nn.Module, **kwargs):
    hidden_states = kwargs.get("hidden_states")
    position_ids = kwargs.get("position_ids")
    bsz, q_len, _ = hidden_states.size()

    qkv = model.qkv_proj(hidden_states)
    mp_num = 4
    qkv_split = qkv.reshape(qkv.shape[:-1] + (mp_num, -1))

    local_dim = model.head_dim * model.num_attention_heads // mp_num
    query, value, key = torch.split(qkv_split, local_dim, dim=-1)
    query = model._split_heads(query, model.num_attention_heads, model.head_dim, mp_num=mp_num)

    embed_positions = model.embed_positions
    if embed_positions.device != position_ids.device:
        embed_positions = embed_positions.to(position_ids.device)
        model.embed_positions = embed_positions

    sincos = embed_positions[position_ids]
    sin, cos = torch.split(sincos, sincos.shape[-1] // 2, dim=-1)

    if model.rotary_dim is not None:
        q_rot = query[:, :, :, : model.rotary_dim]
        q_pass = query[:, :, :, model.rotary_dim:]

        q_rot = moss_apply_rotary_pos_emb(q_rot, sin, cos)

        query = torch.cat([q_rot, q_pass], dim=-1)
    else:
        query = moss_apply_rotary_pos_emb(query, sin, cos)

    query = query.permute(0, 2, 1, 3)

    return query


# Contains the config that is specific to a transformers model type.
ModelTypeConfig = namedtuple(
    "ModelTypeConfig",
    [
        "compute_query_states",
        "attention_module",
        "mlp_module",
        "k_proj_layer",
        "v_proj_layer",
        "o_proj_layer"
    ]
)
# Mapping of transformers model types to their specific configuration.
TRANSFORMERS_MODEL_CONFIG = {
    "llama": ModelTypeConfig(
        compute_query_states=llama_compute_query_states,
        attention_module="self_attn",
        mlp_module="mlp",
        k_proj_layer="k_proj",
        v_proj_layer="v_proj",
        o_proj_layer="o_proj"
    ),
    "gpt_neox": ModelTypeConfig(
        compute_query_states=gpt_neox_compute_query_states,
        attention_module="attention",
        mlp_module="mlp",
        k_proj_layer="query_key_value",
        v_proj_layer="query_key_value",
        o_proj_layer="dense"
    ),
    "gptj": ModelTypeConfig(
        compute_query_states=gptj_compute_query_states,
        attention_module="attn",
        mlp_module="mlp",
        k_proj_layer="k_proj",
        v_proj_layer="v_proj",
        o_proj_layer="out_proj"
    ),
    "moss": ModelTypeConfig(
        compute_query_states=moss_compute_query_states,
        attention_module="attn",
        mlp_module="mlp",
        k_proj_layer="qkv_proj",
        v_proj_layer="qkv_proj",
        o_proj_layer="out_proj"
    )
}


def handle_origin_attention_module_outputs(model_type: str, outputs: tuple):
    if model_type == "llama":
        output, _, past_key_value = outputs
    elif model_type in ["gpt_neox", "gptj", "moss"]:
        output, past_key_value = outputs[0], outputs[1]
    else:
        raise ValueError(f"Unsupported model type: '{model_type}'.")

    return output, past_key_value


def organize_adapted_attention_model_outputs(model_type: str, attn_output, attn_weights, past_key_value):
    if model_type == "llama":
        return attn_output, attn_weights, past_key_value
    elif model_type in ["gpt_neox", "gptj", "moss"]:
        return attn_output, past_key_value, attn_weights
    else:
        raise ValueError(f"Unsupported model type: '{model_type}'.")


@dataclass
class AdaptionPromptV2Config(PeftConfig):
    """Stores the configuration of an [`AdaptionPromptModel`]."""

    attention_module: str = field(
        default=None, metadata={"help": "Name of the attention submodules to be adapted."}
    )
    mlp_module: str = field(
        default=None, metadata={"help": "Name of the mlp submodules to be adapted."}
    )
    adapter_len: int = field(default=None, metadata={"help": "Number of adapter tokens to insert"})
    adapter_layers: int = field(default=None, metadata={"help": "Number of adapter layers (from the top)"})
    add_bias: bool = field(default=True, metadata={"help": "Whether to add bias"})
    add_scale: bool = field(default=True, metadata={"help": "Whether to add scale"})
    multi_modal: bool = field(default=False, metadata={"help": "Whether used for multi modal training or inference"})
    supported_modals: Optional[List[str]] = field(default=None, metadata={"help": "Names of modals that are supported"})

    def __post_init__(self):
        self.peft_type = PeftType.ADAPTION_PROMPT_V2
        if self.multi_modal and not self.supported_modals:
            raise ValueError("name of supported modal has to be specified when is multi_modal mode.")
        if self.supported_modals:
            num_modals = len(self.supported_modals)
            if num_modals != len(set(self.supported_modals)):
                raise ValueError("duplicate modal name found in supported_modals, please make sure each name is unique.")
            if num_modals != [modal for modal in self.supported_modals if modal]:
                raise ValueError("modal name can't be empty string.")


def prepare_config(
    peft_config: AdaptionPromptV2Config,
    model,
) -> AdaptionPromptV2Config:
    """Prepare the config based on the llama model type."""
    if model.config.model_type not in TRANSFORMERS_MODEL_CONFIG:
        raise ValueError(f"Unsupported model type for adaption prompt: '{model.config.model_type}'.")

    model_config = TRANSFORMERS_MODEL_CONFIG[model.config.model_type]

    if peft_config.attention_module is None:
        peft_config.attention_module = model_config.attention_module
    if peft_config.mlp_module is None:
        peft_config.mlp_module = model_config.mlp_module

    return peft_config


class AdaptionPromptV2Model(nn.Module):
    """
    Implements adaption prompts v2 as described in https://arxiv.org/pdf/2304.15010.pdf.

    The top L attention modules are replaced with Adapted* modules that wrap the original ones.

    Notes on the multi-adapter pattern:
    - We store the states of different adapters by keeping a dictionary of Adapted* modules indexed by adapter
      name.
    - Every time we switch adapters, we remove the modules of the currently active adapter from the model, store them
      in the dictionary, and replace them with the modules of the new adapter.
    - To avoid duplicated and potentially inconsistent state, the currently active adapter is always removed from the
      dictionary.
    - Disabling the adapter would also result in the modules being removed from the model.
    """

    def __init__(self, model, configs: Dict, adapter_name: str):
        super().__init__()
        self.model = model
        # Store adapter configs by name.
        self._configs: Dict[str, AdaptionPromptV2Config] = {}
        # Store lists of the parents of the affected modules by adapter name.
        # We keep references to the parents so we can swap the adapters in-and-out of the model.
        self._parents: Dict[str, List[nn.Module]] = {}
        # Store lists of cached Adapted* modules by name.
        self._cached_adapters: Dict[str, List] = {}
        # The name of the currently active adapter.
        self._active_adapter = None
        # Whether the adapter is enabled.
        self._enabled = True
        self.forward = self.model.forward
        self.add_adapter(adapter_name, configs[adapter_name])
        self._mark_only_adaption_prompts_as_trainable()

    def add_adapter(self, adapter_name: str, config: AdaptionPromptV2Config) -> None:
        """Add an adapter with the given name and config."""
        config = prepare_config(config, self.model)
        if adapter_name in self._configs:
            raise ValueError(f"Adapter with name '{adapter_name}' already exists.")

        parents = []
        for name, _ in self.model.named_modules():
            if name.endswith(config.attention_module):
                par, _, _ = _get_submodules(self.model, name)
                parents.append(par)
        if len(parents) < config.adapter_layers:
            raise ValueError(
                f"Config specifies more adapter layers '{config.adapter_layers}'"
                f" than the model has '{len(parents)}'."
            )
        # Note that if the target modules are not in Sequential, ModuleList, or
        # some other PyTorch ordered container, the behavior is undefined as we
        # assume here that the order of the modules is the same as the order of
        # the transformer decoder layers.
        first_parent = parents[0]
        parents = parents[-config.adapter_layers :]
        if config.multi_modal and first_parent not in parents:
            parents = [first_parent] + parents
        self._parents[adapter_name] = parents

        # It is only None during initialization.
        # If it is disabled, we don't have to remove the modules.
        if self._active_adapter is not None and self._enabled:
            self._remove_adapted_modules(self._active_adapter)
        self._active_adapter = adapter_name
        self._configs[adapter_name] = config
        self._create_adapted_modules(config, parents)
        if not self._enabled:
            self._remove_adapted_modules(self._active_adapter)

        if config.inference_mode:
            _freeze_adapter(self.model, adapter_name)

    def set_adapter(self, adapter_name: str) -> None:
        """Set the model to use the adapter with the given name."""
        if self._active_adapter == adapter_name:
            return
        if adapter_name not in self._configs:
            raise ValueError(f"Adapter with name '{adapter_name}' does not exist.")

        if self._enabled:
            self._remove_adapted_modules(self._active_adapter)
            self._set_adapted_modules(adapter_name)

        self._active_adapter = adapter_name

    def freeze_adaption_params(self, modal: Optional[str] = None, mode: bool = True):
        """Freeze adaption params, this function is useful at multi-modal training"""
        if not self._enabled:
            return

        config = self._configs[self._active_adapter]

        if modal and not config.multi_modal:
            return
        if modal and modal not in config.supported_modals:
            return

        parents = []
        for name, _ in self.model.named_modules():
            if name.endswith(config.attention_module):
                par, _, _ = _get_submodules(self.model, name)
                parents.append(par)
        if modal:
            parents = [parents[0]]
        else:
            adapter_layers = config.adapter_layers
            if config.multi_modal and adapter_layers == len(parents):
                adapter_layers -= 1
            if adapter_layers <= 0:
                return
            parents = parents[-adapter_layers:]
        if not modal:
            modal = "text"

        for par in parents:
            for n, m in getattr(par, config.attention_module).named_modules():
                if n.split(".")[-1].startswith("adaption_"):
                    m[modal].requires_grad = not mode

    def unfreeze_adaption_params(self, modal: Optional[str] = None):
        """Unfreeze adaption params, this function is useful at multi-modal training"""
        self.freeze_adaption_params(modal, False)

    def enable_adapter_layers(self):
        """Enable adapter layers by swapping in cached Adapted* modules."""
        self._enabled = True
        self._set_adapted_modules(self._active_adapter)

    def disable_adapter_layers(self):
        """Disable adapter layers by swapping out Adapted* modules."""
        self._enabled = False
        self._remove_adapted_modules(self._active_adapter)

    def _create_adapted_modules(self, config: AdaptionPromptV2Config, parents: List[nn.Module]) -> None:
        """Wrap original modules with newly created Adapted* modules."""
        for i, par in enumerate(parents):
            if config.multi_modal and i == 0:
                attn = AdaptedAttention(
                    config=self.model.config,
                    model=getattr(par, config.attention_module),
                    adapter_len=config.adapter_len,
                    add_bias=False,
                    add_scale=False,
                    supported_modals=config.supported_modals
                )
                setattr(par, config.attention_module, attn)
            else:
                attn = AdaptedAttention(
                    config=self.model.config,
                    model=getattr(par, config.attention_module),
                    adapter_len=config.adapter_len,
                    add_bias=config.add_bias,
                    add_scale=config.add_scale,
                )
                mlp = AdaptedMLP(
                    config=self.model.config,
                    model=getattr(par, config.mlp_module),
                    add_bias=config.add_bias,
                    add_scale=config.add_scale
                )
                setattr(par, config.attention_module, attn)
                setattr(par, config.mlp_module, mlp)

    def _set_adapted_modules(self, adapter_name: str) -> None:
        """Replace original model's submodules with cached Adapted* modules."""
        cached = self._cached_adapters[adapter_name]
        del self._cached_adapters[adapter_name]
        config = self._configs[adapter_name]
        for i, par in enumerate(self._parents[adapter_name]):
            setattr(par, config.attention_module, cached[i][0])
            setattr(par, config.mlp_module, cached[i][1])

    def _remove_adapted_modules(self, adapter_name: str) -> None:
        """Remove Adapted* modules from the model and store them in the cache."""
        config = self._configs[adapter_name]
        adapted_module_groups = []
        for par in self._parents[adapter_name]:
            attn = getattr(par, config.attention_module)
            mlp = getattr(par, config.mlp_module)
            adapted_module_groups.append((attn, mlp))
            setattr(par, config.attention_module, attn.model)
            setattr(par, config.mlp_module, mlp.model)
        self._cached_adapters[adapter_name] = adapted_module_groups

    def _mark_only_adaption_prompts_as_trainable(self) -> None:
        """Freeze all parameters of the model except the adaption prompts."""
        for n, p in self.model.named_parameters():
            if "adaption_" not in n:
                p.requires_grad = False

    def __getattr__(self, name: str):
        """Forward missing attributes to the wrapped module."""
        try:
            return super().__getattr__(name)  # defer to nn.Module's logic
        except AttributeError:
            # This is necessary as e.g. causal models have various methods that we
            # don't want to re-implement here.
            return getattr(self.model, name)


class AdaptedLinear(nn.Module):
    """This module wraps nn.Linear module."""
    def __init__(self, model: nn.Linear, add_bias, add_scale):
        """
        Initialize object.

        Args:
            model: nn.Linear module that is being wrapped.
            add_bias: Whether to add bias.
            add_scale: Whether to add scale.
        """
        assert not isinstance(model, AdaptedLinear)
        super(AdaptedLinear, self).__init__()
        self.model = model
        in_feat = self.model.in_features
        out_feat = self.model.out_features
        device = next(model.parameters()).device

        if add_bias:
            self.adaption_bias = nn.Parameter(torch.zeros([out_feat], device=device))
        else:
            self.register_parameter("adaption_bias", None)

        if add_scale:
            self.adaption_scale = nn.Parameter(torch.ones([in_feat], device=device))
        else:
            self.register_parameter("adaption_scale", None)

    def forward(self, x):
        if self.adaption_scale is not None:
            x = x * self.adaption_scale
        x = self.model(x)
        if self.adaption_bias is not None:
            x = x + self.adaption_bias
        return x


class AdaptedAttention(nn.Module):
    """This module wraps the original model's attention module."""

    def __init__(
        self,
        config,
        model,
        adapter_len: int,
        add_bias: bool,
        add_scale: bool,
        supported_modals: Optional[List[str]] = None
    ):
        """
        Initialize object.

        Args:
            config: Base model's config.
            model: The original transformer attention module that is being wrapped.
            adapter_len: The length of the adaption prompt to insert.
            add_bias: Whether to add bias.
            add_scale: Whether to add scale.
        """
        assert not isinstance(model, AdaptedAttention)
        super().__init__()
        self.model = model
        self.model_type = config.model_type
        self.hidden_size = config.hidden_size
        self.num_head = config.num_attention_heads
        self.head_size = self.hidden_size // self.num_head
        self.adapter_len = adapter_len
        # Assume all parameters of the attention model we are wrapping are on the same device.
        device = next(model.parameters()).device
        if supported_modals:
            self.adaption_prompts = nn.ParameterDict(
                {
                    modal: nn.Parameter(
                        torch.empty(1, adapter_len, self.hidden_size, device=device).normal_()
                    ) for modal in supported_modals
                }
            )
            self.adaption_gates = nn.ParameterDict(
                {
                    modal: nn.Parameter(torch.zeros(1, device=device)) for modal in supported_modals
                }
            )
        else:
            self.adaption_prompts = nn.ParameterDict(
                {
                    "text": nn.Parameter(
                        torch.empty(1, adapter_len, self.hidden_size, device=device).normal_()
                    )
                }
            )
            self.adaption_gates = nn.ParameterDict({"text": nn.Parameter(torch.zeros(1, device=device))})
        # Initialize adapted linear
        linear_name_modules = []
        for name, module in self.model.named_children():
            if isinstance(module, nn.Linear):
                linear_name_modules.append((name, module))
        self.linears = nn.ModuleDict(
            {name: AdaptedLinear(module, add_bias=add_bias, add_scale=add_scale) for name, module in linear_name_modules}
        )

    def _set_adapted_linears(self):
        for name, linear in self.linears.items():
            setattr(self.model, name, linear)

    def _remove_adapted_linears(self):
        for name, linear in self.linears.items():
            setattr(self.model, name, linear.model)

    def forward(self, hidden_states=None, **kwargs):
        """
        Forward pass for the adapter which wraps the original model's attention module.

        Args:
            hidden_states: See the original model's attention module.
            kwargs: See the original model's attention module.
        """
        if kwargs.get("output_attention", False):
            raise NotImplementedError("output_attention is not currently supported.")

        self._set_adapted_linears()

        output, past_key_value = handle_origin_attention_module_outputs(self.model_type, self.model(hidden_states, **kwargs))
        # Recompute query states.
        compute_query_states = TRANSFORMERS_MODEL_CONFIG[self.model_type].compute_query_states
        # (bsz, num_heads, q_len, head_dim)
        if "hidden_states" not in kwargs:
            kwargs["hidden_states"] = hidden_states
        query_states = compute_query_states(model=self.model, **kwargs)

        bsz = output.shape[0]
        k_proj_layer = TRANSFORMERS_MODEL_CONFIG[self.model_type].k_proj_layer
        v_proj_layer = TRANSFORMERS_MODEL_CONFIG[self.model_type].v_proj_layer
        o_proj_layer = TRANSFORMERS_MODEL_CONFIG[self.model_type].o_proj_layer

        modal2adapter_kv = dict()
        for modal in self.adaption_prompts:
            adaption_prompt = self.adaption_prompts[modal]
            if k_proj_layer == v_proj_layer:
                _, key, value = getattr(self.model, k_proj_layer)(adaption_prompt).split(self.hidden_size, dim=2)
            else:
                key = getattr(self.model, k_proj_layer)(adaption_prompt)
                value = getattr(self.model, v_proj_layer)(adaption_prompt)
            # (bsz, num_heads, adapter_len, head_dim)
            adapter_k = (
                key.view(1, self.adapter_len, self.num_head, self.head_size)
                .repeat(bsz, 1, 1, 1)
                .transpose(1, 2)
            )
            # (bsz, num_heads, adapter_len, head_dim)
            adapter_v = (
                value.view(1, self.adapter_len, self.num_head, self.head_size)
                .repeat(bsz, 1, 1, 1)
                .transpose(1, 2)
            )
            modal2adapter_kv[modal] = (adapter_k, adapter_v)

        # Compute adapter output
        bsz, num_heads, q_len, head_dim = query_states.shape
        for modal, (adapter_k, adapter_v) in modal2adapter_kv.items():
            # (bsz, num_heads, q_len, adapter_len)
            scores = torch.matmul(query_states.type_as(adapter_k), adapter_k.transpose(2, 3)) / math.sqrt(head_dim)
            # Upcast attention to fp32
            # (bsz, num_heads, q_len, adapter_len)
            scores = self.adaption_gates[modal] * F.softmax(scores, dim=-1, dtype=torch.float32).to(query_states.dtype)
            # (bsz, q_len, num_heads * head_dim)
            adapter_output = torch.matmul(scores, adapter_v).transpose(1, 2).reshape(bsz, q_len, -1)
            # (bsz, q_len, hidden_size)
            if o_proj_layer is not None:
                adapter_output = getattr(self.model, o_proj_layer)(adapter_output)

            # Add adaption prompt output to original output.
            output = output + adapter_output

        self._remove_adapted_linears()
        return organize_adapted_attention_model_outputs(self.model_type, output, None, past_key_value)


class AdaptedMLP(nn.Module):
    """This module wraps the original model's mlp module"""
    def __init__(self, config, model, add_bias: bool, add_scale: bool):
        """
        Initialize object.

        Args:
            config: Base model's config.
            model: The original transformer attention module that is being wrapped.
            add_bias: Whether to add bias.
            add_scale: Whether to add scale.
        """
        assert not isinstance(model, AdaptedMLP)
        super().__init__()
        self.model = model
        self.model_type = config.model_type
        # Initialize adapted linear
        linear_name_modules = []
        for name, module in self.model.named_children():
            if isinstance(module, nn.Linear):
                linear_name_modules.append((name, module))
        self.linears = nn.ModuleDict(
            {name: AdaptedLinear(module, add_bias=add_bias, add_scale=add_scale) for name, module in linear_name_modules}
        )

    def _set_adapted_linears(self):
        for name, linear in self.linears.items():
            setattr(self.model, name, linear)

    def _remove_adapted_linears(self):
        for name, linear in self.linears.items():
            setattr(self.model, name, linear.model)

    def forward(self, *args, **kwargs):
        """
        Forward pass for the adapter which wraps the original model's mlp module.

        Args:
            args: See the original model's mlp module.
            kwargs: See the original model's mlp module.
        """
        self._set_adapted_linears()
        out = self.model(*args, **kwargs)
        self._remove_adapted_linears()
        return out
