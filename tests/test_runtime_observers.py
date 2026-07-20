"""Tests for observing firings across execution modes (SUGGESTIONS.md item 1).

``fired_since`` diffs two snapshots of ``graph.fired_counts`` and is the
lossless way for an observer to answer "what fired since I last looked" — it
works identically under SEQUENTIAL and CONCURRENT. ``graph.last_fired`` is a
convenience that the concurrent deposit loops also set, but it names only one
completion per ``asyncio.wait`` batch, and the sequential loops' final
quiescence probe resets it to None — so nothing here asserts on its post-run
value, only on what observers see at notification time.
"""

import pytest

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.runtime import ExecutionMode, RunContext, Runner, fired_since


def increment(x: int) -> int:
    return x + 1


def _chain_graph(length: int, tokens=(0,)):
    """A linear chain P0 -(T1)-> P1 -(T2)-> ... -(Tn)-> Pn with tokens in P0."""
    parts = [ListPlaceNode('P0', int, list(tokens))]
    for i in range(1, length + 1):
        parts += [
            ArgumentEdgeToTransition(f'P{i - 1}', f'T{i}', 'x'),
            FunctionTransitionNode(f'T{i}', increment),
            ReturnedEdgeFromTransition(f'T{i}', f'P{i}'),
            ListPlaceNode(f'P{i}', int),
        ]
    return ExecutableGraphOperations.construct_graph(parts)


def _two_lane_graph():
    """Two independent lanes, both enabled from the start. The concurrent sweep
    launches both in one pass and their (synchronous) bodies complete in the
    same ``asyncio.wait`` batch — the case where ``last_fired`` alone would
    lose a firing."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode('A', int, [1]),
        ArgumentEdgeToTransition('A', 'Left', 'x'),
        FunctionTransitionNode('Left', increment),
        ReturnedEdgeFromTransition('Left', 'A Out'),
        ListPlaceNode('A Out', int),
        ListPlaceNode('B', int, [1]),
        ArgumentEdgeToTransition('B', 'Right', 'x'),
        FunctionTransitionNode('Right', increment),
        ReturnedEdgeFromTransition('Right', 'B Out'),
        ListPlaceNode('B Out', int),
    ])


def _accumulating_observer():
    """An observer that diffs ``fired_counts`` between notifications.

    Returns ``(observe, state)`` where ``state['totals']`` accumulates every
    firing and ``state['last_fired_at_deposit']`` records what ``last_fired``
    showed at each notification whose diff was non-empty.
    """
    state = {'previous': {}, 'totals': {}, 'last_fired_at_deposit': []}

    def observe(graph):
        current = dict(graph.fired_counts)
        delta = fired_since(state['previous'], current)
        state['previous'] = current
        for name, count in delta.items():
            state['totals'][name] = state['totals'].get(name, 0) + count
        if delta:
            state['last_fired_at_deposit'].append(graph.last_fired)

    return observe, state


class TestFiredSince:
    def test_empty_previous_returns_current(self):
        assert fired_since({}, {'A': 2, 'B': 1}) == {'A': 2, 'B': 1}

    def test_zero_deltas_are_omitted(self):
        assert fired_since({'A': 2, 'B': 1}, {'A': 2, 'B': 3}) == {'B': 2}

    def test_identical_snapshots_yield_empty(self):
        assert fired_since({'A': 4}, {'A': 4}) == {}

    def test_multi_count_delta(self):
        assert fired_since({'A': 1}, {'A': 4}) == {'A': 3}


class TestObserverSeesEveryFiring:
    @pytest.mark.asyncio
    async def test_concurrent_chain_reports_every_transition(self):
        graph = _chain_graph(12)
        observe, state = _accumulating_observer()
        ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, observers=(observe,))
        await Runner.run_to_completion(ctx)
        assert state['totals'] == {f'T{i}': 1 for i in range(1, 13)}

    @pytest.mark.asyncio
    async def test_last_fired_is_set_at_concurrent_deposits(self):
        # Regression: the concurrent loops used to never set last_fired, so
        # every deposit notification observed None.
        graph = _chain_graph(12)
        observe, state = _accumulating_observer()
        ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, observers=(observe,))
        await Runner.run_to_completion(ctx)
        assert state['last_fired_at_deposit']
        assert all(name is not None for name in state['last_fired_at_deposit'])

    @pytest.mark.asyncio
    async def test_same_batch_completions_are_not_lost(self):
        graph = _two_lane_graph()
        observe, state = _accumulating_observer()
        ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, observers=(observe,))
        await Runner.run_to_completion(ctx)
        assert state['totals'] == {'Left': 1, 'Right': 1}

    @pytest.mark.asyncio
    async def test_sequential_and_concurrent_report_identical_multisets(self):
        totals_by_mode = {}
        for mode in (ExecutionMode.SEQUENTIAL, ExecutionMode.CONCURRENT):
            graph = _chain_graph(5, tokens=(0, 10))
            observe, state = _accumulating_observer()
            ctx = RunContext(graph=graph, mode=mode, observers=(observe,))
            await Runner.run_to_completion(ctx)
            totals_by_mode[mode] = state['totals']
        assert totals_by_mode[ExecutionMode.SEQUENTIAL] == {f'T{i}': 2 for i in range(1, 6)}
        assert totals_by_mode[ExecutionMode.SEQUENTIAL] == totals_by_mode[ExecutionMode.CONCURRENT]
