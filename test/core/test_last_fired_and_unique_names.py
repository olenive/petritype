"""``ExecutableGraph.last_fired`` and the unique-name guarantees it relies on.

``last_fired`` gives callers an authoritative "what just fired" signal without
inferring it from token-count diffs (which is ambiguous for self-loops). It
stores the transition *name*, which is only a sound identifier because names
are required to be unique — so we also pin that the model rejects duplicates.

Sync bodies + ``asyncio.run`` (no pytest-asyncio auto-mode; matches the other
core tests).
"""

import asyncio

import pytest

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)


def _chain_graph(initial):
    """Input -> Inc -> Output."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Input", int, list(initial)),
        ArgumentEdgeToTransition("Input", "Inc", "x"),
        FunctionTransitionNode("Inc", lambda x: x + 1),
        ReturnedEdgeFromTransition("Inc", "Output"),
        ListPlaceNode("Output", int),
    ])


def _self_loop_graph():
    """Loop -> SelfLoop -> Loop: a fire that leaves token counts unchanged."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Loop", int, [0]),
        ArgumentEdgeToTransition("Loop", "SelfLoop", "x"),
        FunctionTransitionNode("SelfLoop", lambda x: x + 1),
        ReturnedEdgeFromTransition("SelfLoop", "Loop"),
    ])


# -- last_fired --

def test_last_fired_defaults_to_none():
    assert _chain_graph([]).last_fired is None


def test_last_fired_records_the_fired_transition():
    graph = _chain_graph([1])
    graph, fired = asyncio.run(
        ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
    )
    assert fired == 1
    assert graph.last_fired == "Inc"


def test_last_fired_is_set_for_self_loops():
    """The whole point: a self-loop (unchanged token counts) is still
    attributed correctly."""
    graph = _self_loop_graph()
    graph, _ = asyncio.run(
        ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
    )
    assert graph.last_fired == "SelfLoop"
    assert graph.place_named("Loop").tokens == [1]


def test_last_fired_resets_to_none_when_a_call_fires_nothing():
    graph = _chain_graph([1])
    graph, _ = asyncio.run(ExecutableGraphOperations.execute_graph(graph, max_transitions=1))
    assert graph.last_fired == "Inc"
    # Input is now empty → next call fires nothing → last_fired reflects THIS call.
    graph, fired = asyncio.run(ExecutableGraphOperations.execute_graph(graph, max_transitions=1))
    assert fired == 0
    assert graph.last_fired is None


def test_last_fired_holds_the_most_recent_of_a_multi_fire_call():
    graph = _chain_graph([1, 2, 3])
    graph, fired = asyncio.run(
        ExecutableGraphOperations.execute_graph(graph, max_transitions=3)
    )
    assert fired == 3
    assert graph.last_fired == "Inc"  # same transition fired thrice


# -- fired_counts (cumulative, never trimmed) --

def test_fired_counts_defaults_to_empty():
    assert _chain_graph([]).fired_counts == {}


def test_fired_counts_accumulates_across_calls():
    """Unlike the capped transition_history, fired_counts is the authoritative
    "how many times has X fired" — it must survive history trimming and span
    multiple execute_graph calls."""
    graph = _chain_graph([1, 2, 3])
    # Default history length is 1, so transition_history can hold only the last
    # fire — fired_counts must still report the true total.
    graph, _ = asyncio.run(ExecutableGraphOperations.execute_graph(graph, max_transitions=2))
    assert graph.fired_counts == {"Inc": 2}
    assert len(graph.transition_history) == 1  # capped — can't be the counter

    graph, _ = asyncio.run(ExecutableGraphOperations.execute_graph(graph, max_transitions=1))
    assert graph.fired_counts == {"Inc": 3}  # cumulative, not reset per call


def test_fired_counts_keys_each_distinct_transition():
    """Two transitions in sequence: A -> Mid -> B -> Out."""
    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("A", int, [1]),
        ArgumentEdgeToTransition("A", "First", "x"),
        FunctionTransitionNode("First", lambda x: x + 1),
        ReturnedEdgeFromTransition("First", "Mid"),
        ListPlaceNode("Mid", int),
        ArgumentEdgeToTransition("Mid", "Second", "x"),
        FunctionTransitionNode("Second", lambda x: x + 1),
        ReturnedEdgeFromTransition("Second", "Out"),
        ListPlaceNode("Out", int),
    ])
    graph, fired = asyncio.run(
        ExecutableGraphOperations.execute_graph(graph, max_transitions=10)
    )
    assert fired == 2
    assert graph.fired_counts == {"First": 1, "Second": 1}


def test_fired_counts_are_independent_per_graph_instance():
    """Guards against a shared mutable default leaking counts across graphs."""
    g1 = _chain_graph([1])
    g2 = _chain_graph([1])
    g1, _ = asyncio.run(ExecutableGraphOperations.execute_graph(g1, max_transitions=1))
    assert g1.fired_counts == {"Inc": 1}
    assert g2.fired_counts == {}  # untouched


# -- unique-name enforcement (relied on by last_fired / edges / find-by-name) --

def test_duplicate_transition_names_rejected():
    with pytest.raises(ValueError, match="Transition names must be unique"):
        ExecutableGraphOperations.construct_graph([
            ListPlaceNode("A", int, [1]),
            ListPlaceNode("B", int),
            ListPlaceNode("C", int),
            ArgumentEdgeToTransition("A", "T", "x"),
            FunctionTransitionNode("T", lambda x: x),
            ReturnedEdgeFromTransition("T", "B"),
            ArgumentEdgeToTransition("B", "T", "x"),  # second transition, same name
            FunctionTransitionNode("T", lambda x: x),
            ReturnedEdgeFromTransition("T", "C"),
        ])


def test_duplicate_place_names_rejected():
    with pytest.raises(ValueError, match="Place names must be unique"):
        ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Dup", int, [1]),
            ListPlaceNode("Dup", int),  # duplicate place name
            ArgumentEdgeToTransition("Dup", "T", "x"),
            FunctionTransitionNode("T", lambda x: x),
        ])


def test_unique_names_accepted():
    graph = _chain_graph([1])
    assert {t.name for t in graph.transitions} == {"Inc"}
    assert {p.name for p in graph.places} == {"Input", "Output"}
