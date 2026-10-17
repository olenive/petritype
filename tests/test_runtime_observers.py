"""Tests for observing firings across execution modes (SUGGESTIONS.md item 1).

``fired_since`` diffs two snapshots of ``graph.fired_counts`` and is the
lossless way for an observer to answer "what fired since I last looked" — it
works identically under SEQUENTIAL and CONCURRENT. ``graph.last_fired`` is a
convenience that the concurrent deposit loops also set, but it names only one
completion per ``asyncio.wait`` batch, and the sequential loops' final
quiescence probe resets it to None — so nothing here asserts on its post-run
value, only on what observers see at notification time.
"""

import asyncio
import threading

import pytest
from pydantic import BaseModel

from petritype.core.events import Failed, Fired
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    LaunchNotificationError,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    TransitionFailedError,
)
from petritype.runtime import ExecutionMode, Fire, RunContext, Runner, fired_since


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
    """Two independent lanes, both fireable from the start. The concurrent sweep
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


def _single_transition_graph(body, execution='inline'):
    """One transition ``Work`` from ``In`` (holding one token) to ``Out``."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode('In', int, [1]),
        ArgumentEdgeToTransition('In', 'Work', 'x'),
        FunctionTransitionNode('Work', body, execution=execution),
        ReturnedEdgeFromTransition('Work', 'Out'),
        ListPlaceNode('Out', int),
    ])


class TestSequentialInFlight:
    """Sequential firing names the running transition in ``graph.in_flight`` for
    the length of its body, and leaves the set empty once the body returns or
    raises, so a heartbeat reading the graph can say what is working."""

    @pytest.mark.asyncio
    async def test_body_sees_itself_in_flight_and_step_clears_it(self):
        seen = []
        holder = {}

        def body(x: int) -> int:
            seen.append(set(holder['graph'].in_flight))
            return x

        graph = _single_transition_graph(body)
        holder['graph'] = graph
        assert await Runner.step(RunContext(graph=graph)) == 1
        assert seen == [{'Work'}]
        assert graph.in_flight == set()

    @pytest.mark.asyncio
    async def test_thread_body_is_visible_from_the_event_loop(self):
        """A slow ``execution="thread"`` body leaves the loop free, so a watcher
        task on it plays the heartbeat: it must read the running transition's
        name while the body runs."""
        started = threading.Event()
        release = threading.Event()
        holder = {}
        seen_inside = []

        def body(x: int) -> int:
            seen_inside.append(set(holder['graph'].in_flight))
            started.set()
            release.wait(timeout=5)
            return x

        graph = _single_transition_graph(body, execution='thread')
        holder['graph'] = graph
        step = asyncio.ensure_future(Runner.step(RunContext(graph=graph)))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        observed_while_running = set(graph.in_flight)
        release.set()
        assert await step == 1
        assert seen_inside == [{'Work'}]
        assert observed_while_running == {'Work'}
        assert graph.in_flight == set()

    @pytest.mark.asyncio
    async def test_raising_body_clears_in_flight(self):
        def body(x: int) -> int:
            raise ValueError('boom')

        graph = _single_transition_graph(body)
        with pytest.raises(TransitionFailedError):
            await Runner.step(RunContext(graph=graph))
        assert graph.in_flight == set()

    @pytest.mark.asyncio
    async def test_each_write_assigns_a_fresh_set(self):
        """Readers on another thread rely on the reference being replaced, never
        mutated: a set captured during the body keeps its contents afterwards."""
        holder = {}
        captured = []

        def body(x: int) -> int:
            captured.append(holder['graph'].in_flight)
            return x

        graph = _single_transition_graph(body)
        holder['graph'] = graph
        await Runner.run_to_completion(RunContext(graph=graph))
        assert captured[0] == {'Work'}
        assert graph.in_flight == set()
        assert graph.in_flight is not captured[0]


def _in_flight_recorder():
    """An observer that appends ``set(graph.in_flight)`` on each call, and keeps a
    snapshot of the graph at each launch call (one where the set is non-empty).

    Returns ``(observe, calls, launches)``.
    """
    calls, launches = [], []

    def observe(graph):
        calls.append(set(graph.in_flight))
        if graph.in_flight:
            launches.append({
                'tokens': {place.name: list(place.tokens) for place in graph.places},
                'step_count': graph.step_count,
                'last_fired': graph.last_fired,
                'fired_counts': dict(graph.fired_counts),
            })

    return observe, calls, launches


class _Box(BaseModel):
    """A token holding one inner net, ``counter``."""

    inner: ExecutableGraph

    def __petritype_nets__(self) -> dict[str, ExecutableGraph]:
        return {'counter': self.inner}


class TestSequentialLaunchNotification:
    """Sequential firing notifies observers once more per firing, as the
    transition goes in flight and before its body runs, so an observer on the
    event loop sees the set non-empty as the concurrent loop already lets it."""

    @pytest.mark.asyncio
    async def test_run_notifies_at_marking_launch_and_completion(self):
        graph = _single_transition_graph(increment)
        observe, calls, launches = _in_flight_recorder()
        await Runner.run(RunContext(graph=graph, observers=(observe,)))
        assert calls == [set(), {'Work'}, set()]
        assert launches == [{
            'tokens': {'In': [], 'Out': []},
            'step_count': 0,
            'last_fired': None,
            'fired_counts': {},
        }]

    @pytest.mark.asyncio
    async def test_step_notifies_at_launch_and_completion(self):
        graph = _single_transition_graph(increment)
        observe, calls, _launches = _in_flight_recorder()
        assert await Runner.step(RunContext(graph=graph, observers=(observe,))) == 1
        assert calls == [{'Work'}, set()]

    @pytest.mark.asyncio
    async def test_thread_body_agrees_with_the_launch_call(self):
        holder = {}
        seen_inside = []

        def body(x: int) -> int:
            seen_inside.append(set(holder['graph'].in_flight))
            return x

        graph = _single_transition_graph(body, execution='thread')
        holder['graph'] = graph
        observe, calls, _launches = _in_flight_recorder()
        assert await Runner.step(RunContext(graph=graph, observers=(observe,))) == 1
        assert calls == [{'Work'}, set()]
        assert seen_inside == [{'Work'}]

    @pytest.mark.asyncio
    async def test_raising_body_is_seen_at_launch_and_still_raises(self):
        def body(x: int) -> int:
            raise ValueError('boom')

        graph = _single_transition_graph(body)
        observe, calls, _launches = _in_flight_recorder()
        with pytest.raises(TransitionFailedError):
            await Runner.run(RunContext(graph=graph, observers=(observe,)))
        assert calls == [set(), {'Work'}]
        assert graph.in_flight == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('restore', [True, False])
    async def test_observer_raising_at_launch_fails_the_firing(self, restore):
        ran = []

        def body(x: int) -> int:
            ran.append(x)
            return x

        graph = _single_transition_graph(body)
        graph.restore_tokens_on_failure = restore
        graph.record_events = True
        boom = ValueError('boom')
        raised = []

        def observe(graph):
            if graph.in_flight and not raised:
                raised.append(True)
                raise boom

        with pytest.raises(TransitionFailedError) as caught:
            await Runner.run(RunContext(graph=graph, observers=(observe,)))
        launch_error = caught.value.__cause__
        assert isinstance(launch_error, LaunchNotificationError)
        assert launch_error.transition_name == 'Work'
        assert launch_error.observer_error is boom
        assert launch_error.__cause__ is boom
        assert str(launch_error) == (
            "an observer raised at the launch notification of 'Work', before its body ran: "
            "ValueError('boom')"
        )
        assert ran == [], 'the body never ran'
        assert caught.value.restored is restore
        assert graph.place_named('In').tokens == ([1] if restore else [])
        assert graph.place_named('Out').tokens == []
        assert graph.in_flight == set()
        events = graph.drain_events()
        failed = [event for event in events if isinstance(event, Failed)]
        assert len(failed) == 1
        assert failed[0].restored is restore
        assert 'launch notification' in failed[0].error
        assert not any(isinstance(event, Fired) for event in events)

    @pytest.mark.asyncio
    async def test_bookkeeping_is_unchanged_by_the_launch_call(self):
        """The counts, the summary and the log are what a run gave before the
        launch notification existed; the observer saw one launch per firing."""
        graph = _chain_graph(3)
        graph.record_events = True
        observe, calls, launches = _in_flight_recorder()
        summary = await Runner.run(RunContext(graph=graph, observers=(observe,)))
        assert summary.n_fired == 3
        assert summary.fired == {'T1': 1, 'T2': 1, 'T3': 1}
        assert graph.step_count == 3
        assert graph.fired_counts == {'T1': 1, 'T2': 1, 'T3': 1}
        events = graph.drain_events()
        assert all(isinstance(event, Fired) for event in events)
        assert [(event.transition, event.consumed, event.produced) for event in events] == [
            ('T1', (('P0', 1),), (('P1', 2),)),
            ('T2', (('P1', 2),), (('P2', 3),)),
            ('T3', (('P2', 3),), (('P3', 4),)),
        ]
        assert [call for call in calls if call] == [{'T1'}, {'T2'}, {'T3'}]
        assert [launch['step_count'] for launch in launches] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_an_inner_firing_has_no_launch_call(self):
        """An inner net's launch notifies nothing: the outer observer sees the
        marking and the completion, and the outer set stays empty throughout."""
        holder = {}
        outer_in_flight = []

        def body(x: int) -> int:
            outer_in_flight.append(set(holder['outer'].in_flight))
            return x

        outer = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Shelf', _Box, [_Box(inner=_single_transition_graph(body))], nested=True),
        ])
        holder['outer'] = outer
        observe, calls, _launches = _in_flight_recorder()
        summary = await Runner.run(RunContext(graph=outer, observers=(observe,)), stop_after_n_firings=1)
        assert calls == [set(), set()]
        assert outer_in_flight == [set()]
        assert summary.fired == {'Shelf/#1/counter/Work': 1}
        assert outer.fired_counts == {'Shelf/#1/counter/Work': 1}

    @pytest.mark.asyncio
    async def test_a_fire_command_has_no_launch_call(self):
        """``Fire`` fires inside the synchronous drain, possibly on a helper
        thread's loop, so observers hear of it once, after the drain."""
        graph = _single_transition_graph(increment)
        observe, calls, _launches = _in_flight_recorder()
        ctx = RunContext(graph=graph, observers=(observe,), inbox=asyncio.Queue())
        ctx.inbox.put_nowait(Fire('Work'))
        assert await Runner.step(ctx) == 0, 'the Fire took the one token; the step found nothing'
        assert calls == [set()]
        assert ctx.n_fired == 1
        assert graph.fired_counts == {'Work': 1}
