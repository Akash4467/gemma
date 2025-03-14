# Copyright 2024 DeepMind Technologies Limited.
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

"""Sampler for Gemma transformer.

An example of a sampling class for a Gemma model.
"""

from collections.abc import Sequence
import dataclasses

import flax
from gemma import modules
from gemma import params as params_lib
from gemma import transformer as transformer_lib
from gemma.gm.nn import _transformer
from gemma.gm.text import _sampling
from gemma.gm.text import _tokenizer
import jax
import jax.numpy as jnp
from kauldron.typing import PRNGKey


@flax.struct.dataclass(kw_only=True)
class _SamplingState:
  """Internal sampling state."""

  # Decoding step.
  decoding_step: jnp.int32

  # Number of tokens in the prompt.
  num_input_tokens: jnp.ndarray  # [B]

  # Fixed-size buffer for accumulating the output tokens.
  token_buffer: jnp.ndarray  # [B, L]

  # Position indices, based on ignoring pad tokens.
  positions: jnp.ndarray  # [B, L]

  # Model state for conditioning the model on autoregressively.
  cache: dict[str, modules.LayerCache]

  # Is decoding done on the given sequence?
  done: jnp.ndarray  # [B]

  # Total sampling steps (including the prompt).
  total_sampling_steps: int

  # Fixed-size buffer for accumulating the output logits.
  logits_buffer: jnp.ndarray | None = None  # [B, L, V]

  # List of tokens that are forbidden to be generated.
  forbidden_token_ids: Sequence[int] | None = None

  rng: PRNGKey


@dataclasses.dataclass
class SamplerOutput:

  # Decoded samples from the model.
  text: list[str]

  # Per-step logits used during sampling.
  logits: list[list[float]]

  # Tokens corresponding to the generated samples.
  tokens: list[list[int]]


class Sampler:
  """Sampler for gemma transformer."""

  # TODO(epot): Add sharding.
  def __init__(
      self,
      *,
      transformer: _transformer.Transformer,
      tokenizer: _tokenizer.Tokenizer,
      params: params_lib.Params,
      cache_length: int = 1024,
      seed: int,
  ):
    """Initializes a sampler for a Gemma model.

    Args:
      transformer: an instance of the Gemma transformer.
      tokenizer: tokenizer of the given model.
      params: weights of the model.
      cache_length: Max length of the cache.
      seed: Random seed for the sampler.
    """
    self.transformer = transformer
    self.tokenizer = tokenizer
    self.params = params
    self.cache_length = cache_length
    self._compiled_sample_fn = jax.jit(
        self._sample_fn, static_argnames=('sampling',)
    )
    self._rng = jax.random.PRNGKey(seed)

  @property
  def dtype(self) -> jnp.dtype:
    return jax.tree_util.tree_leaves(self.params)[0].dtype

  def _sample_step(
      self,
      params,
      sampler_state: _SamplingState,
      *,
      sampling: _sampling.SamplingMethod,
  ) -> _SamplingState:
    """Performs a single sampling step."""
    batch_size = sampler_state.token_buffer.shape[0]
    decoding_step = jnp.asarray(sampler_state.decoding_step, dtype=jnp.int32)
    last_token = sampler_state.token_buffer[:, decoding_step]
    input_mask = sampler_state.token_buffer != self.tokenizer.special_tokens.PAD
    attention_mask = _compute_attention_masks(
        decoding_step, self.cache_length, input_mask
    )
    step_positions = jnp.expand_dims(
        sampler_state.positions[:, decoding_step], -1
    )
    last_token = last_token.reshape((batch_size, 1))

    out = self.transformer.apply(
        {'params': params},
        last_token,
        positions=step_positions,
        cache=sampler_state.cache,
        attention_mask=attention_mask,
        # TODO(epot): return_last_only=True
    )
    logits = out.logits
    if sampler_state.forbidden_token_ids:
      logits = logits.at[:, :, sampler_state.forbidden_token_ids].set(-jnp.inf)

    # Logit is `B L V` with `L=1`
    next_rng, curr_rng = jax.random.split(sampler_state.rng)
    next_token_candidate = sampling.get_next_tokens(logits, rng=curr_rng)
    next_token_candidate = next_token_candidate[:, 0]

    next_token_candidate = jnp.where(
        decoding_step < sampler_state.num_input_tokens - 1,
        sampler_state.token_buffer[:, decoding_step + 1],
        next_token_candidate,
    )

    token_buffer = sampler_state.token_buffer.at[:, decoding_step + 1].set(
        next_token_candidate
    )

    if sampler_state.logits_buffer is not None:
      next_logits = jnp.squeeze(logits, 1)
      logits_buffer = sampler_state.logits_buffer.at[:, decoding_step + 1].set(
          next_logits
      )
    else:
      logits_buffer = sampler_state.logits_buffer

    # TODO(epot): Use `jnp.isin(xx, end_tokens)` to supports multiple EOS
    # tokens.
    done = sampler_state.done | jnp.equal(
        token_buffer[:, decoding_step + 1], self.tokenizer.special_tokens.EOS
    )

    return _SamplingState(
        decoding_step=sampler_state.decoding_step + 1,
        num_input_tokens=sampler_state.num_input_tokens,
        token_buffer=token_buffer,
        positions=sampler_state.positions,
        logits_buffer=logits_buffer,
        cache=out.cache,
        done=done,
        total_sampling_steps=sampler_state.total_sampling_steps,
        forbidden_token_ids=sampler_state.forbidden_token_ids,
        rng=next_rng,
    )

  def init_cache(self, bsz) -> dict[str, modules.LayerCache]:
    """Initializes the attention cache for each layer."""
    return self.transformer.init_cache(
        batch_size=bsz,
        dtype=self.dtype,
        cache_length=self.cache_length,
    )

  def init_sample_state(
      self,
      all_input_ids: list[jax.Array],
      total_sampling_steps: int,
      include_logits: bool = False,
      forbidden_token_ids: Sequence[int] | None = None,
  ) -> _SamplingState:
    """Initializes the sampling state given input prompts."""
    bsz = len(all_input_ids)
    num_input_tokens = [len(input_ids) for input_ids in all_input_ids]
    buffer_size = total_sampling_steps + 1

    token_buffer = jnp.full(
        (
            bsz,
            buffer_size,
        ),
        self.tokenizer.special_tokens.PAD,
        dtype=jnp.int32,
    )
    input_mask = jnp.ones_like(token_buffer, dtype=jnp.bool_)
    for i, (input_ids, num_tokens) in enumerate(
        zip(all_input_ids, num_input_tokens)
    ):
      token_buffer = token_buffer.at[i, :num_tokens].set(input_ids)
      input_mask = input_mask.at[i, :num_tokens].set(
          input_ids != self.tokenizer.special_tokens.PAD
      )
    positions = transformer_lib.build_positions_from_mask(input_mask)

    done = jnp.zeros((bsz,), dtype=jnp.bool_)

    if include_logits:
      logits_buffer = jnp.zeros(
          (bsz, buffer_size, self.transformer.config.num_embed),
          dtype=jnp.float32,
      )
    else:
      logits_buffer = None

    return _SamplingState(
        decoding_step=0,
        num_input_tokens=jnp.array(num_input_tokens, dtype=jnp.int32),
        token_buffer=token_buffer,
        positions=positions,
        logits_buffer=logits_buffer,
        cache=self.init_cache(bsz),
        done=done,
        total_sampling_steps=total_sampling_steps,
        forbidden_token_ids=forbidden_token_ids,
        rng=self._rng,
    )

  def tokenize(self, input_string: str) -> jax.Array:
    """Tokenizes the input string."""
    input_ids = self.tokenizer.encode(input_string)
    input_ids = jnp.array(
        [self.tokenizer.special_tokens.BOS] + jnp.array(input_ids).tolist(),
        dtype=jnp.int32,
    )
    return input_ids

  def mask_tokens_after_eos_ids(self, token_buffer):
    """Mask token IDs after the EOS token with the padding ID."""
    eos_id = self.tokenizer.special_tokens.EOS
    eos_exists = jnp.any(jnp.equal(token_buffer, eos_id), axis=-1)
    eos_indices = jnp.where(
        eos_exists,
        jnp.argmax(jnp.equal(token_buffer, eos_id), axis=-1),
        token_buffer.shape[-1],
    )
    mask = jnp.less_equal(
        jnp.arange(token_buffer.shape[-1]), eos_indices[:, None]
    )
    masked_token_buffer = (
        token_buffer * mask + self.tokenizer.special_tokens.PAD * (1 - mask)
    )

    return masked_token_buffer

  def _sample_fn(
      self,
      params: params_lib.Params,
      initial_sampling_state: _SamplingState,
      *,
      sampling: _sampling.SamplingMethod,
  ) -> _SamplingState:
    """Internal sampling function (to be jitted)."""

    def sample_with_params(sampler_state: _SamplingState):
      return self._sample_step(params, sampler_state, sampling=sampling)

    def cond_fn(sampler_state: _SamplingState):
      return (
          sampler_state.decoding_step < sampler_state.total_sampling_steps
      ) & jnp.any(jnp.logical_not(sampler_state.done))

    return jax.lax.while_loop(
        cond_fn, sample_with_params, initial_sampling_state
    )

  def __call__(
      self,
      input_strings: Sequence[str],
      *,
      total_generation_steps: int,
      echo: bool = False,
      return_logits: bool = False,
      forbidden_tokens: Sequence[str] | None = None,
      sampling: _sampling.SamplingMethod,
  ) -> SamplerOutput:
    """Samples a completion of the input string.

    Args:
      input_strings: input prompts to feed to the model for sampling.
      total_generation_steps: number of generation steps. will correspond to the
        longest prompt in the batch.
      echo: whether to return the prompt as part of the output sample.
      return_logits: whether to return per-step logits used during generation.
      forbidden_tokens: list of tokens that are forbidden to be generated. Each
        token must map to a single token id in the vocab.
      sampling: Sampling method to use.

    Returns:
      sampler_output: A SamplerOutput object containing the generated samples.
    """
    forbidden_token_ids = None
    if forbidden_tokens is not None:
      forbidden_token_ids = []
      for token in forbidden_tokens:
        token_id = self.tokenizer.encode(token)
        if len(token_id) != 1:
          raise ValueError(
              'Forbidden tokens must map to single token ids in the vocab.'
          )
        forbidden_token_ids.extend(token_id)
      forbidden_token_ids = tuple(forbidden_token_ids)
    all_input_ids = [self.tokenize(x) for x in input_strings]
    max_input_length = max(len(input_ids) for input_ids in all_input_ids)
    total_sampling_steps = max_input_length + total_generation_steps
    initial_sampling_state = self.init_sample_state(
        all_input_ids,
        include_logits=return_logits,
        total_sampling_steps=total_sampling_steps,
        forbidden_token_ids=forbidden_token_ids,
    )

    sampling_state = self._compiled_sample_fn(
        self.params, initial_sampling_state, sampling=sampling
    )

    masked_token_buffer = self.mask_tokens_after_eos_ids(
        sampling_state.token_buffer
    )

    out_tokens = []
    out_logits = []
    for i, (token_buffer, num_tokens) in enumerate(
        zip(
            masked_token_buffer,
            sampling_state.num_input_tokens,
        )
    ):
      start_idx = 0 if echo else num_tokens
      out_tokens.append(token_buffer[start_idx:total_sampling_steps].tolist())
      if return_logits:
        logits_buffer = sampling_state.logits_buffer[i]
        out_logits.append(
            logits_buffer[start_idx:total_sampling_steps].tolist()
        )

    decoded_outputs = [self.tokenizer.decode(tokens) for tokens in out_tokens]

    # Update the rng for the next call.
    self._rng = sampling_state.rng

    result = SamplerOutput(
        text=decoded_outputs,
        logits=out_logits,
        tokens=out_tokens,
    )
    return result


def _compute_attention_masks(
    time_step: jax.Array, seq_len: int, input_mask: jax.Array
) -> jax.Array:
  """Computes causal attention mask."""
  bsz = input_mask.shape[0]
  batch_time_step = jnp.full((bsz, 1), time_step, dtype=jnp.uint32)
  causal_mask = jnp.less_equal(
      jnp.expand_dims(jnp.arange(seq_len), 0), batch_time_step
  )
  max_seq_len = min(input_mask.shape[-1], seq_len)
  input_mask = jax.lax.dynamic_slice(
      input_mask,
      (0, jnp.maximum(time_step - seq_len + 1, 0)),
      (bsz, max_seq_len),
  )
  input_mask = (
      jnp.ones((bsz, seq_len), dtype=jnp.bool_)
      .at[:, :max_seq_len]
      .set(input_mask)
  )

  causal_mask = jnp.logical_and(causal_mask, input_mask)
  attention_mask = causal_mask[:, jnp.newaxis, :].astype(jnp.bool_)

  return attention_mask
