"""Asking whether a transition can fire costs no time in proportion to the tokens of
a place that one of its edges takes or reads every token of (a list-mode take, a
drain, a survey, a read-all, a list-mode snapshot or mutate edge). Stage 1's plan
knows how many tokens such an edge gets without listing them
(``ExecutableGraphOperations.plan_stage_1``), and a firing lists them as it reads
them; the answer and the plan come from that one planner."""

import time

import pytest

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    DrainEdge,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReadAllEdge,
    SnapshotEdge,
    SurveyEdge,
)


class Unlisted(list):
    """A place's tokens that refuse to be listed or indexed; their count is known."""

    def __iter__(self):
        raise AssertionError("the tokens were listed")

    def __getitem__(self, index):
        raise AssertionError("a token was read")


def count(xs: list[int]) -> int:
    return len(xs)


EDGES = {
    "list-mode take": ArgumentEdgeToTransition,
    "drain": DrainEdge,
    "survey": SurveyEdge,
    "read-all": ReadAllEdge,
    "list-mode snapshot": SnapshotEdge,
    "list-mode mutate": MutateEdge,
}


@pytest.mark.parametrize("edge", EDGES.values(), ids=EDGES.keys())
def test_asking_whether_an_edge_on_every_token_can_fire_lists_none_of_them(edge):
    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, [1]),
        FunctionTransitionNode("Count", count),
        edge("In", "Count", "xs"),
    ])
    graph.place_named("In").tokens = Unlisted(range(200_000))
    places, incoming, _, reads = ExecutableGraphOperations.maps(graph)
    transition = graph.transitions[0]
    started = time.perf_counter()
    for _ in range(50):
        assert ExecutableGraphCheck.transition_is_fireable(transition, graph, incoming, places, reads)
    elapsed = time.perf_counter() - started
    # About 0.2 ms; listing every index, as the planner once did, took about 250 ms.
    assert elapsed < 0.025, f"50 checks took {elapsed * 1000:.1f} ms"
