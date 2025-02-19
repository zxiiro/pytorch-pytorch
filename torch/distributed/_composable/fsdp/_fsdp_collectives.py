from typing import List, NamedTuple, Optional

import torch
import torch.distributed as dist
from torch.utils._contextlib import _DecoratorContextManager
from ._fsdp_common import (
    _get_dim0_padded_size,
    _raise_assert_with_print,
    _to_dtype_if_needed,
)
from ._fsdp_param import FSDPParam


class AllGatherResult(NamedTuple):
    all_gather_output: torch.Tensor
    all_gather_event: Optional[torch.cuda.Event]
    all_gather_work: Optional[dist.distributed_c10d.Work]
    all_gather_input_numels: List[int]


@torch.no_grad()
def foreach_all_gather(
    fsdp_params: List[FSDPParam],
    group: dist.ProcessGroup,
    async_op: bool,
    all_gather_copy_in_stream: torch.cuda.Stream,
    all_gather_stream: torch.cuda.Stream,
    device: torch.device,
    dtype: torch.dtype,
) -> Optional[AllGatherResult]:
    world_size, rank = group.size(), group.rank()
    # - Copy in
    with torch.cuda.stream(all_gather_copy_in_stream):
        param_all_gather_inputs = [
            fsdp_param.all_gather_input for fsdp_param in fsdp_params
        ]
        inp_split_sizes = [inp.numel() for inp in param_all_gather_inputs]
        all_gather_input_numel = sum(inp_split_sizes)
        all_gather_output = torch.empty(
            (all_gather_input_numel * world_size,), dtype=dtype, device=device
        )
        all_gather_input = all_gather_output.narrow(
            0, all_gather_input_numel * rank, all_gather_input_numel
        )
        foreach_copy_dsts = torch.split(all_gather_input, inp_split_sizes)
        torch._foreach_copy_(foreach_copy_dsts, param_all_gather_inputs)
        del param_all_gather_inputs
        all_gather_copy_in_event = torch.cuda.Event()
        all_gather_copy_in_event.record()
    all_gather_stream.wait_event(all_gather_copy_in_event)
    with torch.cuda.stream(all_gather_stream):
        # - All-gather
        all_gather_work = dist.all_gather_into_tensor(
            output_tensor=all_gather_output,
            input_tensor=all_gather_input,
            group=group,
            async_op=async_op,
        )
        all_gather_event = torch.cuda.Event()
        all_gather_event.record()
        return AllGatherResult(
            all_gather_output, all_gather_event, all_gather_work, inp_split_sizes
        )


def foreach_all_gather_copy_out(
    all_gather_result: AllGatherResult,
    fsdp_params: List[FSDPParam],
    group: dist.ProcessGroup,
) -> None:
    (
        all_gather_output,
        all_gather_event,
        all_gather_work,
        all_gather_input_numels,
    ) = all_gather_result
    if all_gather_event is not None:  # sync op
        torch.cuda.current_stream().wait_event(all_gather_event)
    if all_gather_work is not None:  # async op
        all_gather_work.wait()
    world_size = group.size()
    dtype, device = all_gather_output.dtype, all_gather_output.device
    for all_gather_input_numel, fsdp_param in zip(all_gather_input_numels, fsdp_params):
        fsdp_param.init_all_gather_output(
            all_gather_input_numel, world_size, dtype, device
        )  # no-op after 1st call
        fsdp_param.alloc_all_gather_output()
    all_gather_output = all_gather_output.view(world_size, -1)
    out = [
        fsdp_param.all_gather_output.view(world_size, -1) for fsdp_param in fsdp_params
    ]
    with _unsafe_preserve_version_counters(out):
        torch.split_with_sizes_copy(
            all_gather_output, all_gather_input_numels, dim=1, out=out
        )


@torch.no_grad()
def foreach_reduce_scatter(
    fsdp_params: List[FSDPParam],
    unsharded_grads: List[torch.Tensor],
    group: dist.ProcessGroup,
    reduce_scatter_stream: torch.cuda.Stream,
    orig_dtype: torch.dtype,
    reduce_dtype: Optional[torch.dtype],
    device: torch.device,
    predivide_factor: float,
    postdivide_factor: float,
) -> torch.cuda.Event:
    """
    ``unsharded_grads`` owns the references to the gradients computed by
    autograd, so clearing the list frees the gradients.
    """
    grad_dtypes = {grad.dtype for grad in unsharded_grads}
    if len(grad_dtypes) != 1:
        # Check this at runtime since it could be a real runtime error if e.g.
        # fp8 weights do not produce the correct higher precision gradients
        _raise_assert_with_print(
            f"FSDP reduce-scatter expects uniform gradient dtype but got {grad_dtypes}"
        )
    grad_dtype = unsharded_grads[0].dtype
    reduce_dtype = reduce_dtype or grad_dtype
    world_size = group.size()
    padded_unsharded_sizes = tuple(
        _get_dim0_padded_size(grad.size(), world_size) for grad in unsharded_grads
    )
    reduce_scatter_input_numel = sum(s.numel() for s in padded_unsharded_sizes)
    reduce_scatter_output_numel = reduce_scatter_input_numel // world_size
    current_stream = torch.cuda.current_stream()
    reduce_scatter_stream.wait_stream(current_stream)
    with torch.cuda.stream(reduce_scatter_stream):
        reduce_scatter_input = torch.empty(
            (reduce_scatter_input_numel,), dtype=reduce_dtype, device=device
        )
        foreach_reduce_scatter_copy_in(
            unsharded_grads, reduce_scatter_input, world_size
        )
        _div_if_needed(reduce_scatter_input, predivide_factor)
        # Record to mark the end of the reduce-scatter copy-in in the RS stream
        copy_in_event = torch.cuda.Event()
        copy_in_event.record()
        # Only after the copy-in finishes can we free the gradients, which were
        # computed in the default stream
        current_stream.wait_event(copy_in_event)
        unsharded_grads.clear()
        reduce_scatter_output = reduce_scatter_input.new_empty(
            (reduce_scatter_output_numel,)
        )
        dist.reduce_scatter_tensor(
            output=reduce_scatter_output, input=reduce_scatter_input, group=group
        )
        _div_if_needed(reduce_scatter_output, postdivide_factor)
        reduce_scatter_output = _to_dtype_if_needed(reduce_scatter_output, orig_dtype)
        # - View out and accumulate
        flat_grad_offset = 0  # [0, reduce_scatter_output_numel - 1]
        for padded_unsharded_size, fsdp_param in zip(
            padded_unsharded_sizes, fsdp_params
        ):
            new_sharded_grad = torch.as_strided(
                reduce_scatter_output,
                size=fsdp_param.sharded_size,
                stride=fsdp_param.contiguous_sharded_stride,
                storage_offset=flat_grad_offset,
            )
            to_accumulate_grad = fsdp_param.sharded_param.grad is not None
            new_sharded_dtensor_grad = fsdp_param.to_sharded_dtensor(new_sharded_grad)
            if to_accumulate_grad:
                fsdp_param.sharded_param.grad += new_sharded_dtensor_grad
            else:
                fsdp_param.sharded_param.grad = new_sharded_dtensor_grad
            padded_sharded_numel = padded_unsharded_size.numel() // world_size
            flat_grad_offset += padded_sharded_numel
        reduce_scatter_view_out_event = torch.cuda.Event()
        reduce_scatter_view_out_event.record()
    # The RS output is allocated in the RS stream and used in the default
    # stream (for optimizer). To ensure its memory is not reused for later
    # RSs, we do not need extra synchronization since the sharded parameters
    # hold refs through the end of backward.
    return reduce_scatter_view_out_event


def foreach_reduce_scatter_copy_in(
    unsharded_grads: List[torch.Tensor],
    reduce_scatter_input: torch.Tensor,
    world_size: int,
) -> None:
    grad_views: List[torch.Tensor] = []
    grads_to_copy: List[torch.Tensor] = []
    padded_grad_slices: List[torch.Tensor] = []
    for grad in unsharded_grads:
        grad_size = grad.size()
        dim0_padded_size = _get_dim0_padded_size(grad_size, world_size)
        if dim0_padded_size != grad_size:
            padded_grad = grad.new_empty(dim0_padded_size)
            padded_grad_slices.append(padded_grad[: grad.size(0)])
            grads_to_copy.append(grad)
            grad = padded_grad
        grad_views.append(grad.view(world_size, -1))
    if padded_grad_slices:
        torch._foreach_copy_(padded_grad_slices, grads_to_copy)
    torch.cat(grad_views, dim=-1, out=reduce_scatter_input.view(world_size, -1))


def _div_if_needed(tensor: torch.Tensor, div_factor: float) -> None:
    if div_factor > 1:
        tensor.div_(div_factor)


# We need this context for the backward all-gather, which would otherwise
# raise an error when writing to the all-gather output tensors in-place, e.g.:
# RuntimeError: one of the variables needed for gradient computation has been
# modified by an inplace operation: [torch.cuda.FloatTensor [15, 3]], which is
# output 0 of AsStridedBackward0, is at version 3; expected version 2 instead.
class _unsafe_preserve_version_counters(_DecoratorContextManager):
    # Same as `_unsafe_preserve_version_counter` but only entering/exiting the
    # context manager once for a list of tensors to reduce CPU overhead
    def __init__(self, tensors: List[torch.Tensor]) -> None:
        self.tensors = tensors
        self.prev_versions = [t._version for t in tensors]

    def __enter__(self) -> None:
        pass

    def __exit__(self, *args) -> None:
        for tensor, prev_version in zip(self.tensors, self.prev_versions):
            torch._C._autograd._unsafe_set_version_counter(tensor, prev_version)
