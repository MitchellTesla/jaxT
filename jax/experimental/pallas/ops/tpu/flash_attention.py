# Copyright 2023 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Flash Attention TPU kernel."""
from __future__ import annotations
import dataclasses
import functools
from typing import Any

import jax
from jax import lax
import jax.numpy as jnp
from jax.experimental import pallas as pl
from jax.experimental.pallas import tpu as pltpu

DEFAULT_MASK_VALUE = -0.7 * float(jnp.finfo(jnp.dtype("float32")).max)


def when(condition):
  return lambda f: jax.lax.cond(condition, f, lambda: None)


@dataclasses.dataclass(frozen=True)
class BlockSizes:
  """Tile sizes parameterizing FlashAttention kernels.

  Those parameters have negligible effect on numerics, but affect performance
  greatly.
  """
  block_q: int
  block_k_major: int
  block_k: int
  block_b: int

  block_q_major_dkv: int | None = None
  block_k_major_dkv: int | None = None
  block_k_dkv: int | None = None
  block_q_dkv: int | None = None

  block_k_major_dq: int | None = None
  block_k_dq: int | None = None
  block_q_dq: int | None = None

  def __post_init__(self):
    def verify_major_minor(prefix, suffix, major, minor):
      if minor > major:
        raise ValueError(
            f"{prefix}{suffix}={minor} should be smaller than"
            f" {prefix}_major{suffix}={major}"
        )
      if major % minor != 0:
        raise ValueError(
            f"{prefix}{suffix}={minor} should divide"
            f" {prefix}_major{suffix}={major}"
        )

    verify_major_minor("block_k", "", self.block_k_major, self.block_k)
    if self.block_q_major_dkv is not None and self.block_q_dkv is not None:
      verify_major_minor(
          "block_q", "_dkv", self.block_q_major_dkv, self.block_q_dkv
      )
    if self.block_k_major_dkv is not None and self.block_k_dkv is not None:
      verify_major_minor(
          "block_k", "_dkv", self.block_k_major_dkv, self.block_k_dkv
      )
    if self.block_k_major_dq is not None and self.block_k_dq is not None:
      verify_major_minor(
          "block_k", "_dq", self.block_k_major_dq, self.block_k_dq
      )

  @property
  def has_backward_blocks(self) -> bool:
    backward_blocks = (
        self.block_q_major_dkv,
        self.block_k_major_dkv,
        self.block_q_dkv,
        self.block_k_dkv,
        self.block_k_major_dq,
        self.block_k_dq,
        self.block_q_dq,
    )
    return all(b is not None for b in backward_blocks)

  @classmethod
  def get_default(cls, batch_size, num_heads, q_seq_len, kv_len, d_model):
    # TODO(apaszke,sharadmv): Select better parameters based on a heuristic.
    del batch_size, num_heads, q_seq_len, kv_len, d_model  # Unused.
    return BlockSizes(
        block_q=128,
        block_k_major=128,
        block_k=128,
        block_b=1,
        block_q_major_dkv=128,
        block_k_major_dkv=128,
        block_k_dkv=128,
        block_q_dkv=128,
        block_k_major_dq=128,
        block_k_dq=128,
        block_q_dq=128,
    )


@functools.partial(
    jax.jit,
    static_argnames=[
        "causal",
        "sm_scale",
        "block_sizes",
        "debug",
    ],
)
def flash_attention(
    q,  # [batch_size, num_heads, q_seq_len, d_model]
    k,  # [batch_size, num_heads, kv_seq_len, d_model]
    v,  # [batch_size, num_heads, kv_seq_len, d_model]
    ab=None,  # [batch_size, num_heads, q_seq_len, kv_seq_len]
    *,
    causal: bool = False,
    sm_scale: float = 1.0,
    block_sizes: BlockSizes | None = None,
    debug: bool = False,
):
  batch_size, num_heads, q_seq_len, d_model = q.shape
  batch_size_k, num_heads_k, kv_seq_len, d_model_k = k.shape
  batch_size_v, num_heads_v, kv_seq_len_v, d_model_v = v.shape
  if batch_size != batch_size_k or batch_size != batch_size_v:
    raise ValueError(
        f"Batch size mismatch: got {batch_size}, {batch_size_k} and"
        f" {batch_size_v} (for q, k, v respectively)"
    )
  if num_heads != num_heads_k or num_heads != num_heads_v:
    raise ValueError(
        f"Head count mismatch: got {num_heads}, {num_heads_k},"
        f" {num_heads_v} (for q, k, v respectively)"
    )
  if d_model != d_model_k:
    raise ValueError(
        f"Model dimension mismatch: got {d_model} and {d_model_k} (for q and k"
        " respectively)"
    )
  if d_model != d_model_v:
    raise NotImplementedError(
        "V model dimension unequal to KV model dimension unsupported"
    )
  if kv_seq_len != kv_seq_len_v:
    raise ValueError(
        f"KV sequence length mismatch: got {kv_seq_len} and {kv_seq_len_v}"
    )
  if block_sizes is None:
    block_sizes = BlockSizes.get_default(
        batch_size, num_heads, q_seq_len, kv_seq_len, d_model
    )
  return _flash_attention(
      q, k, v, ab, False, causal, sm_scale, block_sizes, debug
  )


@functools.partial(jax.custom_vjp, nondiff_argnums=range(4, 9))
def _flash_attention(
    q, k, v, ab,
    save_residuals, causal, sm_scale, block_sizes, debug
):
  return _flash_attention_impl(
      q, k, v, ab, save_residuals, causal, sm_scale,
      block_sizes.block_b, block_sizes.block_q,
      block_sizes.block_k_major, block_sizes.block_k,
      debug,
  )


def _flash_attention_fwd(
    q, k, v, ab, save_residuals, causal, sm_scale, block_sizes, debug
):
  if save_residuals:
    raise NotImplementedError("Higher-order AD not supported")
  o, l, m = _flash_attention(
      q, k, v, ab, True, causal, sm_scale, block_sizes, debug)
  return o, (q, k, v, ab, o, l, m)


def _flash_attention_bwd(
    save_residuals: bool,
    causal: bool,
    sm_scale: float,
    block_sizes: BlockSizes,
    debug: bool,
    residuals,
    do,
):
  """VJP rule for FlashAttention."""
  if save_residuals:
    raise NotImplementedError("Higher-order AD not supported")
  if causal:
    raise NotImplementedError("AD for causal attention not implemented")
  (q, k, v, ab, o, l, m) = residuals
  if ab is not None:
    raise NotImplementedError("AD with attention bias not implemented")
  if not block_sizes.has_backward_blocks:
    raise ValueError(
        "Program is being differentiated, but not all backward blocks are"
        " specified"
    )

  di = jnp.sum(
      o.astype(jnp.float32) * do.astype(jnp.float32), axis=-1
  )  # [batch_size, num_heads, q_seq_len]

  dk, dv = _flash_attention_bwd_dkv(
      q, k, v, ab, l, m, do, di,
      block_q_major=block_sizes.block_q_major_dkv,
      block_k_major=block_sizes.block_k_major_dkv,
      block_k=block_sizes.block_k_dkv,
      block_q=block_sizes.block_q_dkv,
      sm_scale=sm_scale,
      debug=debug)

  dq = _flash_attention_bwd_dq(q, k, v, ab, l, m, do, di,
                               block_q_major=block_sizes.block_q_dq,
                               block_k_major=block_sizes.block_k_major_dq,
                               block_k=block_sizes.block_k_dq,
                               sm_scale=sm_scale,
                               debug=debug)
  return dq, dk, dv, None


_flash_attention.defvjp(fwd=_flash_attention_fwd, bwd=_flash_attention_bwd)


MIN_BLOCK_SIZE = 128
TRANS_B_DIM_NUMBERS = (((1,), (1,)), ((), ()))


def below_or_on_diag(r, r_blk_size, c, c_blk_size):
  # A block is considered below or on diagonal as long as the bottom left
  # corner of the block is below or on diagonal.
  return ((r + 1) * r_blk_size - 1) > (c * c_blk_size)


def _flash_attention_kernel(q_tile_ref, *args, **kwargs):
  block_b = q_tile_ref.shape[0]
  # If we're not going to tile the softmax, then we can avoid a bunch of VPU ops.
  if kwargs["block_k"] == kwargs["kv_seq_len"]:
    kernel = _flash_attention_kernel_single_batch_single_step
  else:
    kernel = _flash_attention_kernel_single_batch
  for batch_idx in range(block_b):
    kernel((batch_idx, 0), q_tile_ref, *args, **kwargs)


def _flash_attention_kernel_single_batch(
    batch_idx: tuple[int, ...],
    q_tile_ref,
    k_tile_ref,
    v_tile_ref,
    ab_tile_ref,  # Input arrays
    o_tile_ref,  # Output arrays
    m_scratch_ref,
    l_scratch_ref,
    acc_scratch_ref,
    l_ref: Any | None = None,
    m_ref: Any | None = None,
    *,
    causal,
    sm_scale,
    block_k,
    kv_seq_len,
    mask_value,
):
  block_k_major = k_tile_ref.shape[2]
  block_q = q_tile_ref.shape[2]
  head_dim = q_tile_ref.shape[-1]

  kv_seq_idx = pl.program_id(3)
  @when(kv_seq_idx == 0)
  def start_new_sequence():
    m_scratch_ref[batch_idx] = jnp.full(
        m_scratch_ref.shape[2:], -jnp.inf, jnp.float32
    )
    l_scratch_ref[batch_idx] = jnp.zeros(l_scratch_ref.shape[2:], jnp.float32)
    acc_scratch_ref[batch_idx] = jnp.zeros(
        acc_scratch_ref.shape[2:], jnp.float32
    )

  q_seq_idx = pl.program_id(2)
  should_run = lax.select(
      causal,
      below_or_on_diag(q_seq_idx, block_q, kv_seq_idx, block_k_major),
      True,
  )

  @when(should_run)
  def run():
    @functools.partial(
        lax.fori_loop, 0, block_k_major // block_k, init_val=None
    )
    def body(i, _):
      m_prev = m_scratch_ref[batch_idx]
      l_prev = l_scratch_ref[batch_idx]
      q = q_tile_ref[batch_idx]  # [block_q, head_dim]
      start_k = i * block_k
      k = pl.load(
          k_tile_ref, (*batch_idx, pl.dslice(start_k, block_k), slice(None))
      )  # [block_k, head_dim]

      s = jax.lax.dot_general(
          q, k, TRANS_B_DIM_NUMBERS, preferred_element_type=jnp.float32
      )  # [block_q, block_k]

      # Add attention bias if needed.
      # TODO(tanburn) Should the attention bias be added before or after
      # multiplication by sm_scale?
      if ab_tile_ref is not None:
        ab = pl.load(
            ab_tile_ref,
            (*batch_idx, pl.dslice(None), pl.dslice(start_k, block_k))
        ).astype(jnp.float32)
        s += ab

      if sm_scale != 1.0:
        s *= sm_scale

      if causal:
        mask_shape = (block_q, block_k)
        row_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 0)
        row_ids += q_seq_idx * block_q
        col_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 1)
        col_ids += kv_seq_idx * block_k_major + i * block_k
        causal_mask = row_ids < col_ids
        s = s + jnp.where(causal_mask, mask_value, 0.0)

      m_curr = jnp.max(s, axis=1)[:, None]  # Row max, shape [block_q, 1].
      m_next = jnp.maximum(m_prev, m_curr)  # Shape [block_q, 128].

      block_k_repeats, rem = divmod(block_k, MIN_BLOCK_SIZE)
      if rem:
        raise NotImplementedError(
            f"{block_k=} should be a multiple of {MIN_BLOCK_SIZE}"
        )
      p = jnp.exp(s - pltpu.repeat(m_next, block_k_repeats, 1))

      alpha = jnp.exp(m_prev - m_next)  # Shape [block_q, 128].

      l_corr = alpha * l_prev

      l_next = jnp.sum(p, axis=1)[:, None] + l_corr  # Shape [block_q, 128]

      head_dim_repeats, rem = divmod(head_dim, MIN_BLOCK_SIZE)
      if rem:
        raise NotImplementedError(
            f"{head_dim=} should be a multiple of {MIN_BLOCK_SIZE}"
        )
      l_scratch_ref[batch_idx] = l_next
      m_scratch_ref[batch_idx] = m_next

      l_next_inv_safe = jnp.where(l_next == 0.0, 1.0, 1.0 / l_next)
      acc_scratch_ref[batch_idx] *= pltpu.repeat(
          l_corr * l_next_inv_safe, head_dim_repeats, 1
      )
      v = pl.load(
          v_tile_ref, (*batch_idx, pl.dslice(start_k, block_k), slice(None))
      )
      o_curr = jax.lax.dot(
          p.astype(v.dtype), v, preferred_element_type=jnp.float32
      )
      acc_scratch_ref[batch_idx] += o_curr * pltpu.repeat(
          l_next_inv_safe, head_dim_repeats, 1
      )

  @when(kv_seq_idx == (kv_seq_len // block_k_major) - 1)
  def store_output():
    o_tile_ref[batch_idx] = acc_scratch_ref[batch_idx].astype(o_tile_ref.dtype)
    if l_ref is not None:
      l_ref[batch_idx] = l_scratch_ref[batch_idx].astype(l_ref.dtype)
    if m_ref is not None:
      m_ref[batch_idx] = m_scratch_ref[batch_idx].astype(m_ref.dtype)


def _flash_attention_kernel_single_batch_single_step(
    batch_idx: tuple[int, ...],
    q_tile_ref,
    k_tile_ref,
    v_tile_ref,
    ab_tile_ref,  # Input arrays
    o_tile_ref,  # Output arrays
    m_scratch_ref,
    l_scratch_ref,
    acc_scratch_ref,
    l_ref: Any | None = None,
    m_ref: Any | None = None,
    *,
    causal,
    sm_scale,
    block_k,
    kv_seq_len,
    mask_value,
):
  block_k_major = k_tile_ref.shape[2]
  block_q = q_tile_ref.shape[2]

  scratch_refs = (m_scratch_ref, l_scratch_ref, acc_scratch_ref)
  assert all(ref is None for ref in scratch_refs)
  assert kv_seq_len == block_k_major == block_k

  q = q_tile_ref[batch_idx]  # [block_q, head_dim]
  k = k_tile_ref[batch_idx]  # [block_k, head_dim]
  s = jax.lax.dot_general(
      q, k, TRANS_B_DIM_NUMBERS, preferred_element_type=jnp.float32
  )  # [block_q, block_k]

  if ab_tile_ref is not None:
    s += ab_tile_ref[batch_idx].astype(jnp.float32)
  if sm_scale != 1.0:
    s *= sm_scale

  if causal:
    q_seq_idx = pl.program_id(2)
    mask_shape = (block_q, block_k)
    row_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 0)
    row_ids += q_seq_idx * block_q
    col_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 1)
    causal_mask = row_ids < col_ids
    s = s + jnp.where(causal_mask, mask_value, 0.0)

  m = jnp.max(s, axis=1)[:, None]
  p = jnp.exp(s - m)
  l = jnp.sum(p, axis=1)[:, None]
  p /= l

  if m_ref is not None:
    m_ref[batch_idx] = lax.broadcast_in_dim(m, m_ref.shape[2:], range(2))
  if l_ref is not None:
    l_ref[batch_idx] = lax.broadcast_in_dim(l, l_ref.shape[2:], range(2))

  v = v_tile_ref[batch_idx]
  o_tile_ref[batch_idx] = jax.lax.dot(
      p.astype(v.dtype), v, preferred_element_type=jnp.float32
  ).astype(o_tile_ref.dtype)


def _flash_attention_impl(
    q, k, v, ab, save_residuals, causal, sm_scale,
    block_b, block_q, block_k_major, block_k,
    debug
):
  batch_size, num_heads, q_seq_len, head_dim = q.shape
  _, _, kv_seq_len, _ = k.shape
  _verify_block("block_q", "q_seq_len", block_q, q_seq_len, should_divide=False)
  _verify_block("block_k_major", "kv_seq_len", block_k_major, kv_seq_len)
  _verify_block("block_k", "kv_seq_len", block_k, kv_seq_len)
  _verify_block("block_b", "batch", block_b, batch_size, should_divide=False)

  # TODO(apaszke): Tile over heads as well.
  grid = (
      pl.cdiv(batch_size, block_b),
      num_heads,
      pl.cdiv(q_seq_len, block_q),
      kv_seq_len // block_k_major,
  )

  def q_index_map(batch_index, head_index, q_seq_index, _):
    return (batch_index, head_index, q_seq_index, 0)

  def kv_index_map(batch_index, head_index, q_seq_index, kv_seq_index):
    should_run = lax.select(
        causal,
        below_or_on_diag(q_seq_index, block_q, kv_seq_index, block_k_major),
        True,
    )
    # If the kv block is skipped, prefetch the next valid kv block, i.e. the
    # 0th one to be used for the next block_q rows.
    next_kv_index = lax.select(should_run, kv_seq_index, 0)
    return (batch_index, head_index, next_kv_index, 0)

  def ab_index_map(batch_index, head_index, q_seq_index, kv_seq_index):
    should_run = lax.select(
        causal,
        below_or_on_diag(q_seq_index, block_q, kv_seq_index, block_k_major),
        True,
    )
    # If the ab block is skipped, prefetch the next valid ab block, i.e. the
    # 0th kv to be used for the next block_q rows.
    next_q_index = lax.select(
        should_run,
        q_seq_index,
        lax.select(
            q_seq_index == (q_seq_len // block_q) - 1, 0, q_seq_index + 1
        ),
    )
    next_kv_index = lax.select(should_run, kv_seq_index, 0)
    return (batch_index, head_index, next_q_index, next_kv_index)

  def o_index_map(batch_index, head_index, q_seq_index, _):
    return (batch_index, head_index, q_seq_index, 0)

  def lm_index_map(batch_index, head_index, q_seq_index, _):
    return (batch_index, head_index, q_seq_index, 0)

  kernel = functools.partial(
      _flash_attention_kernel,
      causal=causal,
      mask_value=DEFAULT_MASK_VALUE,
      sm_scale=sm_scale,
      block_k=block_k,
      kv_seq_len=kv_seq_len,
  )
  out_shape = jax.ShapeDtypeStruct(shape=q.shape, dtype=q.dtype)
  out_shape = [out_shape]
  out_specs = [pl.BlockSpec(o_index_map, (block_b, 1, block_q, head_dim))]

  if block_k != kv_seq_len:
    scratch_shape = functools.partial(jax.ShapeDtypeStruct, dtype=jnp.float32)
    m_scratch = scratch_shape((block_b, 1, block_q, MIN_BLOCK_SIZE))
    l_scratch = scratch_shape((block_b, 1, block_q, MIN_BLOCK_SIZE))
    acc_scratch = scratch_shape((block_b, 1, block_q, head_dim))
    out_shape += [m_scratch, l_scratch, acc_scratch]
    out_specs += [
        pl.BlockSpec(lambda *_: (0, 0, 0, 0), m_scratch.shape),
        pl.BlockSpec(lambda *_: (0, 0, 0, 0), l_scratch.shape),
        pl.BlockSpec(lambda *_: (0, 0, 0, 0), acc_scratch.shape),
    ]
  else:
    out_shape += [None, None, None]
    out_specs += [None, None, None]

  if save_residuals:
    out_specs = [
        *out_specs,
        pl.BlockSpec(lm_index_map, (block_b, 1, block_q, MIN_BLOCK_SIZE)),
        pl.BlockSpec(lm_index_map, (block_b, 1, block_q, MIN_BLOCK_SIZE)),
    ]
    l = jax.ShapeDtypeStruct(
        (batch_size, num_heads, q_seq_len, MIN_BLOCK_SIZE), dtype=jnp.float32
    )
    m = jax.ShapeDtypeStruct(
        (batch_size, num_heads, q_seq_len, MIN_BLOCK_SIZE), dtype=jnp.float32
    )
    out_shape = (*out_shape, l, m)

  ab_block_spec = (
      pl.BlockSpec(ab_index_map, (block_b, 1, block_q, block_k_major))
      if ab is not None else None)
  o, *aux = pl.pallas_call(
      kernel,
      out_shape=out_shape,
      in_specs=[
          pl.BlockSpec(q_index_map, (block_b, 1, block_q, head_dim)),
          pl.BlockSpec(kv_index_map, (block_b, 1, block_k_major, head_dim)),
          pl.BlockSpec(kv_index_map, (block_b, 1, block_k_major, head_dim)),
          ab_block_spec,
      ],
      out_specs=out_specs,
      grid=grid,
      debug=debug,
      mosaic_params=dict(dimension_semantics=(
          "parallel", "parallel", "parallel", "arbitrary"
      ))
  )(q, k, v, ab)
  if save_residuals:
    l, m = (v[..., 0] for v in aux[-2:])
    return (o, l, m)
  else:
    return o


def _flash_attention_dkv_kernel(
    q_tile_ref,
    k_tile_ref,
    v_tile_ref,
    l_tile_ref,
    m_tile_ref,
    do_tile_ref,
    di_tile_ref,
    dk_tile_ref,
    dv_tile_ref,
    dk_scratch_ref,
    dv_scratch_ref,
    *,
    sm_scale: float,
    q_seq_len: int,
    block_q: int,
    block_k: int,
):
  _, _, block_q_major, _ = q_tile_ref.shape
  _, _, block_k_major, _ = k_tile_ref.shape

  q_seq_index = pl.program_id(axis=3)

  @when(q_seq_index == 0)
  def start_new_sequence():
    dk_scratch_ref[:, :] = jnp.zeros(dk_scratch_ref.shape, dk_scratch_ref.dtype)
    dv_scratch_ref[:, :] = jnp.zeros(dv_scratch_ref.shape, dv_scratch_ref.dtype)

  def q_body(j, _):
    start_q = j * block_q
    def k_body(i, _):
      start_k = i * block_k
      k = pl.load(k_tile_ref, (0, 0, pl.ds(start_k, block_k), slice(None)))
      v = pl.load(v_tile_ref, (0, 0, pl.ds(start_k, block_k), slice(None)))
      q = pl.load(q_tile_ref, (0, 0, pl.ds(start_q, block_q), slice(None))
                  )  # [block_q, head_dim]
      l = pl.load(l_tile_ref, (0, 0, pl.ds(start_q, block_q), slice(None))
                  )  # [block_q, 128]
      m = pl.load(m_tile_ref, (0, 0, pl.ds(start_q, block_q), slice(None))
                  )  # [block_q, 128]
      do = pl.load(do_tile_ref, (0, 0, pl.ds(start_q, block_q), slice(None))
                  )  # [block_q, 128]
      di = pl.load(di_tile_ref, (0, 0, pl.ds(start_q, block_q), slice(None))
                  ).astype(jnp.float32)  # [block_q, 128]

      capped_logits = lax.dot_general(
          q, k, TRANS_B_DIM_NUMBERS, preferred_element_type=jnp.float32
      )  # [block_q_major, block_k]
      if sm_scale != 1.0:
        capped_logits *= sm_scale

      p = jnp.exp(
          capped_logits - pltpu.repeat(m, block_k // MIN_BLOCK_SIZE, axis=1)
      )
      p = p * pltpu.repeat(
          1 / l, block_k // MIN_BLOCK_SIZE, axis=1
      )  # [block_q_major, block_k_major]
      dv = lax.dot(p.T.astype(do.dtype), do, preferred_element_type=jnp.float32)
      pl.store(dv_scratch_ref, (pl.ds(start_k, block_k), slice(None)),
               pl.load(dv_scratch_ref, (pl.ds(start_k, block_k), slice(None)))
               + dv.astype(dv_scratch_ref.dtype))

      # di: [block_q, 128]
      # do: [block_q, head_dim]
      # v: [block_k_major, head_dim]
      dp = lax.dot_general(
          do, v, TRANS_B_DIM_NUMBERS, preferred_element_type=jnp.float32
      )
      ds = (dp - pltpu.repeat(di, block_k // MIN_BLOCK_SIZE, axis=1)) * p

      if sm_scale != 1.0:
        ds = ds * sm_scale

      # ds: [block_q_major, block_k_major]
      # q: [block_q_major, head_dim]
      dk = lax.dot(ds.T.astype(do.dtype), q, preferred_element_type=jnp.float32)
      pl.store(dk_scratch_ref, (pl.ds(start_k, block_k), slice(None)),
               pl.load(dk_scratch_ref, (pl.ds(start_k, block_k), slice(None)))
               + dk.astype(dk_scratch_ref.dtype))
    lax.fori_loop(0, block_k_major // block_k, k_body, None)
  lax.fori_loop(0, block_q_major // block_q, q_body, None)

  @when(q_seq_index == q_seq_len // block_q_major - 1)
  def end_of_q_sequence():
    dv_tile_ref[0, 0, :, :] = dv_scratch_ref[...].astype(dv_tile_ref)
    dk_tile_ref[0, 0, :, :] = dk_scratch_ref[...].astype(dk_tile_ref)


def _flash_attention_bwd_dkv(
    q, k, v, ab, l, m, do, di, *,
    block_q_major: int | None,
    block_q: int | None,
    block_k_major: int | None,
    block_k: int | None,
    sm_scale: float,
    debug: bool = False,
):
  batch_size, num_heads, q_seq_len, head_dim = q.shape
  _, _, kv_seq_len, _ = k.shape
  if ab is not None:
    raise NotImplementedError("Attention bias and AD")
  _verify_block("block_q_major_dkv", "q_seq_len", block_q_major, q_seq_len)
  _verify_block("block_q_dkv", "q_seq_len", block_q, q_seq_len)
  _verify_block("block_k_major_dkv", "kv_seq_len", block_k_major, kv_seq_len)
  _verify_block("block_k_dkv", "kv_seq_len", block_k, kv_seq_len)

  # Broadcast out scalar values
  m = jnp.broadcast_to(m[..., None], (*m.shape, MIN_BLOCK_SIZE))
  l = jnp.broadcast_to(l[..., None], (*l.shape, MIN_BLOCK_SIZE))
  # Preprocess contraction for bwd pass
  di = jnp.broadcast_to(di[..., None], (*di.shape, MIN_BLOCK_SIZE))

  grid = (
      batch_size,
      kv_seq_len // block_k_major,
      num_heads,
      q_seq_len // block_q_major
  )

  def qo_index_map(batch_index, _, head_index, q_seq_index):
    return (batch_index, head_index, q_seq_index, 0)
  qo_spec = pl.BlockSpec(qo_index_map, (1, 1, block_q_major, head_dim))
  assert q.ndim == len(qo_spec.block_shape)
  do_spec = qo_spec
  assert do.ndim == len(qo_spec.block_shape)

  def kv_index_map(batch_index, kv_seq_index, head_index, q_seq_index):
    del q_seq_index
    return (batch_index, head_index, kv_seq_index, 0)
  kv_spec = pl.BlockSpec(kv_index_map, (1, 1, block_k_major, head_dim))
  assert k.ndim == len(kv_spec.block_shape)
  assert v.ndim == len(kv_spec.block_shape)

  def lm_index_map(batch_index, _, head_index, q_seq_index):
    return (batch_index, head_index, q_seq_index, 0)
  lm_spec = pl.BlockSpec(lm_index_map, (1, 1, block_q_major, MIN_BLOCK_SIZE))
  assert l.ndim == len(lm_spec.block_shape)
  assert m.ndim == len(lm_spec.block_shape)

  di_spec = pl.BlockSpec(qo_index_map, (1, 1, block_q_major, MIN_BLOCK_SIZE))
  assert di.ndim == len(di_spec.block_shape)

  in_specs = [
      qo_spec, kv_spec, kv_spec, lm_spec, lm_spec, do_spec, di_spec,
  ]

  out_shapes = [
      jax.ShapeDtypeStruct((batch_size, num_heads, kv_seq_len, head_dim),
                           k.dtype),
      jax.ShapeDtypeStruct((batch_size, num_heads, kv_seq_len, head_dim),
                           v.dtype),
      jax.ShapeDtypeStruct((block_k_major, head_dim), jnp.float32),
      jax.ShapeDtypeStruct((block_k_major, head_dim), jnp.float32),
  ]
  def dkv_index_map(batch_index, kv_seq_index, head_index, q_seq_index):
    del q_seq_index
    return (batch_index, head_index, kv_seq_index, 0)

  dkv_spec = pl.BlockSpec(dkv_index_map, (1, 1, block_k_major, head_dim))
  out_specs = [
      dkv_spec, dkv_spec,
      pl.BlockSpec(lambda *_: (0, 0), (block_k_major, head_dim)),
      pl.BlockSpec(lambda *_: (0, 0), (block_k_major, head_dim)),
  ]

  kernel = functools.partial(_flash_attention_dkv_kernel,
                             block_q=block_q,
                             block_k=block_k,
                             sm_scale=sm_scale,
                             q_seq_len=q_seq_len)
  name_scope = f"flash_mha_bwd_dkv_{block_q_major=}_{block_q=}_{block_k_major=}_{block_k=}"
  with jax.named_scope(name_scope):
    dk, dv, _, _ = pl.pallas_call(
        kernel,
        in_specs=in_specs,
        out_shape=out_shapes,
        out_specs=out_specs,
        grid=grid,
        debug=debug,
        mosaic_params=dict(dimension_semantics=(
            "parallel", "parallel", "parallel", "arbitrary"
        ))
    )(q, k, v, l, m, do, di)
    assert dk.shape == k.shape
    assert dv.shape == v.shape
  return dk, dv


def _flash_attention_dq_kernel(
    q_tile_ref,
    k_tile_ref,
    v_tile_ref,
    l_tile_ref,
    m_tile_ref,
    do_tile_ref,
    di_tile_ref,
    dq_tile_ref,
    dq_scratch_ref,
    *,
    sm_scale: float,
    kv_seq_len: int,
    block_k: int,
):
  _, _, block_k_major, _ = k_tile_ref.shape

  kv_seq_index = pl.program_id(axis=3)

  @when(kv_seq_index == 0)
  def start_new_sequence():
    dq_scratch_ref[:, :] = jnp.zeros(dq_scratch_ref.shape, dq_scratch_ref.dtype)

  def body(i, _):
    k_slice = pl.ds(i * block_k, block_k)
    q = q_tile_ref[0, 0, :, :]
    k = pl.load(
        k_tile_ref, (0, 0, k_slice, slice(None)),
    )  # [block_k, head_dim]
    v = pl.load(
        v_tile_ref, (0, 0, k_slice, slice(None)),
    )  # [block_k, head_dim]
    l = l_tile_ref[0, 0, :, :]  # [block_q_major, 128]
    m = m_tile_ref[0, 0, :, :]  # [block_q_major, 128]
    do = do_tile_ref[0, 0, :, :]  # [block_q_major, head_dim]
    di = di_tile_ref[0, 0, :].astype(jnp.float32)  # [block_q_major, 128]

    capped_logits = jax.lax.dot_general(
        q, k, TRANS_B_DIM_NUMBERS, preferred_element_type=jnp.float32
    )
    if sm_scale != 1.0:
      capped_logits *= sm_scale

    p = jnp.exp(
        capped_logits - pltpu.repeat(m, block_k // MIN_BLOCK_SIZE, axis=1)
    )
    p = p * pltpu.repeat(
        1 / l, block_k // MIN_BLOCK_SIZE, axis=1
    )  # [block_q_major, block_k]

    # di: [block_q_major, 128]
    # do: [block_q_major, head_dim]
    # v: [block_k_major, head_dim]
    dp = jax.lax.dot_general(
        do,
        v,
        TRANS_B_DIM_NUMBERS,
        preferred_element_type=jnp.float32,
    )
    ds = (dp - pltpu.repeat(di, block_k // MIN_BLOCK_SIZE, axis=1)) * p
    # dp = jnp.dot(do, v.T)
    # ds = (dp - (dp * p).sum(axis=1)[:, None]) * p

    if sm_scale != 1.0:
      ds = ds * sm_scale

    # dp: [block_q_major, block_k]
    # k: [block_k, head_dim]
    dq_scratch_ref[:, :] += lax.dot(
        ds.astype(k.dtype),
        k,
        preferred_element_type=jnp.float32,
    ).astype(dq_scratch_ref.dtype)
  lax.fori_loop(0, block_k_major // block_k, body, None)

  @when(kv_seq_index == kv_seq_len // block_k_major - 1)
  def end_of_kv_sequence():
    dq_tile_ref[0, 0, :, :] = dq_scratch_ref[...].astype(dq_tile_ref)
    dq_scratch_ref[...] = jnp.zeros_like(dq_scratch_ref)


def _flash_attention_bwd_dq(
    q, k, v, ab, l, m, do, di,
    *, block_q_major: int | None, block_k_major: int | None,
    block_k: int | None,
    sm_scale: float,
    debug: bool,
):
  batch_size, num_heads, q_seq_len, head_dim = q.shape
  _, _, kv_seq_len, _ = k.shape
  if ab is not None:
    raise NotImplementedError("Attention bias handling not implemented")
  _verify_block("block_q_dq", "q_seq_len", block_q_major, q_seq_len)
  _verify_block("block_k_major_dq", "kv_seq_len", block_k_major, kv_seq_len)
  _verify_block("block_k_dq", "block_k", block_k, kv_seq_len)

  # Broadcast out scalar values
  m = jnp.broadcast_to(m[..., None], (*m.shape, MIN_BLOCK_SIZE))
  l = jnp.broadcast_to(l[..., None], (*l.shape, MIN_BLOCK_SIZE))
  # Preprocess contraction for bwd pass
  di = jnp.broadcast_to(di[..., None], (*di.shape, block_k_major))

  grid = (
      batch_size,
      num_heads,
      q_seq_len // block_q_major,
      kv_seq_len // block_k_major,
  )

  def qo_index_map(batch_index, head_index, q_seq_index, kv_seq_index):
    del kv_seq_index
    return (batch_index, head_index, q_seq_index, 0)

  qo_spec = pl.BlockSpec(qo_index_map, (1, 1, block_q_major, head_dim))
  do_spec = qo_spec

  def kv_index_map(batch_index, head_index, q_seq_index, kv_seq_index):
    del q_seq_index  # Unused.
    return (batch_index, head_index, kv_seq_index, 0)

  kv_spec = pl.BlockSpec(kv_index_map, (1, 1, block_k_major, head_dim))
  assert k.ndim == len(kv_spec.block_shape)
  assert v.ndim == len(kv_spec.block_shape)

  def lm_index_map(batch_index, head_index, q_seq_index, kv_seq_index):
    del kv_seq_index
    return (batch_index, head_index, q_seq_index, 0)

  lm_spec = pl.BlockSpec(lm_index_map, (1, 1, block_q_major, MIN_BLOCK_SIZE))
  assert l.ndim == len(lm_spec.block_shape)
  assert m.ndim == len(lm_spec.block_shape)

  di_spec = pl.BlockSpec(qo_index_map, (1, 1, block_q_major, MIN_BLOCK_SIZE))
  assert di.ndim == len(di_spec.block_shape)

  in_specs = [
      qo_spec, kv_spec, kv_spec, lm_spec, lm_spec, do_spec, di_spec,
  ]

  out_shapes = [
      jax.ShapeDtypeStruct(q.shape, q.dtype),
      jax.ShapeDtypeStruct(
          (block_q_major, head_dim), jnp.float32
      ),
  ]
  dq_spec = pl.BlockSpec(qo_index_map, (1, 1, block_q_major, head_dim))
  out_specs = [
      dq_spec,
      pl.BlockSpec(lambda *_: (0, 0), (block_q_major, head_dim)),
  ]

  kernel = functools.partial(_flash_attention_dq_kernel,
                             sm_scale=sm_scale,
                             block_k=block_k,
                             kv_seq_len=kv_seq_len)
  name_scope = (
      f"flash_mha_bwd_dq_{block_q_major=}_{block_k_major=}_{block_k=}")
  with jax.named_scope(name_scope):
    dq, _ = pl.pallas_call(
        kernel,
        in_specs=in_specs,
        out_shape=out_shapes,
        out_specs=out_specs,
        grid=grid,
        debug=debug,
        mosaic_params=dict(dimension_semantics=(
            "parallel", "parallel", "parallel", "arbitrary"
        ))
    )(q, k, v, l, m, do, di)
  return dq


# For autograd testing.
def mha_reference_no_custom_vjp(
    q,
    k,
    v,
    ab: jax.Array | None = None,
    *,
    causal: bool = False,
    mask_value: float = DEFAULT_MASK_VALUE,
    sm_scale: float = 1.0,
    save_residuals: bool = False,
):
  logits = jnp.einsum("bhqc,bhkc->bhqk", q, k)
  if ab is not None:
    logits += ab
  if sm_scale != 1.0:
    logits *= sm_scale

  if causal:
    _, _, q_seq_len, _ = q.shape
    _, _, kv_seq_len, _ = k.shape
    mask_shape = (q_seq_len, kv_seq_len)
    row_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 0)
    col_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 1)
    causal_mask = (row_ids < col_ids)[None, None, :, :]
    logits = logits + jnp.where(causal_mask, mask_value, 0.0)

  m = logits.max(axis=-1)
  unnormalized = jnp.exp(logits - m[..., None])
  l = unnormalized.sum(axis=-1)
  weights = unnormalized / l[..., None]
  out = jnp.einsum("bhqk,bhkc->bhqc", weights, v)
  if save_residuals:
    return out, l, m
  return out


@functools.partial(
    jax.jit, static_argnames=["causal", "mask_value", "sm_scale"]
)
@jax.default_matmul_precision("bfloat16")
def mha_reference(
    q,
    k,
    v,
    ab,
    causal: bool = False,
    mask_value: float = DEFAULT_MASK_VALUE,
    sm_scale=1.0,
):
  return _mha_reference(
      q,
      k,
      v,
      ab,
      causal=causal,
      mask_value=mask_value,
      sm_scale=sm_scale,
      save_residuals=False,
  )


@functools.partial(jax.custom_vjp, nondiff_argnums=(4, 5, 6, 7))
def _mha_reference(
    q,
    k,
    v,
    ab,
    causal: bool,
    mask_value: float,
    sm_scale: float,
    save_residuals: bool,
):
  return mha_reference_no_custom_vjp(
      q,
      k,
      v,
      ab,
      causal=causal,
      mask_value=mask_value,
      sm_scale=sm_scale,
      save_residuals=save_residuals,
  )


def _mha_reference_fwd(
    q,
    k,
    v,
    ab,
    causal: bool,
    mask_value: float,
    sm_scale: float,
    save_residuals: bool,
):
  if save_residuals:
    raise NotImplementedError
  res = _mha_reference(
      q,
      k,
      v,
      ab,
      causal=causal,
      mask_value=mask_value,
      sm_scale=sm_scale,
      save_residuals=True,
  )
  assert isinstance(res, tuple)
  out, l, m = res
  return out, (q, k, v, ab, out, l, m)


@functools.partial(
    jax.jit,
    static_argnames=[
        "causal",
        "mask_value",
        "sm_scale",
    ],
)
def mha_reference_bwd(
    q,
    k,
    v,
    ab,
    o,
    l,
    m,
    do,
    causal: bool = False,
    mask_value: float = DEFAULT_MASK_VALUE,
    sm_scale: float = 1.0,
):
  if sm_scale != 1.0:
    raise NotImplementedError

  logits = jnp.einsum(
      "bhqc,bhkc->bhqk",
      q.astype(jnp.float32),
      k.astype(jnp.float32),
  )
  if ab is not None:
    logits += ab

  if causal:
    _, _, q_seq_len, _ = q.shape
    _, _, kv_seq_len, _ = k.shape
    mask_shape = (q_seq_len, kv_seq_len)
    row_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 0)
    col_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 1)
    causal_mask = (row_ids < col_ids)[None, None, :, :]
    logits = logits + jnp.where(causal_mask, mask_value, 0.0)

  unnormalized = jnp.exp(logits - m[..., None])
  p = unnormalized / l[..., None]
  dv = jnp.einsum("bhpt,bhpd->bhtd", p, do.astype(jnp.float32)).astype(v.dtype)

  dp = jnp.einsum(
      "bhpd,bhtd->bhpt", do.astype(jnp.float32), v.astype(jnp.float32)
  )

  di = jnp.sum(o.astype(jnp.float32) * do.astype(jnp.float32), axis=-1)[
      ..., None
  ]  # [batch_size, num_heads, q_seq_len]

  ds = (dp - di) * p
  dk = jnp.einsum("bhsd,bhst->bhtd", q.astype(jnp.float32), ds).astype(k.dtype)
  dq = jnp.einsum("bhst,bhtd->bhsd", ds, k.astype(jnp.float32)).astype(q.dtype)

  # dab is just ds
  dab = ds if ab is not None else None
  return dq, dk, dv, dab


def _mha_reference_bwd(
    causal: bool,
    mask_value: float,
    sm_scale: float,
    save_residuals: bool,
    residuals,
    do,
):
  del save_residuals
  q, k, v, ab, o, l, m = residuals
  dq, dk, dv, dab = mha_reference_bwd(
      q,
      k,
      v,
      ab,
      o,
      l,
      m,
      do,
      causal=causal,
      mask_value=mask_value,
      sm_scale=sm_scale,
  )
  return dq, dk, dv, dab


_mha_reference.defvjp(fwd=_mha_reference_fwd, bwd=_mha_reference_bwd)


def _verify_block(block_name, dim_name, block, dim, should_divide=True):
  if block > dim:
    raise ValueError(
        f"{block_name}={block} should be smaller or equal to {dim_name}={dim}"
    )
  if should_divide and dim % block != 0:
    raise ValueError(
        f"{dim_name}={dim} should be divisible by {block_name}={block}"
    )
