"""Tests for the monotonic ``step_count`` on ``ExecutableGraph``.

``step_count`` is the authoritative "which step are we on" counter. The
downstream use case is HTTP-level idempotency: callers send the step they
think the engine is at, and the worker can detect retries vs new steps
without keeping a separate cache.

Sync test bodies + ``asyncio.run`` because ``execute_graph`` is async but
this repo doesn't currently have ``pytest-asyncio`` auto-mode configured.
Keeps these tests independent of that config.
"""

import asyncio

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)


def _make_increment_graph(initial_tokens: list[int]):
    """A minimal net with one ``Input → Inc → Output`` chain.

    Suitable for any test that just needs "fire N times" — each fire moves
    one int from Input through Inc to Output. Shared with the existing
    transition-selection tests but inlined here so this file stands alone.
    """
    def increment(x: int) -> int:
        return x + 1

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Input", int, list(initial_tokens)),
        ArgumentEdgeToTransition("Input", "Inc", "x"),
        FunctionTransitionNode("Inc", increment),
        ReturnedEdgeFromTransition("Inc", "Output"),
        ListPlaceNode("Output", int),
    ])


class TestStepCountInitial:
    """A freshly-constructed graph starts at step_count == 0."""

    def test_default_zero(self):
        graph = _make_increment_graph([])
        assert graph.step_count == 0

    def test_default_zero_with_tokens(self):
        """Having initial tokens doesn't count — only fires bump step_count."""
        graph = _make_increment_graph([1, 2, 3])
        assert graph.step_count == 0


class TestStepCountIncrement:
    """Each successful fire bumps step_count by exactly one."""

    def test_one_fire_increments_by_one(self):
        graph = _make_increment_graph([1])
        updated, fired = asyncio.run(
            ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        )
        assert fired == 1
        assert updated.step_count == 1

    def test_n_fires_increments_by_n(self):
        """``max_transitions=3`` with 3 input tokens → 3 fires → step_count == 3."""
        graph = _make_increment_graph([1, 2, 3])
        updated, fired = asyncio.run(
            ExecutableGraphOperations.execute_graph(graph, max_transitions=3)
        )
        assert fired == 3
        assert updated.step_count == 3

    def test_step_count_persists_across_execute_calls(self):
        """Calling ``execute_graph`` twice in a row should accumulate
        step_count — this is the property that lets HTTP callers use it as
        a stable sequence number across requests."""
        graph = _make_increment_graph([1, 2, 3, 4])
        graph, _ = asyncio.run(
            ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        )
        assert graph.step_count == 1
        graph, _ = asyncio.run(
            ExecutableGraphOperations.execute_graph(graph, max_transitions=2)
        )
        assert graph.step_count == 3


class TestStepCountUnchangedOnNoFire:
    """No enabled transition → no fire → step_count stays put."""

    def test_no_input_tokens_no_increment(self):
        """An empty Input place means no transition is enabled. The engine
        should return without firing and without bumping the counter."""
        graph = _make_increment_graph([])
        updated, fired = asyncio.run(
            ExecutableGraphOperations.execute_graph(graph, max_transitions=5)
        )
        assert fired == 0
        assert updated.step_count == 0

    def test_runs_until_dry_then_stops(self):
        """Three tokens, asking for 10 transitions: should fire 3 then stop.
        step_count tracks actual fires, not the request."""
        graph = _make_increment_graph([1, 2, 3])
        updated, fired = asyncio.run(
            ExecutableGraphOperations.execute_graph(graph, max_transitions=10)
        )
        assert fired == 3
        assert updated.step_count == 3


class TestStepCountIndependentOfHistory:
    """``step_count`` is independent of ``transition_history`` length so it
    remains usable as a stable counter even when history is capped."""

    def test_step_count_grows_while_history_stays_capped(self):
        """``transition_history_length`` defaults to 1, so after many fires
        the history holds only the latest transition — but ``step_count``
        keeps growing."""
        graph = _make_increment_graph([1, 2, 3, 4, 5])
        updated, fired = asyncio.run(
            ExecutableGraphOperations.execute_graph(
                graph, max_transitions=5, transition_history_length=1,
            )
        )
        assert fired == 5
        assert updated.step_count == 5
        # History was capped at 1 entry — but the counter sees all 5 fires.
        assert len(updated.transition_history) == 1


class TestStepCountSerializable:
    """``step_count`` is a plain int field — must survive pydantic
    serialization round-trip so it can be persisted / sent over the wire.

    Mirrors how Petritype-server's worker captures and restores engine
    state. If this regresses, every consumer relying on the counter being
    visible in serialized form would silently break."""

    def test_round_trip_through_json(self):
        graph = _make_increment_graph([1, 2])
        graph, _ = asyncio.run(
            ExecutableGraphOperations.execute_graph(graph, max_transitions=2)
        )
        assert graph.step_count == 2
        # Use mode="python" because places contain non-JSON values; we're
        # not testing JSON encoding here, just the field's round-trip.
        dumped = graph.model_dump()
        assert dumped["step_count"] == 2
