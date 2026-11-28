"""Tests for per-token RW locking, selector ordering, ContextVar-scoped ClosedNets,
and cancellation cleanup in ExecutionMode.CONCURRENT, and for ``Fire`` commands held
to the same locks."""

import asyncio
import threading
from dataclasses import dataclass

import pytest

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ClosedNetError,
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    MutateWhereEdge,
    NestedNets,
    ReadAllEdge,
    ReadEdge,
    ReturnedEdgeFromTransition,
    SurveyEdge,
    TakeWhereEdge,
    TransitionFailedError,
)
from petritype.core.keys import Filter
from petritype.runtime import ExecutionMode, Fire, RunContext, Runner


@dataclass
class Box:
    kind: str
    value: int


@pytest.mark.asyncio
async def test_mutate_and_mutate_on_same_token_are_serialized():
    active = 0
    max_active = 0
    order: list[str] = []

    async def m1(box: Box, _g: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        order.append("m1_start")
        await asyncio.sleep(0.02)
        box.value += 10
        order.append("m1_end")
        active -= 1
        return box.value

    async def m2(box: Box, _g: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        order.append("m2_start")
        await asyncio.sleep(0.02)
        box.value += 100
        order.append("m2_end")
        active -= 1
        return box.value

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("Shared", Box, tokens=[Box("x", 1)]),
            ListPlaceNode("G1", int, tokens=[1]),
            ListPlaceNode("G2", int, tokens=[1]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("M1", m1),
            FunctionTransitionNode("M2", m2),
            MutateEdge("Shared", "M1", "box"),
            ArgumentEdgeToTransition("G1", "M1", "_g"),
            ReturnedEdgeFromTransition("M1", "Out"),
            MutateEdge("Shared", "M2", "box"),
            ArgumentEdgeToTransition("G2", "M2", "_g"),
            ReturnedEdgeFromTransition("M2", "Out"),
        ]
    )
    summary = await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
    assert summary.quiesced is True
    assert summary.n_fired == 2
    assert max_active == 1
    assert order == ["m1_start", "m1_end", "m2_start", "m2_end"]
    assert graph.place_named("Shared").tokens[0].value == 111


@pytest.mark.asyncio
async def test_mutate_and_read_on_same_token_are_serialized():
    active = 0
    max_active = 0
    seen_by_reader: list[int] = []

    async def writer(box: Box, _g: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        box.value = 42
        active -= 1
        return 1

    async def reader(box: Box, _g: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        seen_by_reader.append(box.value)
        active -= 1
        return 2

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("Shared", Box, tokens=[Box("x", 0)]),
            ListPlaceNode("GW", int, tokens=[1]),
            ListPlaceNode("GR", int, tokens=[1]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Writer", writer, priority=lambda g: 10.0),
            FunctionTransitionNode("Reader", reader, priority=lambda g: 1.0),
            MutateEdge("Shared", "Writer", "box"),
            ArgumentEdgeToTransition("GW", "Writer", "_g"),
            ReturnedEdgeFromTransition("Writer", "Out"),
            ReadEdge("Shared", "Reader", "box"),
            ArgumentEdgeToTransition("GR", "Reader", "_g"),
            ReturnedEdgeFromTransition("Reader", "Out"),
        ]
    )
    summary = await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
    assert summary.quiesced is True
    assert max_active == 1
    assert seen_by_reader == [42]


@pytest.mark.asyncio
async def test_read_and_read_on_same_token_run_in_parallel():
    r1_entered = asyncio.Event()
    r2_entered = asyncio.Event()

    async def r1(box: Box, _g: int) -> int:
        r1_entered.set()
        await asyncio.wait_for(r2_entered.wait(), timeout=1.0)
        return box.value

    async def r2(box: Box, _g: int) -> int:
        r2_entered.set()
        await asyncio.wait_for(r1_entered.wait(), timeout=1.0)
        return box.value

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("Shared", Box, tokens=[Box("x", 7)]),
            ListPlaceNode("G1", int, tokens=[1]),
            ListPlaceNode("G2", int, tokens=[1]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("R1", r1),
            FunctionTransitionNode("R2", r2),
            ReadEdge("Shared", "R1", "box"),
            ArgumentEdgeToTransition("G1", "R1", "_g"),
            ReturnedEdgeFromTransition("R1", "Out"),
            ReadEdge("Shared", "R2", "box"),
            ArgumentEdgeToTransition("G2", "R2", "_g"),
            ReturnedEdgeFromTransition("R2", "Out"),
        ]
    )
    summary = await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
    assert summary.quiesced is True
    assert graph.place_named("Out").tokens == [7, 7]


@pytest.mark.asyncio
async def test_different_tokens_in_same_place_run_in_parallel_via_per_token_locks():
    """Mirrors wildlife.py: MutateWhereEdge on Deer and TakeWhereEdge on Bird in the
    same place hold distinct token locks and execute concurrently."""
    deer_entered = asyncio.Event()
    bird_entered = asyncio.Event()

    async def decline_deer(deer: Box, _g: int) -> int:
        deer_entered.set()
        await asyncio.wait_for(bird_entered.wait(), timeout=1.0)
        deer.value -= 1
        return deer.value

    async def migrate_bird(bird: Box) -> Box:
        bird_entered.set()
        await asyncio.wait_for(deer_entered.wait(), timeout=1.0)
        return bird

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode(
                "Forest",
                Box,
                tokens=[Box("Deer", 10), Box("Bird", 5)],
                keys={"kind": lambda b: b.kind},
            ),
            ListPlaceNode("Tick", int, tokens=[1]),
            ListPlaceNode("Wetland", Box),
            ListPlaceNode("Log", int),
            FunctionTransitionNode("Decline", decline_deer),
            FunctionTransitionNode("Migrate", migrate_bird),
            MutateWhereEdge("Forest", "Decline", "deer", where=Filter.where(kind="Deer")),
            ArgumentEdgeToTransition("Tick", "Decline", "_g"),
            ReturnedEdgeFromTransition("Decline", "Log"),
            TakeWhereEdge("Forest", "Migrate", "bird", where=Filter.where(kind="Bird")),
            ReturnedEdgeFromTransition("Migrate", "Wetland"),
        ]
    )
    summary = await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
    assert summary.quiesced is True
    assert summary.n_fired == 2
    assert graph.place_named("Forest").tokens == [Box("Deer", 9)]
    assert graph.place_named("Wetland").tokens == [Box("Bird", 5)]


@pytest.mark.asyncio
async def test_strict_fifo_head_of_line_waiting_does_not_skip_locked_front_token():
    """Place P has [tok1, tok2]. MutateEdge holds tok1; Consume wants front token of P.
    Consume must wait for tok1 to be released and receive tok1 (not skip ahead to tok2)."""
    mutator_done = False
    consumed_order: list[int] = []

    async def mutate_front(box: Box, _g: int) -> int:
        nonlocal mutator_done
        await asyncio.sleep(0.02)
        box.value = 99
        mutator_done = True
        return 1

    async def consume_front(box: Box) -> int:
        assert mutator_done is True, "Consume launched while Mutate still held front token!"
        consumed_order.append(box.value)
        return box.value

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("P", Box, tokens=[Box("a", 1), Box("b", 2)]),
            ListPlaceNode("Gate", int, tokens=[1]),
            ListPlaceNode("Log", int),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Mutate", mutate_front, priority=lambda g: 10.0),
            FunctionTransitionNode("Consume", consume_front, priority=lambda g: 1.0),
            MutateEdge("P", "Mutate", "box"),
            ArgumentEdgeToTransition("Gate", "Mutate", "_g"),
            ReturnedEdgeFromTransition("Mutate", "Log"),
            ArgumentEdgeToTransition("P", "Consume", "box"),
            ReturnedEdgeFromTransition("Consume", "Out"),
        ]
    )
    summary = await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
    assert summary.quiesced is True
    # First consumed must be mutated tok1 (99), then tok2 (2)
    assert consumed_order == [99, 2]


@pytest.mark.asyncio
async def test_whole_place_drain_waits_for_single_token_mutate_and_read_all_allows_survey():
    mutator_done = False
    drained_values: list[int] = []

    async def mutate_one(box: Box, _g: int) -> int:
        nonlocal mutator_done
        await asyncio.sleep(0.02)
        box.value = 50
        mutator_done = True
        return 1

    async def drain_all(boxes: list[Box], _g: int) -> int:
        assert mutator_done is True, "Drain ran while a token in the place was mutated!"
        drained_values.extend(b.value for b in boxes)
        return len(boxes)

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("P", Box, tokens=[Box("a", 1), Box("b", 2)], keys={"kind": lambda b: b.kind}),
            ListPlaceNode("G1", int, tokens=[1]),
            ListPlaceNode("G2", int, tokens=[1]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Mutate", mutate_one, priority=lambda g: 10.0),
            FunctionTransitionNode("Drain", drain_all, priority=lambda g: 1.0),
            MutateWhereEdge("P", "Mutate", "box", where=Filter.where(kind="b")),
            ArgumentEdgeToTransition("G1", "Mutate", "_g"),
            ReturnedEdgeFromTransition("Mutate", "Out"),
            DrainEdge("P", "Drain", "boxes"),
            ArgumentEdgeToTransition("G2", "Drain", "_g"),
            ReturnedEdgeFromTransition("Drain", "Out"),
        ]
    )
    summary = await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
    assert summary.quiesced is True
    assert drained_values == [1, 50]

    # ReadAllEdge and SurveyEdge both take shared place locks and run in parallel.
    ra_entered = asyncio.Event()
    sv_entered = asyncio.Event()

    async def read_all(boxes: list[Box], _g: int) -> int:
        ra_entered.set()
        await asyncio.wait_for(sv_entered.wait(), timeout=1.0)
        return len(boxes)

    async def survey(boxes: list[Box], _g: int) -> int:
        sv_entered.set()
        await asyncio.wait_for(ra_entered.wait(), timeout=1.0)
        return len(boxes)

    graph2 = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("P", Box, tokens=[Box("a", 1)]),
            ListPlaceNode("G1", int, tokens=[1]),
            ListPlaceNode("G2", int, tokens=[1]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("RA", read_all),
            FunctionTransitionNode("SV", survey),
            ReadAllEdge("P", "RA", "boxes"),
            ArgumentEdgeToTransition("G1", "RA", "_g"),
            ReturnedEdgeFromTransition("RA", "Out"),
            SurveyEdge("P", "SV", "boxes"),
            ArgumentEdgeToTransition("G2", "SV", "_g"),
            ReturnedEdgeFromTransition("SV", "Out"),
        ]
    )
    summary2 = await Runner.run(RunContext(graph=graph2, mode=ExecutionMode.CONCURRENT))
    assert summary2.quiesced is True
    assert graph2.place_named("Out").tokens == [1, 1]


@pytest.mark.asyncio
async def test_atomic_multi_token_acquisition_avoids_deadlock():
    """T1 needs (P1, P2) and T2 needs (P2, P1) exclusively; atomic pre-launch
    acquisition guarantees no deadlock and no concurrent overlap."""
    active = 0
    max_active = 0

    async def work_ab(a: Box, b: Box, _g: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        a.value += 1
        b.value += 1
        active -= 1
        return a.value + b.value

    async def work_ba(b: Box, a: Box, _g: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        b.value += 10
        a.value += 10
        active -= 1
        return a.value + b.value

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("P1", Box, tokens=[Box("p1", 0)]),
            ListPlaceNode("P2", Box, tokens=[Box("p2", 0)]),
            ListPlaceNode("G1", int, tokens=[1]),
            ListPlaceNode("G2", int, tokens=[1]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("AB", work_ab),
            FunctionTransitionNode("BA", work_ba),
            MutateEdge("P1", "AB", "a"),
            MutateEdge("P2", "AB", "b"),
            ArgumentEdgeToTransition("G1", "AB", "_g"),
            ReturnedEdgeFromTransition("AB", "Out"),
            MutateEdge("P2", "BA", "b"),
            MutateEdge("P1", "BA", "a"),
            ArgumentEdgeToTransition("G2", "BA", "_g"),
            ReturnedEdgeFromTransition("BA", "Out"),
        ]
    )
    summary = await asyncio.wait_for(
        Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)), timeout=2.0
    )
    assert summary.quiesced is True
    assert summary.n_fired == 2
    assert max_active == 1
    assert graph.place_named("P1").tokens[0].value == 11
    assert graph.place_named("P2").tokens[0].value == 11


@pytest.mark.asyncio
async def test_concurrent_mode_honours_priority_tie_break_and_custom_selector():
    # 1. Priority: High declared AFTER Low must win the single token.
    graph_prio = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("In", int, tokens=[1]),
            ListPlaceNode("LowOut", int),
            ListPlaceNode("HighOut", int),
            FunctionTransitionNode("Low", lambda x: x, priority=lambda g: 1.0),
            FunctionTransitionNode("High", lambda x: x, priority=lambda g: 10.0),
            ArgumentEdgeToTransition("In", "Low", "x"),
            ReturnedEdgeFromTransition("Low", "LowOut"),
            ArgumentEdgeToTransition("In", "High", "x"),
            ReturnedEdgeFromTransition("High", "HighOut"),
        ]
    )
    await Runner.run(RunContext(graph=graph_prio, mode=ExecutionMode.CONCURRENT))
    assert graph_prio.place_named("HighOut").tokens == [1]
    assert graph_prio.place_named("LowOut").tokens == []

    # 2. tie_break="last_declared": Second wins over First on equal priority.
    graph_last = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("In", int, tokens=[1]),
            ListPlaceNode("Out1", int),
            ListPlaceNode("Out2", int),
            FunctionTransitionNode("First", lambda x: x),
            FunctionTransitionNode("Second", lambda x: x),
            ArgumentEdgeToTransition("In", "First", "x"),
            ReturnedEdgeFromTransition("First", "Out1"),
            ArgumentEdgeToTransition("In", "Second", "x"),
            ReturnedEdgeFromTransition("Second", "Out2"),
        ],
        tie_break="last_declared",
    )
    await Runner.run(RunContext(graph=graph_last, mode=ExecutionMode.CONCURRENT))
    assert graph_last.place_named("Out2").tokens == [1]
    assert graph_last.place_named("Out1").tokens == []

    # 3. Custom transition_selector: always pick "Picked".
    graph_sel = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("In", int, tokens=[1]),
            ListPlaceNode("AOut", int),
            ListPlaceNode("PickedOut", int),
            FunctionTransitionNode("A", lambda x: x),
            FunctionTransitionNode("Picked", lambda x: x),
            ArgumentEdgeToTransition("In", "A", "x"),
            ReturnedEdgeFromTransition("A", "AOut"),
            ArgumentEdgeToTransition("In", "Picked", "x"),
            ReturnedEdgeFromTransition("Picked", "PickedOut"),
        ]
    )
    graph_sel.transition_selector = lambda g, fs: next((t for t in fs if t.name == "Picked"), None)
    await Runner.run(RunContext(graph=graph_sel, mode=ExecutionMode.CONCURRENT))
    assert graph_sel.place_named("PickedOut").tokens == [1]
    assert graph_sel.place_named("AOut").tokens == []

    # 4. Priority also honoured in run_indefinitely (_tick_concurrent).
    graph_tick = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("In", int, tokens=[1]),
            ListPlaceNode("LowOut", int),
            ListPlaceNode("HighOut", int),
            FunctionTransitionNode("Low", lambda x: x, priority=lambda g: 1.0),
            FunctionTransitionNode("High", lambda x: x, priority=lambda g: 10.0),
            ArgumentEdgeToTransition("In", "Low", "x"),
            ReturnedEdgeFromTransition("Low", "LowOut"),
            ArgumentEdgeToTransition("In", "High", "x"),
            ReturnedEdgeFromTransition("High", "HighOut"),
        ]
    )
    stop = asyncio.Event()

    def stopper(g: ExecutableGraph):
        if g.place_named("HighOut").tokens or g.place_named("LowOut").tokens:
            stop.set()

    await Runner.run_indefinitely(
        RunContext(graph=graph_tick, mode=ExecutionMode.CONCURRENT, stop=stop, observers=(stopper,)),
        tick=0.01,
    )
    assert graph_tick.place_named("HighOut").tokens == [1]
    assert graph_tick.place_named("LowOut").tokens == []


@pytest.mark.asyncio
async def test_closed_nets_note_is_scoped_to_offending_concurrent_body():
    """Two concurrent ReadEdge bodies hold the same closed inner net.
    Bad tries NestedNets.put on a private place (catching ClosedNetError);
    Good does nothing illegal. Bad's firing must fail with ClosedNetError,
    while Good's firing completes cleanly without being tainted."""
    both_started = asyncio.Event()
    good_done = asyncio.Event()
    started = 0

    inner = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("Port", int, port=True),
            ListPlaceNode("Private", int),
        ]
    )

    async def good_body(net: ExecutableGraph, _g: int) -> str:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1.0)
        # Wait slightly so Bad's violation happens while Good is still holding `net`.
        await asyncio.sleep(0.02)
        good_done.set()
        return "good_ok"

    async def bad_body(net: ExecutableGraph, _g: int) -> str:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1.0)
        try:
            NestedNets.put(net, "Private", [999])
        except ClosedNetError:
            pass
        # Wait until Good has finished and settled before Bad settles, so Good runs
        # its settle() AFTER Bad triggered ClosedNets.note!
        await asyncio.wait_for(good_done.wait(), timeout=1.0)
        await asyncio.sleep(0.02)
        return "bad_returned"

    outer = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("Nets", ExecutableGraph, tokens=[inner], nested=True, stepping="passive"),
            ListPlaceNode("G1", int, tokens=[1]),
            ListPlaceNode("G2", int, tokens=[1]),
            ListPlaceNode("GoodOut", str),
            ListPlaceNode("BadOut", str),
            FunctionTransitionNode("Good", good_body),
            FunctionTransitionNode("Bad", bad_body),
            ReadEdge("Nets", "Good", "net"),
            ArgumentEdgeToTransition("G1", "Good", "_g"),
            ReturnedEdgeFromTransition("Good", "GoodOut"),
            ReadEdge("Nets", "Bad", "net"),
            ArgumentEdgeToTransition("G2", "Bad", "_g"),
            ReturnedEdgeFromTransition("Bad", "BadOut"),
        ]
    )

    with pytest.raises(TransitionFailedError) as exc_info:
        await Runner.run(RunContext(graph=outer, mode=ExecutionMode.CONCURRENT))
    # Good must have succeeded and deposited its output; only Bad failed!
    assert outer.place_named("GoodOut").tokens == ["good_ok"]
    assert outer.place_named("BadOut").tokens == []
    assert exc_info.value.transition_name == "Bad"
    assert isinstance(exc_info.value.__cause__, ClosedNetError)
    assert not inner.held_by_body


@pytest.mark.asyncio
async def test_cancellation_awaits_async_body_cleanup_before_returning():
    started = asyncio.Event()
    cleaned_up = False

    async def slow(x: int) -> int:
        nonlocal cleaned_up
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaned_up = True
        return x

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("In", int, tokens=[1]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Slow", slow),
            ArgumentEdgeToTransition("In", "Slow", "x"),
            ReturnedEdgeFromTransition("Slow", "Out"),
        ]
    )
    task = asyncio.ensure_future(Runner.run_to_completion(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Cleanup ran BEFORE run_to_completion exited (no extra sleep(0) needed).
    assert cleaned_up is True
    assert graph.in_flight == set()


# --- a body handed a whole place holds what lands there while it runs ---------------------------


def worker_net(work: bool) -> ExecutableGraph:
    """A net with one piece of work to do, or none."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Todo", int, [1] if work else []),
        ListPlaceNode("Done", int),
        FunctionTransitionNode("Do", lambda n: n),
        ArgumentEdgeToTransition("Todo", "Do", "n"),
        ReturnedEdgeFromTransition("Do", "Done"),
    ])


@pytest.mark.asyncio
async def test_a_net_landing_in_a_place_a_body_holds_whole_is_not_stepped_until_the_body_lands():
    """A list-mode mutate edge hands the body the place's own list, so a token landing
    there while the body runs is in the body's hands: its net waits for the body."""
    started, release = asyncio.Event(), asyncio.Event()
    seen: list[tuple[int, int]] = []

    async def tend_all(nets: list[ExecutableGraph], _g: int) -> int:
        started.set()
        await release.wait()
        seen.extend((len(net.place_named("Todo").tokens), net.fired_counts.get("Do", 0)) for net in nets)
        return len(nets)

    async def make(_g: int) -> ExecutableGraph:
        await started.wait()
        return worker_net(work=True)

    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Nets", ExecutableGraph, [worker_net(work=False)], nested=True),
        ListPlaceNode("G1", int, [1]),
        ListPlaceNode("G2", int, [1]),
        ListPlaceNode("Log", int),
        FunctionTransitionNode("TendAll", tend_all, priority=lambda g: 10.0),
        FunctionTransitionNode("Make", make),
        MutateEdge("Nets", "TendAll", "nets"),
        ArgumentEdgeToTransition("G1", "TendAll", "_g"),
        ReturnedEdgeFromTransition("TendAll", "Log"),
        ArgumentEdgeToTransition("G2", "Make", "_g"),
        ReturnedEdgeFromTransition("Make", "Nets"),
    ])
    run = asyncio.ensure_future(Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)))
    await started.wait()
    while len(graph.place_named("Nets").tokens) < 2:  # until Make's net has landed beside the body
        await asyncio.sleep(0)
    for _ in range(20):  # rounds in which the loop could step the new net, were it free to
        await asyncio.sleep(0)
    release.set()
    await run

    assert seen == [(0, 0), (1, 0)], "the body saw the new net with its work still to do"
    new_net = graph.place_named("Nets").tokens[1]
    assert new_net.fired_counts.get("Do", 0) == 1, "and the net did its work once the body had landed"


# --- a Fire command is held to the claims of the firings in flight -----------------------------


class WatchedInbox(asyncio.Queue):
    """An inbox that tells when a drain has emptied it: ``drained`` is set each time
    ``get_nowait`` finds it empty, which ``Runner.drain_inbox`` does as it finishes."""

    def __init__(self):
        super().__init__()
        self.drained = asyncio.Event()

    def get_nowait(self):
        try:
            return super().get_nowait()
        except asyncio.QueueEmpty:
            self.drained.set()
            raise


async def through_the_loops_drain(inbox: WatchedInbox, command) -> None:
    """Put ``command`` on the inbox and wait until the running loop's own drain has
    applied it."""
    inbox.drained.clear()
    inbox.put_nowait(command)
    await asyncio.wait_for(inbox.drained.wait(), timeout=1.0)


def mutator_and_taker(mutate, *more, take_guard=None) -> ExecutableGraph:
    """``Mutate`` (``mutate``, first by priority) works on the one token of ``P`` through a
    mutate edge; ``Take`` (guarded by ``take_guard``) consumes it into ``Gone``. ``more``
    are further nodes and edges."""
    return ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("P", Box, tokens=[Box("a", 1)]),
            ListPlaceNode("G", int, tokens=[1]),
            ListPlaceNode("Log", int),
            ListPlaceNode("Gone", Box),
            FunctionTransitionNode("Mutate", mutate, priority=lambda g: 10.0),
            FunctionTransitionNode("Take", lambda box: box, guard=take_guard),
            MutateEdge("P", "Mutate", "box"),
            ArgumentEdgeToTransition("G", "Mutate", "_g"),
            ReturnedEdgeFromTransition("Mutate", "Log"),
            ArgumentEdgeToTransition("P", "Take", "box"),
            ReturnedEdgeFromTransition("Take", "Gone"),
            *more,
        ]
    )


@pytest.mark.asyncio
async def test_fire_of_a_taker_does_nothing_while_a_mutator_of_its_token_is_in_flight():
    started = asyncio.Event()
    release = asyncio.Event()

    async def mutate(box: Box, _g: int) -> int:
        started.set()
        await release.wait()
        box.value = 99
        return 1

    graph = mutator_and_taker(mutate)
    ctx = RunContext(
        graph=graph, mode=ExecutionMode.CONCURRENT, inbox=asyncio.Queue(), strict=True,
        error_after_n_firings=1,
    )
    # The cap stops the sweep after Mutate, so only the Fire could take the token.
    run = asyncio.ensure_future(Runner.run(ctx, stop_after_n_firings=1))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    ctx.inbox.put_nowait(Fire("Take"))
    # Not a rejection (strict would raise it), and the fuse, spent by Mutate, is not touched.
    assert Runner.drain_inbox(ctx) == 0
    assert graph.place_named("P").tokens == [Box("a", 1)]
    assert graph.place_named("Gone").tokens == []
    assert ctx.n_fired == 1
    assert graph.in_flight == {"Mutate"}

    release.set()
    summary = await asyncio.wait_for(run, timeout=1.0)
    assert summary.fired == {"Mutate": 1}
    assert graph.place_named("P").tokens == [Box("a", 99)]


@pytest.mark.asyncio
async def test_fire_from_the_loops_inbox_waits_for_the_mutator_to_land_then_fires():
    """The reproduction of the gap: a Fire drained by ``run_indefinitely`` itself."""
    started = asyncio.Event()
    release = asyncio.Event()
    landed = asyncio.Event()
    armed: list[bool] = []

    async def mutate(box: Box, _g: int) -> int:
        started.set()
        await release.wait()
        box.value = 99
        return 1

    # Take's guard passes only while a Fire of it is being applied, so the sweep never takes.
    graph = mutator_and_taker(mutate, take_guard=lambda g: bool(armed))

    def watch(g: ExecutableGraph):
        if g.fired_counts.get("Mutate"):
            landed.set()

    inbox = WatchedInbox()
    stop = asyncio.Event()
    ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, inbox=inbox, stop=stop, observers=(watch,))
    run = asyncio.ensure_future(Runner.run_indefinitely(ctx, tick=0.001))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    armed.append(True)
    await through_the_loops_drain(inbox, Fire("Take"))
    armed.clear()
    assert graph.place_named("P").tokens == [Box("a", 1)], "taken while Mutate was working on it"
    assert graph.place_named("Gone").tokens == []
    assert "Take" not in graph.fired_counts

    release.set()
    await asyncio.wait_for(landed.wait(), timeout=1.0)
    armed.append(True)
    await through_the_loops_drain(inbox, Fire("Take"))
    armed.clear()
    # Fired by the Fire itself, inline: a launch by the sweep would not have landed yet.
    assert graph.place_named("Gone").tokens == [Box("a", 99)]
    assert graph.place_named("P").tokens == []

    stop.set()
    await asyncio.wait_for(run, timeout=1.0)


@pytest.mark.asyncio
async def test_fire_of_a_reader_runs_beside_a_reader_of_the_same_token_in_flight():
    started = asyncio.Event()
    release = asyncio.Event()

    async def look(box: Box, _g: int) -> int:
        started.set()
        await release.wait()
        return box.value

    def glance(box: Box, _g: int) -> int:
        return box.value * 10

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("Shared", Box, tokens=[Box("x", 7)]),
            ListPlaceNode("G1", int, tokens=[1]),
            ListPlaceNode("G2", int, tokens=[1]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Look", look, priority=lambda g: 10.0),
            FunctionTransitionNode("Glance", glance),
            ReadEdge("Shared", "Look", "box"),
            ArgumentEdgeToTransition("G1", "Look", "_g"),
            ReturnedEdgeFromTransition("Look", "Out"),
            ReadEdge("Shared", "Glance", "box"),
            ArgumentEdgeToTransition("G2", "Glance", "_g"),
            ReturnedEdgeFromTransition("Glance", "Out"),
        ]
    )
    ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, inbox=asyncio.Queue())
    run = asyncio.ensure_future(Runner.run(ctx, stop_after_n_firings=1))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    ctx.inbox.put_nowait(Fire("Glance"))
    assert Runner.drain_inbox(ctx) == 1
    assert graph.place_named("Out").tokens == [70]
    assert graph.in_flight == {"Look"}, "the Fire's own firing does not take Look out of flight"

    release.set()
    await asyncio.wait_for(run, timeout=1.0)
    assert graph.place_named("Out").tokens == [70, 7]


@pytest.mark.asyncio
async def test_fire_takes_another_token_of_a_place_while_one_of_its_tokens_is_mutated():
    started = asyncio.Event()
    release = asyncio.Event()

    async def decline_deer(deer: Box, _g: int) -> int:
        started.set()
        await release.wait()
        deer.value -= 1
        return deer.value

    def clear_all(boxes: list[Box]) -> int:
        return len(boxes)

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode(
                "Forest", Box, tokens=[Box("Deer", 10), Box("Bird", 5)], keys={"kind": lambda b: b.kind},
            ),
            ListPlaceNode("Tick", int, tokens=[1]),
            ListPlaceNode("Log", int),
            ListPlaceNode("Wetland", Box),
            FunctionTransitionNode("Decline", decline_deer, priority=lambda g: 10.0),
            FunctionTransitionNode("Migrate", lambda bird: bird),
            FunctionTransitionNode("Clear", clear_all),
            MutateWhereEdge("Forest", "Decline", "deer", where=Filter.where(kind="Deer")),
            ArgumentEdgeToTransition("Tick", "Decline", "_g"),
            ReturnedEdgeFromTransition("Decline", "Log"),
            TakeWhereEdge("Forest", "Migrate", "bird", where=Filter.where(kind="Bird")),
            ReturnedEdgeFromTransition("Migrate", "Wetland"),
            DrainEdge("Forest", "Clear", "boxes"),
            ReturnedEdgeFromTransition("Clear", "Log"),
        ]
    )
    ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, inbox=asyncio.Queue())
    run = asyncio.ensure_future(Runner.run(ctx, stop_after_n_firings=1))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    ctx.inbox.put_nowait(Fire("Clear"))
    ctx.inbox.put_nowait(Fire("Migrate"))
    assert Runner.drain_inbox(ctx) == 1, "Clear needs the whole place, one of whose tokens Decline holds"
    assert graph.fired_counts == {"Migrate": 1}
    assert graph.place_named("Wetland").tokens == [Box("Bird", 5)]
    assert graph.place_named("Forest").tokens == [Box("Deer", 10)]

    release.set()
    await asyncio.wait_for(run, timeout=1.0)
    assert graph.place_named("Forest").tokens == [Box("Deer", 9)]


@pytest.mark.asyncio
async def test_fire_takes_the_token_it_was_checked_by():
    """Between the Fire's check and the take, a body on a thread changes the key of the
    token it holds, so that planning again would choose that token: the Fire takes the
    one it checked. The window is made with Take's guard, which the engine asks again,
    on the Fire's helper thread, just before the take."""
    started, go, changed, finish = (threading.Event() for _ in range(4))
    armed: list[bool] = []
    asked: list[int] = []

    def mutate(box: Box, _g: int) -> int:
        started.set()
        go.wait(1.0)
        box.kind = "Deer"
        changed.set()
        finish.wait(1.0)
        return 1

    def take_guard(_graph: ExecutableGraph) -> bool:
        if not armed:
            return False
        asked.append(1)
        if len(asked) == 2:  # the engine's own check, after the Fire's
            go.set()
            changed.wait(1.0)
        return True

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode(
                "Forest", Box, tokens=[Box("Bird", 1), Box("Deer", 2)], keys={"kind": lambda b: b.kind},
            ),
            ListPlaceNode("G", int, tokens=[1]),
            ListPlaceNode("Log", int),
            ListPlaceNode("Gone", Box),
            FunctionTransitionNode("Mutate", mutate, execution="thread", priority=lambda g: 10.0),
            FunctionTransitionNode("Take", lambda deer: deer, guard=take_guard),
            MutateEdge("Forest", "Mutate", "box"),
            ArgumentEdgeToTransition("G", "Mutate", "_g"),
            ReturnedEdgeFromTransition("Mutate", "Log"),
            TakeWhereEdge("Forest", "Take", "deer", where=Filter.where(kind="Deer")),
            ReturnedEdgeFromTransition("Take", "Gone"),
        ]
    )
    ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, inbox=asyncio.Queue())
    run = asyncio.ensure_future(Runner.run(ctx, stop_after_n_firings=1))
    assert await asyncio.to_thread(started.wait, 1.0)

    armed.append(True)
    ctx.inbox.put_nowait(Fire("Take"))
    assert Runner.drain_inbox(ctx) == 1
    armed.clear()
    assert changed.is_set(), "the window between the check and the take was not made"
    assert graph.place_named("Gone").tokens == [Box("Deer", 2)]
    assert graph.place_named("Forest").tokens == [Box("Deer", 1)], "Mutate's token stays where it is"

    finish.set()
    await asyncio.wait_for(run, timeout=1.0)


@pytest.mark.asyncio
async def test_fire_leaves_the_loops_firings_in_flight():
    """A Fire's own firing empties ``graph.in_flight`` as it lands; the loop's firings
    are put back, so that a second Fire of one of them, in the same drain, does nothing."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(x: int) -> int:
        started.set()
        await release.wait()
        return x

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("In", int, tokens=[1, 2]),
            ListPlaceNode("Q", int, tokens=[7]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Slow", slow, priority=lambda g: 10.0),
            FunctionTransitionNode("Quick", lambda x: x),
            ArgumentEdgeToTransition("In", "Slow", "x"),
            ReturnedEdgeFromTransition("Slow", "Out"),
            ArgumentEdgeToTransition("Q", "Quick", "x"),
            ReturnedEdgeFromTransition("Quick", "Out"),
        ]
    )
    ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, inbox=asyncio.Queue())
    run = asyncio.ensure_future(Runner.run(ctx, stop_after_n_firings=1))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    ctx.inbox.put_nowait(Fire("Quick"))
    ctx.inbox.put_nowait(Fire("Slow"))
    assert Runner.drain_inbox(ctx) == 1
    assert graph.in_flight == {"Slow"}
    assert graph.place_named("In").tokens == [2], "Slow, in flight, fired again"

    release.set()
    await asyncio.wait_for(run, timeout=1.0)
    assert graph.place_named("Out").tokens == [7, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancelled", "failed"])
async def test_the_claims_go_with_the_loop_however_it_ends(ending):
    started = asyncio.Event()

    async def mutate(box: Box, _g: int) -> int:
        started.set()
        await asyncio.Event().wait()  # never lands
        return 1

    async def boom(_n: int) -> int:
        await started.wait()
        raise RuntimeError("boom")

    boom_nodes = (
        ListPlaceNode("B", int, tokens=[1]),
        FunctionTransitionNode("Boom", boom, priority=lambda g: 5.0),
        ArgumentEdgeToTransition("B", "Boom", "_n"),
        ReturnedEdgeFromTransition("Boom", "Log"),
    )
    graph = mutator_and_taker(mutate, *(boom_nodes if ending == "failed" else ()))
    # Boom's failure lets Mutate land for this long (since 2026-11-28), and Mutate never
    # lands, so it is cancelled then, as a cancelled run cancels it at once.
    graph.cancellation_timeout_seconds = 0.05
    ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, inbox=asyncio.Queue())
    run = asyncio.ensure_future(Runner.run(ctx, stop_after_n_firings=2 if ending == "failed" else 1))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    if ending == "cancelled":
        ctx.inbox.put_nowait(Fire("Take"))
        assert Runner.drain_inbox(ctx) == 0
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    else:
        with pytest.raises(TransitionFailedError):
            await asyncio.wait_for(run, timeout=1.0)

    ctx.inbox.put_nowait(Fire("Take"))
    assert Runner.drain_inbox(ctx) == 1, "a claim of a firing the loop abandoned outlived the loop"
    assert graph.place_named("Gone").tokens == [Box("a", 1)]


def test_fire_with_no_loop_running_is_as_before():
    """Sequential mode drains between firings, with nothing in flight, and a caller's own
    drain with no loop running has no claims to meet: a Fire there fires as it always did."""
    def mutate(box: Box, _g: int) -> int:
        box.value = 99
        return 1

    sequential = mutator_and_taker(mutate)
    ctx = RunContext(graph=sequential, inbox=asyncio.Queue())
    ctx.inbox.put_nowait(Fire("Take"))
    assert asyncio.run(Runner.step(ctx)) == 0, "the Fire took the token Mutate needed"
    assert sequential.place_named("Gone").tokens == [Box("a", 1)]
    assert sequential.fired_counts == {"Take": 1}
    assert ctx.n_fired == 1
    assert sequential.in_flight == set()

    alone = mutator_and_taker(mutate)
    ctx = RunContext(graph=alone, mode=ExecutionMode.CONCURRENT, inbox=asyncio.Queue())
    ctx.inbox.put_nowait(Fire("Take"))
    assert Runner.drain_inbox(ctx) == 1
    assert alone.place_named("Gone").tokens == [Box("a", 1)]
    assert alone.in_flight == set()
