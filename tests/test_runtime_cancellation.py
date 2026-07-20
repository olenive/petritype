"""Tests for Runner cancellation semantics (SUGGESTIONS.md item 5).

Cancelling a concurrent run must not orphan in-flight transition bodies: inputs
are consumed at launch (stage 1) and outputs are deposited by the loop, so an
abandoned task would mean silently lost tokens on top of the cancellation.
Both concurrent loops cancel their in-flight tasks and clear
``graph.in_flight`` on the way out — including when a failed firing (not a
caller cancellation) is what interrupts the run.
"""

import asyncio

import pytest

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    TransitionFailedError,
)
from petritype.runtime import ExecutionMode, RunContext, Runner


def _slow_graph(started: asyncio.Event, body_state: dict):
    """In -(Slow)-> Out where Slow blocks until cancelled, recording what happened."""

    async def slow(x: int) -> int:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            body_state['cancelled'] = True
            raise
        return x

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode('In', int, [1]),
        ArgumentEdgeToTransition('In', 'Slow', 'x'),
        FunctionTransitionNode('Slow', slow),
        ReturnedEdgeFromTransition('Slow', 'Out'),
        ListPlaceNode('Out', int),
    ])


async def _settle():
    """Give the event loop a few passes so requested cancellations are delivered."""
    for _ in range(5):
        await asyncio.sleep(0)


class TestConcurrentCancellation:
    @pytest.mark.asyncio
    async def test_run_to_completion_cancels_in_flight_tasks(self):
        started = asyncio.Event()
        body_state = {'cancelled': False}
        graph = _slow_graph(started, body_state)
        ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)
        run = asyncio.ensure_future(Runner.run_to_completion(ctx))
        await started.wait()  # the body is mid-flight
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        await _settle()
        assert body_state['cancelled'] is True
        assert graph.in_flight == set()
        # The consumed token is gone — the documented partial marking, not restored.
        assert graph.place_named('In').tokens == []
        assert graph.place_named('Out').tokens == []

    @pytest.mark.asyncio
    async def test_run_indefinitely_cancels_in_flight_tasks(self):
        started = asyncio.Event()
        body_state = {'cancelled': False}
        graph = _slow_graph(started, body_state)
        ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)
        run = asyncio.ensure_future(Runner.run_indefinitely(ctx, tick=0.01))
        await started.wait()
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        await _settle()
        assert body_state['cancelled'] is True
        assert graph.in_flight == set()

    @pytest.mark.asyncio
    async def test_failed_firing_does_not_orphan_peers(self):
        # When one lane's body raises, TransitionFailedError propagates out of the
        # run — the other lane's still-running body must be cancelled with it.
        started = asyncio.Event()
        body_state = {'cancelled': False}

        async def slow(x: int) -> int:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                body_state['cancelled'] = True
                raise
            return x

        async def boom(x: int) -> int:
            await started.wait()  # be sure the slow lane is airborne first
            raise RuntimeError('boom')

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('A', int, [1]),
            ArgumentEdgeToTransition('A', 'Slow', 'x'),
            FunctionTransitionNode('Slow', slow),
            ReturnedEdgeFromTransition('Slow', 'A Out'),
            ListPlaceNode('A Out', int),
            ListPlaceNode('B', int, [1]),
            ArgumentEdgeToTransition('B', 'Boom', 'x'),
            FunctionTransitionNode('Boom', boom),
            ReturnedEdgeFromTransition('Boom', 'B Out'),
            ListPlaceNode('B Out', int),
        ])
        ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)
        with pytest.raises(TransitionFailedError):
            await Runner.run_to_completion(ctx)
        await _settle()
        assert body_state['cancelled'] is True
        assert graph.in_flight == set()
