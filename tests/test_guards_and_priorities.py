"""Tests for the per-transition `guard` and `priority` callables.

`guard` is engine-level semantics: an (ExecutableGraph) -> bool enabling condition
checked during enabled-transition discovery alongside token availability, in every
execution mode (sequential and both concurrent runner loops). `priority` is
selection policy: an (ExecutableGraph) -> float hint the default selector uses to
fire the highest-priority enabled transition — unset scores 0.0 and ties fall back
to definition order, so nets without priorities keep the old first-enabled
behaviour.

The legacy `activation_function` conflated both roles and the engine never
consulted it (attaching a "guard" silently did nothing without a custom selector);
it is deprecated in favour of the two explicit fields but still visible to custom
selectors until removed.
"""

import warnings

import pytest

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.runtime import ExecutionMode, RunContext, Runner


def increment(x: int) -> int:
    return x + 1


def _relay_graph(**transition_kwargs):
    """In -(Increment)-> Out with three initial tokens."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode('In', int, [1, 2, 3]),
        ArgumentEdgeToTransition('In', 'Increment', 'x'),
        FunctionTransitionNode('Increment', increment, **transition_kwargs),
        ReturnedEdgeFromTransition('Increment', 'Out'),
        ListPlaceNode('Out', int),
    ])


def _two_lane_graph(first_kwargs=None, second_kwargs=None, a_tokens=(1,), b_tokens=(1,)):
    """Two independent lanes; the 'First' transition is defined before 'Second'."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode('A', int, list(a_tokens)),
        ArgumentEdgeToTransition('A', 'First', 'x'),
        FunctionTransitionNode('First', increment, **(first_kwargs or {})),
        ReturnedEdgeFromTransition('First', 'A Out'),
        ListPlaceNode('A Out', int),
        ListPlaceNode('B', int, list(b_tokens)),
        ArgumentEdgeToTransition('B', 'Second', 'x'),
        FunctionTransitionNode('Second', increment, **(second_kwargs or {})),
        ReturnedEdgeFromTransition('Second', 'B Out'),
        ListPlaceNode('B Out', int),
    ])


class TestGuard:
    """A failing guard means "not enabled", indistinguishable from missing tokens."""

    @pytest.mark.asyncio
    async def test_failing_guard_disables_the_transition(self):
        graph = _relay_graph(guard=lambda graph: False)
        graph, fired = await ExecutableGraphOperations.execute_graph(graph, max_transitions=None)
        assert fired == 0
        assert graph.place_named('In').tokens == [1, 2, 3]
        assert graph.place_named('Out').tokens == []

    @pytest.mark.asyncio
    async def test_guard_reads_the_live_marking(self):
        # Enabled only while more than one token remains: the run ends with the
        # last token still in place, not when the tokens run out.
        graph = _relay_graph(guard=lambda graph: len(graph.place_named('In').tokens) > 1)
        graph, fired = await ExecutableGraphOperations.execute_graph(graph, max_transitions=None)
        assert fired == 2
        assert len(graph.place_named('In').tokens) == 1
        assert len(graph.place_named('Out').tokens) == 2

    @pytest.mark.asyncio
    async def test_guard_applies_in_the_concurrent_runner(self):
        # The concurrent runner has its own enabled-discovery sweep; a guarded
        # transition must not be launched there either.
        graph = _relay_graph(guard=lambda graph: False)
        ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)
        await Runner.run_to_completion(ctx)
        assert graph.place_named('In').tokens == [1, 2, 3]
        assert graph.place_named('Out').tokens == []


class TestPriority:
    """The default selector fires the highest-priority enabled transition."""

    @pytest.mark.asyncio
    async def test_higher_priority_fires_before_definition_order(self):
        graph = _two_lane_graph(second_kwargs={'priority': lambda graph: 1.0})
        graph, fired = await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert fired == 1
        assert graph.place_named('B Out').tokens == [2]
        assert graph.place_named('A Out').tokens == []

    @pytest.mark.asyncio
    async def test_without_priorities_definition_order_wins(self):
        graph = _two_lane_graph()
        graph, fired = await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert fired == 1
        assert graph.place_named('A Out').tokens == [2]
        assert graph.place_named('B Out').tokens == []

    @pytest.mark.asyncio
    async def test_priority_reads_the_live_marking(self):
        # Priority = queue length: B (two tokens) beats A (one) for the first
        # firing; after that both lanes tie at one token each and definition
        # order resolves the rest.
        graph = _two_lane_graph(
            first_kwargs={'priority': lambda graph: float(len(graph.place_named('A').tokens))},
            second_kwargs={'priority': lambda graph: float(len(graph.place_named('B').tokens))},
            b_tokens=(1, 2),
        )
        graph, fired = await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert fired == 1
        assert len(graph.place_named('B Out').tokens) == 1
        assert graph.place_named('A Out').tokens == []


class TestActivationFunctionDeprecation:
    def test_constructing_with_activation_function_warns(self):
        with pytest.warns(DeprecationWarning, match="activation_function is deprecated"):
            FunctionTransitionNode('T', increment, activation_function=lambda: True)

    def test_guard_and_priority_do_not_warn(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            FunctionTransitionNode(
                'T', increment, guard=lambda graph: True, priority=lambda graph: 1.0
            )
