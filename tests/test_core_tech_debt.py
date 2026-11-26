"""Tests for core technical debt improvements: public API, PetritypeError, caches, and helpers."""

import asyncio
import copy
import pytest

import petritype
from petritype import (
    ArgumentEdgeToTransition,
    ClosedNetError,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    HeldByBodyError,
    LaunchNotificationError,
    ListPlaceNode,
    PetritypeError,
    ReturnedEdgeFromTransition,
    Runner,
    SubnetHeldError,
    TooManyFiringsError,
    TransitionFailedError,
    petri_net,
)
from petritype.core.executable_graph_components import ClosedNets


def test_top_level_public_api_exports():
    for name in petritype.__all__:
        assert hasattr(petritype, name), f"petritype.__all__ lists missing symbol {name!r}"
    assert petritype.ExecutableGraph is ExecutableGraph
    assert petritype.Runner is Runner
    assert petritype.petri_net is petri_net


def test_petritype_error_hierarchy_preserves_stdlib_bases():
    assert issubclass(ClosedNetError, PetritypeError)
    assert issubclass(ClosedNetError, ValueError)
    assert issubclass(HeldByBodyError, PetritypeError)
    assert issubclass(HeldByBodyError, ValueError)
    assert issubclass(TransitionFailedError, PetritypeError)
    assert issubclass(TransitionFailedError, RuntimeError)
    assert issubclass(LaunchNotificationError, PetritypeError)
    assert issubclass(LaunchNotificationError, RuntimeError)
    assert issubclass(SubnetHeldError, PetritypeError)
    assert issubclass(SubnetHeldError, RuntimeError)
    assert issubclass(TooManyFiringsError, PetritypeError)
    assert issubclass(TooManyFiringsError, RuntimeError)


def test_graph_lookup_and_maps_caches_do_not_leak_across_copies():
    def inc(x: int) -> int:
        return x + 1

    g1 = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("A", int, tokens=[1]),
            ListPlaceNode("B", int),
            FunctionTransitionNode("Inc", inc),
            ArgumentEdgeToTransition("A", "Inc", "x"),
            ReturnedEdgeFromTransition("Inc", "B"),
        ]
    )
    assert g1.place_named("A") is g1.places[0]
    assert g1.transition_named("Inc") is g1.transitions[0]
    maps1 = ExecutableGraphOperations.maps(g1)
    assert ExecutableGraphOperations.maps(g1) is maps1

    g2 = copy.deepcopy(g1)
    assert g2.place_named("A") is g2.places[0]
    assert g2.place_named("A") is not g1.place_named("A")
    assert g2.transition_named("Inc") is g2.transitions[0]
    assert g2.transition_named("Inc") is not g1.transition_named("Inc")
    maps2 = ExecutableGraphOperations.maps(g2)
    assert maps2[0]["A"] is g2.places[0]
    assert maps2[0]["A"] is not maps1[0]["A"]


def test_a_move_of_its_own_checks_transition_selector_by_keyword_and_position():
    def inc(x: int) -> int:
        return x + 1

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("A", int, tokens=[1], port=True),
            ListPlaceNode("B", int, port=True),
            FunctionTransitionNode("Inc", inc),
            ArgumentEdgeToTransition("A", "Inc", "x"),
            ReturnedEdgeFromTransition("Inc", "B"),
        ]
    )
    places, incoming, outgoing, reads = ExecutableGraphOperations.maps(graph)
    hold = ClosedNets.hold(graph)
    try:
        # Keyword argument
        with pytest.raises(HeldByBodyError):
            asyncio.run(
                ExecutableGraphOperations.fire_one_transition(
                    graph, places, incoming, outgoing,
                    transition_selector=lambda g, fs: fs[0] if fs else None,
                )
            )
        # Positional argument (index 4: executable_graph, place_nodes, incoming, outgoing, ..., wait: transition_selector)
        with pytest.raises(HeldByBodyError):
            asyncio.run(
                ExecutableGraphOperations.execute_graph(
                    graph, 1, None, None, False, 1, 1, 0,
                    lambda g, fs: fs[0] if fs else None,
                )
            )
    finally:
        ClosedNets.unhold(hold)


def test_place_history_length_zero_skips_input_place_history():
    def inc(x: int) -> int:
        return x + 1

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("A", int, tokens=[1, 2]),
            ListPlaceNode("B", int),
            FunctionTransitionNode("Inc", inc),
            ArgumentEdgeToTransition("A", "Inc", "x"),
            ReturnedEdgeFromTransition("Inc", "B"),
        ]
    )
    asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1, place_history_length=0))
    assert graph.input_place_history == []
    assert graph.output_place_history == []
    assert graph.place_named("B").tokens == [2]

    asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1, place_history_length=1))
    assert len(graph.input_place_history) == 1
    assert len(graph.output_place_history) == 1
