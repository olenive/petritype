"""Several input edges of one transition on one place: two consuming edges, a
consuming edge and a snapshot, or a drain and a single take. The transition is
fireable only when the place holds a token for each edge; stage 1 refuses
without taking anything when it does not, and takes them in FIFO order when it
does; a failed firing that restores puts them back in that order.
"""

import asyncio

import pytest

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    DrainEdge,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    TransitionFailedError,
)


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


def test_take_plus_list_mode_take_same_place_needs_a_token_for_each():
    """A take and a list-mode take (``list[int]``) on one place: the take gets the
    front token and the list the rest, which may not be empty, so one token is not
    enough; with three, the list gets the two behind the front one."""

    def split(a: int, rest: list[int]) -> int:
        return a * 100 + sum(rest)

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("P", int, tokens=[1]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Split", split),
            ArgumentEdgeToTransition("P", "Split", "a"),
            ArgumentEdgeToTransition("P", "Split", "rest"),
            ReturnedEdgeFromTransition("Split", "Out"),
        ]
    )
    places, incoming, _, reads = ExecutableGraphOperations.maps(graph)
    transition = graph.transitions[0]
    assert not ExecutableGraphCheck.sufficient_tokens_are_available(transition, incoming, places, reads, graph)

    graph.place_named("P").tokens.extend([2, 3])
    assert ExecutableGraphCheck.sufficient_tokens_are_available(transition, incoming, places, reads, graph)
    plan = ExecutableGraphOperations.plan_stage_1(transition, incoming, places, reads, graph.params)
    assert plan is not None
    assert [(tuple(item.indices), len(item.indices)) for item in plan.incoming] == [((0,), 1), ((1, 2), 2)]
    _, fired = asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    assert fired == 1
    assert graph.place_named("P").tokens == []
    assert graph.place_named("Out").tokens == [105]


def pair_of(a: int) -> tuple[int, int]:
    return a, a + 1


def same(a: int) -> int:
    return a


@pytest.mark.parametrize("edges, copying", [
    ((ReturnedEdgeFromTransition("T", "Out"), ReturnedEdgeFromTransition("T", "Out")), False),
    ((ReturnedEdgeFromTransition("T", "Out"), ReturnedEdgeFromTransition("T", "Out")), True),
    ((ReturnedEdgeFromTransition("T", "Out", 0), ReturnedEdgeFromTransition("T", "Out", 1)), False),
], ids=["plain", "plain with token copying", "indexed"])
def test_two_output_edges_from_one_transition_to_one_place_are_refused_at_construction(edges, copying):
    """Since 2026-11-28. A firing routes its result by place, so until then the net
    built and its firing failed, or, with token copying on or with return indices,
    one token landed where the drawing shows two."""
    body = pair_of if edges[0].return_index is not None else same
    with pytest.raises(ValueError, match="transition 'T' has two output edges to 'Out'"):
        ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [1]), ListPlaceNode("Out", int),
            FunctionTransitionNode("T", body), ArgumentEdgeToTransition("In", "T", "a"), *edges,
        ], allow_token_copying=copying)


def test_output_edges_from_one_transition_to_two_places_and_from_two_transitions_to_one_place_stay_allowed():
    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, [1]), ListPlaceNode("Also In", int, [5]),
        ListPlaceNode("Out", int), ListPlaceNode("Also", int),
        FunctionTransitionNode("T", pair_of), FunctionTransitionNode("U", same),
        ArgumentEdgeToTransition("In", "T", "a"), ArgumentEdgeToTransition("Also In", "U", "a"),
        ReturnedEdgeFromTransition("T", "Out", 0), ReturnedEdgeFromTransition("T", "Also", 1),
        ReturnedEdgeFromTransition("U", "Out"),
    ])
    asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=None))
    assert graph.place_named("Out").tokens == [1, 5] and graph.place_named("Also").tokens == [2]
