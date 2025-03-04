# coding=utf-8
# Adapted from
# https://github.com/huggingface/transformers/blob/v4.33.2/src/transformers/models/llama/modeling_llama.py
# Copyright 2023 The vLLM team.
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
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
"""Rotary Positional Embeddings."""
import math
from typing import Tuple, Union

import torch
import torch.nn as nn

from vllm import pos_encoding_ops
from vllm.model_executor.input_metadata import InputMetadata


class RotaryEmbedding(nn.Module):
    """Original rotary positional embedding."""

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: int,
        is_neox_style: bool,
    ) -> None:
        super().__init__()
        self.head_size = head_size
        self.rotary_dim = rotary_dim
        self.max_position_embeddings = max_position_embeddings
        self.base = base
        self.is_neox_style = is_neox_style

        cache = self._compute_cos_sin_cache()
        cache = cache.to(torch.get_default_dtype())
        self.register_buffer("cos_sin_cache", cache, persistent=False)

    def _compute_inv_freq(self, base: Union[int, float]) -> torch.Tensor:
        """Compute the inverse frequency."""
        # NOTE(woosuk): The HF implementation uses `torch.arange(...).float()`.
        # However, we use `torch.arange(..., dtype=torch.float)` instead to
        # avoid numerical issues with large base values (e.g., 10000000).
        # This may cause a slight numerical difference between the HF
        # implementation and ours.
        # NOTE(woosuk): To exactly match the HF implementation, we need to
        # use CPU to compute the cache and then move it to GPU. However, we
        # create the cache on GPU for faster initialization. This may cause
        # a slight numerical difference between the HF implementation and ours.
        inv_freq = 1.0 / (base**(torch.arange(
            0, self.rotary_dim, 2, dtype=torch.float, device="cuda") /
                                 self.rotary_dim))
        return inv_freq

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        """Compute the cos and sin cache."""
        inv_freq = self._compute_inv_freq(self.base)
        t = torch.arange(self.max_position_embeddings,
                         dtype=torch.float,
                         device="cuda")

        freqs = torch.einsum("i,j -> ij", t, inv_freq)
        cos = freqs.cos()
        sin = freqs.sin()
        cache = torch.cat((cos, sin), dim=-1)
        return cache

    def forward(
        self,
        positions: torch.Tensor,
        query: torch.Tensor,
        key: torch.Tensor,
        input_metadata: InputMetadata,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # pos_encoding_ops.rotary_embedding() is an in-place operation that
        # updates the query and key tensors.
        pos_encoding_ops.rotary_embedding(positions, query, key,
                                          self.head_size, self.cos_sin_cache,
                                          self.is_neox_style)
        return query, key


class LinearScalingRotaryEmbedding(RotaryEmbedding):
    """RotaryEmbedding extended with linear scaling.

    Credits to the Reddit user /u/kaiokendev
    """

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: int,
        is_neox_style: bool,
        scaling_factor: float,
    ) -> None:
        self.scaling_factor = scaling_factor
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style)

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        inv_freq = self._compute_inv_freq(self.base)
        # NOTE(woosuk): self.max_position_embeddings is the original
        # maximum length before applying the rope scaling.
        # Thus, the maximum length after applying the rope scaling is
        # self.max_position_embeddings * self.scaling_factor.
        max_len = self.max_position_embeddings * self.scaling_factor
        t = torch.arange(max_len, dtype=torch.float, device="cuda")
        t = t / self.scaling_factor

        freqs = torch.einsum("i,j -> ij", t, inv_freq)
        cos = freqs.cos()
        sin = freqs.sin()
        cache = torch.cat((cos, sin), dim=-1)
        return cache


class DynamicNTKScalingRotaryEmbedding(RotaryEmbedding):
    """RotaryEmbedding extended with Dynamic NTK scaling.

    Credits to the Reddit users /u/bloc97 and /u/emozilla
    """

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: int,
        is_neox_style: bool,
        scaling_factor: float,
    ) -> None:
        self.scaling_factor = scaling_factor
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style)

    def _compute_cos_sin_cache(self) -> torch.Tensor:
        # NOTE(woosuk): self.max_position_embeddings is the original
        # maximum length before applying the rope scaling.
        # Thus, the maximum length after applying the rope scaling is
        # self.max_position_embeddings * self.scaling_factor.
        max_len = self.max_position_embeddings * self.scaling_factor
        base = self.base * (
            (self.scaling_factor * max_len / self.max_position_embeddings) -
            (self.scaling_factor - 1))**(self.rotary_dim /
                                         (self.rotary_dim - 2))
        inv_freq = self._compute_inv_freq(base)
        t = torch.arange(max_len, dtype=torch.float, device="cuda")

        freqs = torch.einsum("i,j -> ij", t, inv_freq)
        cos = freqs.cos()
        sin = freqs.sin()
        cache = torch.cat((cos, sin), dim=-1)
        return cache


class DynamicNTKScalingRotaryEmbeddingQwen(RotaryEmbedding):
    """RotaryEmbedding + Qwen's Dynamic-NTK

    Reference: https://huggingface.co/Qwen/Qwen-7B-Chat/blob/main/modeling_qwen.py
    """

    def __init__(
        self,
        head_size: int,
        rotary_dim: int,
        max_position_embeddings: int,
        base: int,
        is_neox_style: bool,
        seq_length: int
    ) -> None:
        self._config_seq_length = seq_length # 8192 for Qwen-7B
        
        self._seq_len_cached = 0
        self._ntk_alpha_cached = 1.0
        super().__init__(head_size, rotary_dim, max_position_embeddings, base,
                         is_neox_style)
    
    def _update_cos_sin_cache(self, max_seq_len):
        ntk_alpha = self.get_ntk_alpha(max_seq_len)

        if max_seq_len > self._seq_len_cached or ntk_alpha != self._ntk_alpha_cached:
            base = self.base * ntk_alpha**(self.rotary_dim /(self.rotary_dim - 2))
            inv_freq = self._compute_inv_freq(base)
            
            self._seq_len_cached = max(2 * max_seq_len, 16)
            self._ntk_alpha_cached = ntk_alpha
            
            t = torch.arange(self._seq_len_cached, dtype=torch.float, device="cuda")
            freqs = torch.einsum("i,j -> ij", t, inv_freq)
            cos = freqs.cos()
            sin = freqs.sin()
            cache = torch.cat((cos, sin), dim=-1)
            cache = cache.to(self.cos_sin_cache.dtype)
            self.cos_sin_cache = cache
        
    def get_ntk_alpha(self, true_seq_len):
        # this function is copied from `modeling_qwen.py`
        context_value = math.log(true_seq_len / self._config_seq_length, 2) + 1
        ntk_alpha = 2 ** math.ceil(context_value) - 1
        ntk_alpha = max(ntk_alpha, 1)
        return ntk_alpha

    ## new implementation of vllm+qwen's dynamic-ntk
    ## use a for loop to calculate ntk_alpha and the corresponding positioned `q` and `k` for each data in the batch
    def forward(
        self,
        positions: torch.Tensor,
        query: torch.Tensor,
        key: torch.Tensor,
        input_metadata: InputMetadata,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        # vllm forward has two stages: first forward and continous forward
        # in first forward，sequence length is stored in `input_metadata.prompt_lens (list)`
        # in continous forward, sequence length is stored in `input_metadata.context_lens (torch.tensor)`
        
        query_list, key_list = [], []
        
        # get true sequence length
        is_continue, seq_lens = False, []
        if len(input_metadata.prompt_lens) > 0:
            seq_lens = input_metadata.prompt_lens
            is_continue = False
        else:
            seq_lens = input_metadata.context_lens.tolist()
            is_continue = True

        batch_size = len(seq_lens)
        _prev = 0
        for i, seq_len in enumerate(seq_lens):
            _start = _prev
            
            if is_continue: # continous forward, input for each data has 1 token
                _end = _start + 1
            else: # first forward, input for each data has `seq_len` tokens
                _end = _start + seq_len
                
            if i != batch_size - 1:
                _query = query[_start:_end, :]
                _key = key[_start:_end, :]
                _positions = positions[_start:_end]
            else: # reach paddings
                _query = query[_start:, :]
                _key = key[_start:, :]
                _positions = positions[_start:]

            # update cos_sin_cache with `seq_len`
            self._update_cos_sin_cache(seq_len)
            pos_encoding_ops.rotary_embedding(_positions, _query, _key,
                                              self.head_size, self.cos_sin_cache,
                                              self.is_neox_style)
            query_list.append(_query)
            key_list.append(_key)
            
            _prev = _end
            
        query = torch.cat(query_list, dim=0)
        key = torch.cat(key_list, dim=0)
        
        return query, key
