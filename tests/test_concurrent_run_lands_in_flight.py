"""A concurrent run that ends on an error lands every firing in flight before the
error goes on (since 2026-11-28): a guard, a priority or a selector that raises in
the middle of a sweep, the fuse, or a failed firing, whose peers did nothing
wrong. The bodies in flight finish, for the net's ``cancellation_timeout_seconds``
at most, their outputs are deposited and their events recorded, and then the
error is raised. Until then they were cancelled and their tokens lost, and so were
the outputs of the peers that finished beside a failed firing. A cancellation from
outside and a stop still cancel the bodies in flight
(``tests/test_cancellation_timeout.py``, ``tests/test_runtime_cancellation.py``)."""

import asyncio
import time

import pytest

from petritype.core.events import Failed, Fired
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    TransitionFailedError,
)
from petritype.runtime import ExecutionMode, RunContext, Runner, TooManyFiringsError


async def slow(a: int) -> str:
    await asyncio.sleep(0.05)
    return f"slow {a}"


def slow_on_a_thread(a: int) -> str:
    time.sleep(0.05)
    return f"slow {a}"


async def fails_slowly(a: int) -> str:
    await asyncio.sleep(0.05)
    raise RuntimeError("the slow body fails")


def other(b: int) -> str:
    return f"other {b}"


def raises_the_second_time(kind: str):
    """A guard or a priority that answers once and raises when asked again, which a
    sweep does once it has launched ``Slow``."""
    asked = []

    def question(graph):
        asked.append(graph)
        if len(asked) > 1:
            raise RuntimeError(f"the {kind} raises")
        return True if kind == "guard" else 0.0

    return question


def two_lanes(what: str, slow_body=slow, **settings) -> ExecutableGraph:
    """``Slow`` takes from ``A`` and ``Other`` from ``B``, each into ``Done``; ``Slow``
    is declared first, so it launches first, and then ``what`` raises."""
    other_settings = {"guard": raises_the_second_time("guard")} if what == "guard" else {}
    if what == "priority":
        other_settings = {"priority": raises_the_second_time("priority")}
    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("A", int, [1]), ListPlaceNode("B", int, [2]), ListPlaceNode("Done", str),
        FunctionTransitionNode("Slow", slow_body, execution="thread" if slow_body is slow_on_a_thread else "inline"),
        FunctionTransitionNode("Other", other, **other_settings),
        ArgumentEdgeToTransition("A", "Slow", "a"), ArgumentEdgeToTransition("B", "Other", "b"),
        ReturnedEdgeFromTransition("Slow", "Done"), ReturnedEdgeFromTransition("Other", "Done"),
    ], open=True, record_events=True, **settings)
    if what == "selector":
        chosen = []

        def selector(graph, offered):
            chosen.append(offered)
            if len(chosen) > 1:
                raise RuntimeError("the selector raises")
            return offered[0]

        graph.transition_selector = selector
    return graph


async def run(ctx: RunContext, indefinitely: bool, to_completion: bool = False) -> None:
    if indefinitely:
        ctx.stop = asyncio.Event()
        await Runner.run_indefinitely(ctx, tick=0.001)
    elif to_completion:
        await Runner.run_to_completion(ctx)
    else:
        await Runner.run(ctx)


async def boom(b: int) -> str:
    raise RuntimeError("Boom fails")


async def quick(a: int) -> str:
    return f"quick {a}"


def a_failure_beside(*peers, **settings) -> ExecutableGraph:
    """``Boom``, declared and so launched first, takes the 2 of ``B`` and fails at once;
    each peer, a ``(name, body)`` pair, takes the 1 of ``In <name>`` into ``Done``."""
    parts: list = [
        ListPlaceNode("B", int, [2]), ListPlaceNode("Done", str),
        FunctionTransitionNode("Boom", boom),
        ArgumentEdgeToTransition("B", "Boom", "b"), ReturnedEdgeFromTransition("Boom", "Done"),
    ]
    for name, body in peers:
        parts += [
            ListPlaceNode(f"In {name}", int, [1]),
            FunctionTransitionNode(name, body, execution="thread" if body is slow_on_a_thread else "inline"),
            ArgumentEdgeToTransition(f"In {name}", name, "a"),
            ReturnedEdgeFromTransition(name, "Done"),
        ]
    return ExecutableGraphOperations.construct_graph(parts, record_events=True, **settings)


def firings(graph: ExecutableGraph) -> list[tuple[str, str]]:
    return [
        (type(event).__name__, event.transition)
        for event in graph.drain_events() if isinstance(event, (Fired, Failed))
    ]


@pytest.mark.parametrize("what", ["guard", "priority", "selector"])
@pytest.mark.parametrize("body", [slow, slow_on_a_thread], ids=["async", "thread"])
@pytest.mark.parametrize("indefinitely", [False, True], ids=["run", "run_indefinitely"])
def test_a_question_that_raises_mid_sweep_lands_the_firing_in_flight_first(what, body, indefinitely):
    async def go():
        graph = two_lanes(what, body)
        with pytest.raises(RuntimeError, match=f"the {what} raises"):
            await run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT), indefinitely)
        assert graph.place("Done").tokens == ["slow 1"], "Slow landed"
        assert graph.place("A").tokens == [] and graph.place("B").tokens == [2]
        assert [event.transition for event in graph.drain_events() if isinstance(event, Fired)] == ["Slow"]
        assert graph.in_flight == set() and graph.fired_counts == {"Slow": 1}
    asyncio.run(go())


def test_the_fuse_lands_the_firing_in_flight_first():
    async def go():
        graph = two_lanes("nothing")
        ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, error_after_n_firings=1)
        with pytest.raises(TooManyFiringsError) as blown:
            await Runner.run(ctx)
        assert graph.place("Done").tokens == ["slow 1"] and graph.place("B").tokens == [2]
        assert blown.value.summary.fired == {"Slow": 1}
    asyncio.run(go())


def test_a_firing_that_fails_as_it_lands_is_told_in_a_note_on_the_error():
    async def go():
        graph = two_lanes("guard", fails_slowly)
        with pytest.raises(RuntimeError, match="the guard raises") as raised:
            await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
        [note] = raised.value.__notes__
        assert "'Slow'" in note and "failed as it landed" in note
        assert graph.place("Done").tokens == [] and graph.in_flight == set()
    asyncio.run(go())


def test_a_body_still_running_at_the_timeout_is_cancelled_and_named():
    async def go():
        graph = two_lanes("guard", cancellation_timeout_seconds=0.01)
        with pytest.raises(RuntimeError, match="the guard raises") as raised:
            await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
        [note] = raised.value.__notes__
        assert "Slow" in note and "cancelled, not landed" in note
        assert graph.place("Done").tokens == [] and graph.in_flight == set()
    asyncio.run(go())


ENTRY_POINTS = {
    "run": {"indefinitely": False},
    "run_to_completion": {"indefinitely": False, "to_completion": True},
    "run_indefinitely": {"indefinitely": True},
}


@pytest.mark.parametrize("how", ENTRY_POINTS)
@pytest.mark.parametrize("body", [slow, slow_on_a_thread], ids=["async", "thread"])
def test_a_failed_firing_lets_its_peer_land_first(how, body):
    """Until 2026-11-28 ``Slow`` was cancelled and its token lost. ``Boom``'s own
    token is not restored, as before, since ``restore_tokens_on_failure`` is off."""
    async def go():
        graph = a_failure_beside(("Slow", body))
        with pytest.raises(TransitionFailedError) as raised:
            await run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT), **ENTRY_POINTS[how])
        assert raised.value.transition_name == "Boom" and not getattr(raised.value, "__notes__", [])
        assert raised.value.consumed == {"b": 2} and not raised.value.restored
        assert graph.place("Done").tokens == ["slow 1"], "Slow landed"
        assert graph.place("B").tokens == [] and graph.place("In Slow").tokens == []
        assert firings(graph) == [("Failed", "Boom"), ("Fired", "Slow")]
        assert graph.in_flight == set() and graph.fired_counts == {"Slow": 1}
    asyncio.run(go())


@pytest.mark.parametrize("indefinitely", [False, True], ids=["run", "run_indefinitely"])
def test_a_peer_that_finished_beside_the_failing_body_lands(indefinitely):
    """``Quick`` finishes in the same batch as ``Boom``, which was launched before it
    and fails; until 2026-11-28 its outputs were dropped with the batch."""
    async def go():
        graph = a_failure_beside(("Quick", quick))
        with pytest.raises(TransitionFailedError, match="'Boom'"):
            await run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT), indefinitely)
        assert graph.place("Done").tokens == ["quick 1"]
        assert firings(graph) == [("Failed", "Boom"), ("Fired", "Quick")]
    asyncio.run(go())


@pytest.mark.parametrize("restore", [False, True], ids=["lost", "restored"])
def test_a_peer_that_fails_as_it_lands_is_told_in_a_note_on_the_first_failure(restore):
    """The run raises the failure it met first, ``Boom``'s, and tells ``Slow``'s, met
    as ``Slow`` landed, in a note; ``restore_tokens_on_failure`` applies to both."""
    async def go():
        graph = a_failure_beside(("Slow", fails_slowly), restore_tokens_on_failure=restore)
        with pytest.raises(TransitionFailedError) as raised:
            await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
        assert raised.value.transition_name == "Boom" and raised.value.restored is restore
        [note] = raised.value.__notes__
        assert "'Slow'" in note and "failed as it landed" in note
        assert graph.place("B").tokens == ([2] if restore else [])
        assert graph.place("In Slow").tokens == ([1] if restore else [])
        assert graph.place("Done").tokens == [] and graph.in_flight == set()
        events = [event for event in graph.drain_events() if isinstance(event, Failed)]
        assert [(event.transition, event.restored) for event in events] == [("Boom", restore), ("Slow", restore)]
    asyncio.run(go())


def test_a_cancellation_while_a_failure_waits_for_its_peer_cancels_the_peer():
    """A cancellation from outside cancels every body in flight, as before, also
    while a failed firing's peers are landing."""
    async def go():
        started = asyncio.Event()
        cancelled = []

        async def long(a: int) -> str:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.append(a)
                raise
            return f"long {a}"

        graph = a_failure_beside(("Long", long))
        running = asyncio.ensure_future(Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.sleep(0.02)  # Boom has failed, and the run waits for Long to land
        assert firings(graph) == [("Failed", "Boom")] and not running.done()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert cancelled == [1]
        assert graph.place("Done").tokens == [] and graph.in_flight == set()
    asyncio.run(go())
