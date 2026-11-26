"""Tests for core engine and runtime correctness bug fixes."""

import asyncio
import warnings
import pytest

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ClosedNets,
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    HeldByBodyError,
    ListPlaceNode,
    NestedNets,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    TransitionFailedError,
)
from petritype.runtime import (
    ExecutionMode,
    RunContext,
    Runner,
    SetNetParam,
    by_control,
)


def _fanout_graph(allow_token_copying: bool) -> ExecutableGraph:
    def dup(x: list[int]) -> list[int]:
        return x

    return ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("In", list[int], tokens=[[1, 2]]),
            ListPlaceNode("OutA", list[int]),
            ListPlaceNode("OutB", list[int]),
            FunctionTransitionNode("Dup", dup),
            ArgumentEdgeToTransition("In", "Dup", "x"),
            ReturnedEdgeFromTransition("Dup", "OutA"),
            ReturnedEdgeFromTransition("Dup", "OutB"),
        ],
        allow_token_copying=allow_token_copying,
    )


def test_concurrent_mode_honours_allow_token_copying():
    graph = _fanout_graph(allow_token_copying=True)
    ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)
    summary = asyncio.run(Runner.run(ctx))
    assert summary.quiesced is True
    assert summary.n_fired == 1
    out_a = graph.place_named("OutA").tokens
    out_b = graph.place_named("OutB").tokens
    assert out_a == [[1, 2]]
    assert out_b == [[1, 2]]
    assert out_a[0] is not out_b[0]


def test_concurrent_mode_still_rejects_duplicate_token_when_copying_disabled():
    graph = _fanout_graph(allow_token_copying=False)
    ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)
    with pytest.raises(TransitionFailedError):
        asyncio.run(Runner.run_to_completion(ctx))


def test_multiple_consuming_edges_same_place_require_enough_tokens():
    def add(a: int, b: int) -> int:
        return a + b

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("P", int, tokens=[10]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Add", add),
            ArgumentEdgeToTransition("P", "Add", "a"),
            ArgumentEdgeToTransition("P", "Add", "b"),
            ReturnedEdgeFromTransition("Add", "Out"),
        ]
    )
    places, incoming, outgoing, reads = ExecutableGraphOperations.maps(graph)
    transition = graph.transitions[0]

    assert not ExecutableGraphCheck.sufficient_tokens_are_available(
        transition, incoming, places, reads, graph
    )
    # Direct stage_1 call must raise without popping or losing the single token.
    with pytest.raises(ValueError):
        ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
            transition, incoming, places, transition_names_to_read_edges=reads, net_params=graph.params
        )
    assert graph.place_named("P").tokens == [10]

    # With a second token, it becomes fireable and consumes both in FIFO order.
    graph.place_named("P").tokens.append(25)
    assert ExecutableGraphCheck.sufficient_tokens_are_available(
        transition, incoming, places, reads, graph
    )
    _, fired = asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    assert fired == 1
    assert graph.place_named("P").tokens == []
    assert graph.place_named("Out").tokens == [35]


def test_multiple_consuming_edges_same_place_restore_fifo_order_on_failure():
    def boom(a: int, b: int) -> int:
        raise RuntimeError(f"boom {a} {b}")

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("P", int, tokens=[1, 2, 3]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Boom", boom),
            ArgumentEdgeToTransition("P", "Boom", "a"),
            ArgumentEdgeToTransition("P", "Boom", "b"),
            ReturnedEdgeFromTransition("Boom", "Out"),
        ],
        restore_tokens_on_failure=True,
    )
    with pytest.raises(TransitionFailedError):
        asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    assert graph.place_named("P").tokens == [1, 2, 3]


def test_consuming_and_snapshot_edge_same_place_atomic():
    def pair(a: int, peek: int) -> int:
        return a * 10 + peek

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("P", int, tokens=[4]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Pair", pair),
            ArgumentEdgeToTransition("P", "Pair", "a"),
            SnapshotEdge("P", "Pair", "peek"),
            ReturnedEdgeFromTransition("Pair", "Out"),
        ]
    )
    places, incoming, _, reads = ExecutableGraphOperations.maps(graph)
    assert not ExecutableGraphCheck.sufficient_tokens_are_available(
        graph.transitions[0], incoming, places, reads, graph
    )
    with pytest.raises(ValueError):
        ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
            graph.transitions[0], incoming, places, transition_names_to_read_edges=reads, net_params=graph.params
        )
    assert graph.place_named("P").tokens == [4]

    graph.place_named("P").tokens.append(7)
    _, fired = asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    assert fired == 1
    assert graph.place_named("P").tokens == [7]
    assert graph.place_named("Out").tokens == [47]


def test_drain_plus_single_edge_same_place_is_not_fireable():
    def combine(xs: list[int], a: int) -> int:
        return sum(xs) + a

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("P", int, tokens=[1, 2]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Combine", combine),
            DrainEdge("P", "Combine", "xs"),
            ArgumentEdgeToTransition("P", "Combine", "a"),
            ReturnedEdgeFromTransition("Combine", "Out"),
        ]
    )
    places, incoming, _, reads = ExecutableGraphOperations.maps(graph)
    assert not ExecutableGraphCheck.sufficient_tokens_are_available(
        graph.transitions[0], incoming, places, reads, graph
    )
    with pytest.raises(ValueError):
        ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
            graph.transitions[0], incoming, places, transition_names_to_read_edges=reads, net_params=graph.params
        )
    assert graph.place_named("P").tokens == [1, 2]


@pytest.mark.parametrize("bad_count", [-1, -100, True, False, 1.5, "1"])
def test_nested_nets_take_rejects_invalid_count(bad_count):
    graph = ExecutableGraphOperations.construct_graph(
        [ListPlaceNode("P", int, tokens=[1, 2, 3])]
    )
    with pytest.raises(ValueError):
        NestedNets.take(graph, "P", count=bad_count)
    assert graph.place_named("P").tokens == [1, 2, 3]


def test_nested_nets_take_zero_returns_empty():
    graph = ExecutableGraphOperations.construct_graph(
        [ListPlaceNode("P", int, tokens=[1, 2])]
    )
    assert NestedNets.take(graph, "P", count=0) == []
    assert graph.place_named("P").tokens == [1, 2]


def test_set_net_param_by_control_succeeds_on_held_net():
    graph = ExecutableGraphOperations.construct_graph(
        [ListPlaceNode("In", int, port=True)],
        params={"limit": 10},
    )
    hold = ClosedNets.hold(graph)
    try:
        # Regular SetNetParam is refused under strict mode while held.
        ctx_strict = RunContext(graph=graph, inbox=asyncio.Queue(), strict=True)
        ctx_strict.inbox.put_nowait(SetNetParam("limit", 20))
        with pytest.raises(HeldByBodyError):
            Runner.drain_inbox(ctx_strict)
        assert graph.params["limit"] == 10

        # A control-marked SetNetParam is inside the net and must succeed.
        ctx_ctrl = RunContext(graph=graph, inbox=asyncio.Queue(), strict=True)
        ctx_ctrl.inbox.put_nowait(by_control(SetNetParam("limit", 25)))
        assert Runner.drain_inbox(ctx_ctrl) == 1
        assert graph.params["limit"] == 25
    finally:
        ClosedNets.unhold(hold)


def test_place_history_json_roundtrip_emits_no_pydantic_warning():
    def inc(x: int) -> int:
        return x + 1

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("A", int, tokens=[1]),
            ListPlaceNode("B", int),
            FunctionTransitionNode("Inc", inc),
            ArgumentEdgeToTransition("A", "Inc", "x"),
            ReturnedEdgeFromTransition("Inc", "B"),
        ]
    )
    asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dumped = graph.model_dump()
    pydantic_warnings = [w for w in caught if "PydanticSerializationUnexpectedValue" in str(w.message)]
    assert pydantic_warnings == []
    assert "input_place_history" in dumped
    assert "output_place_history" in dumped


def test_positional_args_base_model_rejects_extra_and_duplicate_args():
    # Too many positional args
    with pytest.raises(TypeError, match="at most"):
        ArgumentEdgeToTransition("P", "T", "x", "extra_arg")

    # Duplicate positional and keyword arg
    with pytest.raises(TypeError, match="multiple values"):
        ListPlaceNode("P", int, name="Other")


def test_transition_selector_returning_non_fireable_transition_raises():
    def f(x: int) -> int:
        return x

    t1 = FunctionTransitionNode("T1", f)
    t2 = FunctionTransitionNode("T2", f)
    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("A", int, tokens=[1]),
            ListPlaceNode("B", int),
            ListPlaceNode("Out", int),
            t1,
            t2,
            ArgumentEdgeToTransition("A", "T1", "x"),
            ReturnedEdgeFromTransition("T1", "Out"),
            ArgumentEdgeToTransition("B", "T2", "x"),
            ReturnedEdgeFromTransition("T2", "Out"),
        ]
    )
    with pytest.raises(ValueError, match="not among the fireable transitions"):
        asyncio.run(
            ExecutableGraphOperations.execute_graph(
                graph,
                stop_after_n_firings=1,
                transition_selector=lambda g, fireable: t2,
            )
        )
    assert graph.place_named("A").tokens == [1]


def test_closed_nets_detects_in_place_mutation_of_mutable_params():
    inner = ExecutableGraphOperations.construct_graph(
        [ListPlaceNode("Port", int, port=True)],
        params={"tags": ["a"]},
    )

    def mutate_param(net: ExecutableGraph) -> ExecutableGraph:
        net.params["tags"].append("sneaky")
        return net

    outer = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("Hold", ExecutableGraph, tokens=[inner], nested=True, stepping="passive"),
            ListPlaceNode("Done", ExecutableGraph, nested=True, stepping="passive"),
            FunctionTransitionNode("Sneak", mutate_param),
            ArgumentEdgeToTransition("Hold", "Sneak", "net"),
            ReturnedEdgeFromTransition("Sneak", "Done"),
        ]
    )
    with pytest.raises(TransitionFailedError) as exc_info:
        asyncio.run(ExecutableGraphOperations.execute_graph(outer, stop_after_n_firings=1))
    assert isinstance(exc_info.value.__cause__, HeldByBodyError)
