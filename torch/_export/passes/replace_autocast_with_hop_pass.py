# mypy: allow-untyped-defs
import contextlib
import copy

from typing import List

import torch
from torch._higher_order_ops.wrap import wrap_with_autocast

from ..utils import (
    node_inline_,
    node_replace_,
    nodes_filter,
    nodes_first,
    nodes_map,
    sequential_split,
)


def _is_autocast_node(node: torch.fx.Node):
    return (
        node
        and node.op == "call_function"
        and node.target
        in [
            torch.amp.autocast_mode._enter_autocast,
            torch.amp.autocast_mode._exit_autocast,
        ]
    )


def _is_enter_autocast_node(node: torch.fx.Node):
    return (
        node
        and node.op == "call_function"
        and node.target == torch.amp.autocast_mode._enter_autocast
    )


def _is_exit_autocast_node(node: torch.fx.Node):
    return (
        node
        and node.op == "call_function"
        and node.target == torch.amp.autocast_mode._exit_autocast
    )


def _is_autocast_sub_mod(node: torch.fx.Node):
    """
    Check if the first non-placeholder node is `torch.amp.autocast_mode._enter_autocast`.
    """
    if node.op == "call_module":
        assert isinstance(node.target, str)
        subgm = getattr(node.graph.owning_module, node.target)
        first_non_ph = nodes_first(
            subgm.graph.nodes, lambda node: node.op != "placeholder"
        )
        if (
            first_non_ph
            and first_non_ph.op == "call_function"
            and first_non_ph.target == torch.amp.autocast_mode._enter_autocast
        ):
            # TODO: check if current auto-cast type is the same as the args of
            # _enter_autocast. If so, return False, i.e. do not create a submodule.
            return True
    return False


def _check_valid_autocast_block(enter_autocast_node, exit_autocast_node):
    assert _is_enter_autocast_node(enter_autocast_node)
    assert _is_exit_autocast_node(exit_autocast_node)
    assert exit_autocast_node.args[0] == enter_autocast_node


def _replace_with_hop(node: torch.fx.Node):
    assert node.op == "call_module"
    graph: torch.fx.Graph = node.graph
    gm: torch.fx.GraphModule = graph.owning_module
    assert isinstance(node.target, str)
    sub_gm = getattr(gm, node.target)
    sub_graph = sub_gm.graph
    autocast_nodes = nodes_filter(sub_graph.nodes, _is_autocast_node)
    if len(autocast_nodes) > 0:
        assert len(autocast_nodes) > 1  # need at least an enter node and an exist node
        enter_autocast_node = autocast_nodes[0]
        exit_autocast_node = autocast_nodes[-1]
        _check_valid_autocast_block(enter_autocast_node, exit_autocast_node)

        with graph.inserting_before(node):
            get_attr_node = graph.get_attr(node.target)
            get_attr_node.meta["nn_module_stack"] = copy.copy(
                enter_autocast_node.meta.get("nn_module_stack", {})
            )
            output_node = next(iter(reversed(sub_gm.graph.nodes)), None)
            # Split_module pass intentially doesn't add output node
            # if the graph doesn't return anything.
            # TODO (tmanlaibaatar) Figure out if this is right behaviour
            # for split_module
            if isinstance(output_node, torch.fx.Node) and output_node.op != "output":
                output_node = None
            if output_node is not None:
                assert len(output_node.args) == 1
                output_args = output_node.args[0]
                autocast_node_args = enter_autocast_node.args
                if isinstance(output_args, (tuple, list)):
                    call_func_node = graph.call_function(
                        wrap_with_autocast,
                        (autocast_node_args, get_attr_node, *node.args),
                        {},
                    )
                    # Create the metadata
                    call_func_node.meta["val"] = tuple(
                        arg.meta["val"] for arg in output_args
                    )
                    call_func_node.meta["nn_module_stack"] = copy.copy(
                        enter_autocast_node.meta.get("nn_module_stack", {})
                    )
                    call_func_node.meta["torch_fn"] = (
                        f"{wrap_with_autocast.__name__}",
                        f"{wrap_with_autocast.__class__.__name__}.{wrap_with_autocast.__name__}",
                    )
                    node_replace_(node, call_func_node, delete_old=True)

                    # Rename the name of getitem nodes to the actual name of its contents
                    # for passing verifier and better readability, also propagate metadata
                    for get_item_node in call_func_node.users.keys():
                        idx: int = get_item_node.args[1]
                        output_node = output_args[idx]
                        get_item_node._rename(output_node.name)
                        get_item_node.meta = output_node.meta
                        pass

                elif isinstance(output_args, torch.fx.Node):
                    call_func_node = graph.create_node(
                        "call_function",
                        wrap_with_autocast,
                        (autocast_node_args, get_attr_node, *node.args),
                        {},
                        output_args.name,
                    )
                    call_func_node.meta = output_args.meta
                    node_replace_(node, call_func_node, delete_old=True)
                else:
                    raise NotImplementedError(
                        f"repalce_autocast_with_hop_pass doesnt' support output type {type(output_args)}"
                    )
            else:
                # TODO (shangdiy): remove this line, since the export graph can be non-functional
                node.graph.erase_node(node)
        sub_graph.erase_node(exit_autocast_node)
        sub_graph.erase_node(enter_autocast_node)


def _split_autocast(gm: torch.fx.GraphModule) -> torch.fx.GraphModule:
    """
    split_autocast creates a new graph module that splits the input graph module into multiple submodules
    based on the `_enter_autocast` and `_exit_autocast` nodes. It doesn't mutate the input graph module.

    Nodes between the **outer-most** `_enter_autocast` and `_exit_autocast(_enter_autocast)` are splitted
    into a submodule. Nested autocast regions are not splitted.
    `_enter_autocast` and `_exit_autocast(_enter_autocast)` nodes are in the submodule as well.
    """
    enter_autocast_node_stack: List[torch.fx.Node] = []
    first_node_after_outer_most_exit: bool = False

    def node_call_back(node: torch.fx.Node):
        nonlocal enter_autocast_node_stack, first_node_after_outer_most_exit
        if first_node_after_outer_most_exit or (
            len(enter_autocast_node_stack) == 0 and _is_enter_autocast_node(node)
        ):
            assert len(enter_autocast_node_stack) == 0
            first_node_after_outer_most_exit = False
            if _is_enter_autocast_node(node):
                enter_autocast_node_stack.append(node)
            return True
        if _is_exit_autocast_node(node):
            assert len(enter_autocast_node_stack) > 0
            last_enter_autocast_node = enter_autocast_node_stack.pop()
            assert node.args[0] == last_enter_autocast_node
            if len(enter_autocast_node_stack) == 0:
                # next node should be in the next submodule since
                # autocast block ends
                first_node_after_outer_most_exit = True
        return False

    return sequential_split(gm, node_call_back)


def _sequential_split_and_maybe_inline_subgraphs(
    gm: torch.fx.GraphModule, graph_signature
):
    """
    Helper function for replace_autocast_with_hop_pass().
    Split the graph module into multiple subgraphs based on the autocast nodes.
    For each subgraph, decides whether to construct a HOO subgraph, or inline the calls
    back into the parent graph module.
    Nodes between `_enter_autocast` and `_exit_autocast(_enter_autocast)` are considered
    as a subgraph.
    """
    need_replacing = any(_is_autocast_node(node) for node in gm.graph.nodes)
    if not need_replacing:
        return gm, graph_signature

    # split_autocast returns a new graph module that could have different output
    # args names. We need to fix the graph signature.
    new_gm = _split_autocast(gm)

    # TODO (shangdiy): can merge the block below with replace_set_grad_with_hop_pass.
    replace_ctx = contextlib.nullcontext()
    new_signature = None
    if graph_signature is not None:
        new_signature = copy.deepcopy(graph_signature)
        new_gm_out_node = next(reversed(new_gm.graph.find_nodes(op="output")))
        assert new_gm_out_node.op == "output" and len(new_gm_out_node.args[0]) == len(
            new_signature.output_specs
        )
        for arg_node, out_spec in zip(
            new_gm_out_node.args[0], new_signature.output_specs
        ):
            if arg_node is None:
                assert out_spec.arg.value is None
            elif out_spec.arg.name != arg_node.name:
                out_spec.arg.name = arg_node.name

        replace_ctx = new_gm._set_replace_hook(new_signature.get_replace_hook())  # type: ignore[assignment]

    with replace_ctx:

        def _maybe_inline_or_replace_with_hop(node: torch.fx.Node):
            if _is_autocast_sub_mod(node):
                _replace_with_hop(node)
            else:
                assert node.op == "call_module"
                assert isinstance(node.target, str)
                node_inline_(node)

        nodes_map(
            list(new_gm.graph.nodes),
            lambda node: (
                _maybe_inline_or_replace_with_hop(node)
                if node.op == "call_module"
                else node
            ),
        )
    new_gm.recompile()
    return new_gm, new_signature


# TODO (shangdiy): can merge the block below with replace_set_grad_with_hop_pass.
def replace_autocast_with_hop_pass(gm: torch.fx.GraphModule, graph_signature):
    new_gm, new_signature = _sequential_split_and_maybe_inline_subgraphs(
        gm, graph_signature
    )
    # recursively call
    for node in new_gm.graph.nodes:
        if node.op == "get_attr":
            subgm = getattr(new_gm, node.target)
            if not isinstance(subgm, torch.fx.GraphModule):
                continue
            new_subgm, _ = replace_autocast_with_hop_pass(subgm, None)
            setattr(new_gm, node.target, new_subgm)

    new_gm.recompile()
    new_gm.graph.lint()
    return new_gm, new_signature
