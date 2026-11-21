"""The stepping rule per place (PROTOCOL.md §12.3, ``NestedNets``).

Each place that holds nets has one rule, ``stepping``: inner first (the default),
take turns, outer first, or passive. A deep step tries three bands, the
inner-first nets, then the ring of the net and its take-turns nets, then the
outer-first nets, and a member's turn is a deep step of its own, so every level
follows the rules of its own places. These tests are the bands on one net, two
levels with rules of their own, the ring and its cursor, the default, a net that
never comes to rest, the in-flight rule of outer first, the concurrent runtime,
the fuse and the build error. They follow the Rust core's ``tests/stepping.rs``
case for case, so the two runtimes step alike. ``tests/test_engine_policies.py``
holds the evidence that one level of take turns steps as the interleaved policy it
replaced did, and that outer first steps as the old default did.
"""

import asyncio
import copy
from typing import Optional

import pytest

from petritype.core.events import Fired, InnerFiring, Nested, PathStep
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    NestedNets,
    ReturnedEdgeFromTransition,
    TransitionFailedError,
)
from petritype.core.serialisation import NetJson, Templates
from petritype.runtime import ExecutionMode, RunContext, Runner, TooManyFiringsError


def run(coroutine):
    return asyncio.run(coroutine)


def pass_on(n: int) -> int:
    return n


def spin(n: int) -> int:
    return n + 1


# --- a few nets ------------------------------------------------------------------------------


def worker(jobs: int) -> ExecutableGraph:
    """A bare inner net with ``jobs`` things to do: ``Todo -Work-> Done``."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Todo", int, list(range(jobs))),
        ListPlaceNode("Done", int),
        FunctionTransitionNode("Work", pass_on),
        ArgumentEdgeToTransition("Todo", "Work", "n"),
        ReturnedEdgeFromTransition("Work", "Done"),
    ])


def spinner() -> ExecutableGraph:
    """An inner net that never comes to rest: ``Spin`` puts back what it takes."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Wheel", int, [0]),
        FunctionTransitionNode("Spin", spin),
        ArgumentEdgeToTransition("Wheel", "Spin", "n"),
        ReturnedEdgeFromTransition("Spin", "Wheel"),
    ])


def todo(net: ExecutableGraph) -> int:
    return len(net.place_named("Todo").tokens)


def nets_in(net: ExecutableGraph, place: str) -> list[ExecutableGraph]:
    """The nets of the tokens of ``place``, one each, in token order."""
    return list(net.place_named(place).tokens)


async def deep_step(net: ExecutableGraph):
    return await ExecutableGraphOperations.fire_one_transition(net, *ExecutableGraphOperations.maps(net))


def story(net: ExecutableGraph, n: int, names: dict[int, str]) -> list[str]:
    """Where each of ``n`` deep steps fired, named by ``names`` (``net_id`` to name;
    ``"outer"`` for the net stepped, ``"-"`` for a step that fired nothing)."""
    out = []
    for _ in range(n):
        fired = run(deep_step(net))
        if fired is None:
            out.append("-")
        elif isinstance(fired, str):
            out.append("outer")
        else:
            out.append(names[NestedNets.resolve(net, fired.path).net_id])
    return out


def office(stepping: Optional[str], inbox: int, jobs: int, **options) -> tuple[ExecutableGraph, ExecutableGraph]:
    """An outer net with ``inbox`` jobs of its own (``Inbox -Forward-> Outbox``) and a
    ``Desk`` under ``stepping`` (the default when ``None``) holding one worker with
    ``jobs``; and the worker."""
    desk_worker = worker(jobs)
    net = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Desk", ExecutableGraph, [desk_worker], nested=True, stepping=stepping),
        ListPlaceNode("Inbox", int, list(range(inbox))),
        ListPlaceNode("Outbox", int),
        FunctionTransitionNode("Forward", pass_on),
        ArgumentEdgeToTransition("Inbox", "Forward", "n"),
        ReturnedEdgeFromTransition("Forward", "Outbox"),
    ], **options)
    return net, desk_worker


# --- the default -----------------------------------------------------------------------------


class TestTheDefault:

    def test_the_default_is_inner_first(self):
        net, desk_worker = office(None, 2, 2)
        assert net.place_named("Desk").stepping == "inner_first"
        assert story(net, 5, {desk_worker.net_id: "worker"}) == ["worker", "worker", "outer", "outer", "-"], (
            "the inner net that can fire goes before the outer transition that can"
        )

    def test_inner_first_fires_one_transition_a_deep_step(self):
        net, desk_worker = office("inner_first", 0, 5)
        fired = run(deep_step(net))
        assert fired == InnerFiring((PathStep("Desk", 1, ""),), "Work")
        assert todo(desk_worker) == 4, "one firing, not a run to rest"
        assert desk_worker.step_count == 1

    def test_an_inner_net_that_never_rests_stops_the_net_around_it_under_inner_first_only(self):
        def build(stepping):
            wheel = spinner()
            net = ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Desk", ExecutableGraph, [wheel], nested=True, stepping=stepping),
                ListPlaceNode("Inbox", int, [1]),
                ListPlaceNode("Outbox", int),
                FunctionTransitionNode("Forward", pass_on),
                ArgumentEdgeToTransition("Inbox", "Forward", "n"),
                ReturnedEdgeFromTransition("Forward", "Outbox"),
            ])
            return net, {wheel.net_id: "spinner"}

        net, names = build("inner_first")
        assert story(net, 20, names) == ["spinner"] * 20, "the outer net never fires"
        assert "Forward" not in net.fired_counts
        net, names = build("take_turns")
        assert story(net, 4, names) == ["outer", "spinner", "spinner", "spinner"], (
            "the outer net has its turn, and then nothing left to fire"
        )
        assert net.fired_counts["Forward"] == 1


# --- the bands -------------------------------------------------------------------------------


def bands():
    """One net with a place of each rule, declared in the opposite order to the
    bands': ``Late`` (outer first) holding a worker with one job, ``Ring`` (take
    turns) one with two, ``Early`` (inner first) one with two, and ``Shelf``
    (passive) one with five; and three jobs of its own."""
    late, ring, early, shelf = worker(1), worker(2), worker(2), worker(5)
    net = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Late", ExecutableGraph, [late], nested=True, stepping="outer_first"),
        ListPlaceNode("Ring", ExecutableGraph, [ring], nested=True, stepping="take_turns"),
        ListPlaceNode("Early", ExecutableGraph, [early], nested=True, stepping="inner_first"),
        ListPlaceNode("Shelf", ExecutableGraph, [shelf], nested=True, stepping="passive"),
        ListPlaceNode("Inbox", int, [0, 1, 2]),
        ListPlaceNode("Outbox", int),
        FunctionTransitionNode("Forward", pass_on),
        ArgumentEdgeToTransition("Inbox", "Forward", "n"),
        ReturnedEdgeFromTransition("Forward", "Outbox"),
    ])
    names = {late.net_id: "late", ring.net_id: "ring", early.net_id: "early", shelf.net_id: "shelf"}
    return net, names


class TestTheBands:

    def test_a_deep_step_tries_inner_first_then_the_ring_then_outer_first(self):
        net, names = bands()
        assert story(net, 9, names) == [
            # The inner-first net until it is at rest.
            "early", "early",
            # The ring, from the net itself: it and the take-turns net in turn,
            # until both are at rest.
            "outer", "ring", "outer", "ring", "outer",
            # Then the outer-first net, and nothing: the shelf is passive.
            "late", "-",
        ]
        assert not NestedNets.any_fireable(net), "quiescent deep: the passive shelf does not count"
        assert todo(nets_in(net, "Shelf")[0]) == 5

        # Work everywhere again: each band in its order, the ring going on after
        # the net itself, which took its last turn.
        for place in ("Early", "Late"):
            NestedNets.put(nets_in(net, place)[0], "Todo", [9])
        net.place_named("Inbox").tokens.append(9)  # resynced at the next step
        assert story(net, 4, names) == ["early", "outer", "late", "-"]

    def test_the_turns_without_the_net_s_own_take_the_bands_alone(self):
        net, names = bands()
        order = []
        while True:
            fired = run(NestedNets.take_turn(net, "ahead")) or run(NestedNets.take_turn(net, "behind"))
            if fired is None:
                break
            order.append(names[NestedNets.resolve(net, fired.path).net_id])
        assert order == ["early", "early", "ring", "ring", "late"]
        assert len(net.place_named("Inbox").tokens) == 3, "Forward never fired"


# --- two levels ------------------------------------------------------------------------------


def two_levels(outer: str, middle: str):
    """An outer net that can always fire, with a ``Yard`` under ``outer`` holding a
    middle net, which has two jobs of its own and a ``Held`` place under ``middle``
    holding a worker with two."""
    bottom = worker(2)
    middle_net = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Todo", int, [0, 1]),
        ListPlaceNode("Done", int),
        ListPlaceNode("Held", ExecutableGraph, [bottom], nested=True, stepping=middle),
        FunctionTransitionNode("Work", pass_on),
        ArgumentEdgeToTransition("Todo", "Work", "n"),
        ReturnedEdgeFromTransition("Work", "Done"),
    ])
    net = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Wheel", int, [0]),
        ListPlaceNode("Yard", ExecutableGraph, [middle_net], nested=True, stepping=outer),
        FunctionTransitionNode("Spin", spin),
        ArgumentEdgeToTransition("Wheel", "Spin", "n"),
        ReturnedEdgeFromTransition("Spin", "Wheel"),
    ])
    return net, {middle_net.net_id: "middle", bottom.net_id: "bottom"}


class TestTwoLevels:

    def test_each_level_follows_the_rules_of_its_own_places(self):
        # The outer net takes turns with the middle one, whose turn is its own deep
        # step: the bottom net first, inner first, until it rests.
        net, names = two_levels("take_turns", "inner_first")
        assert story(net, 10, names) == [
            "outer", "bottom", "outer", "bottom", "outer", "middle", "outer", "middle", "outer", "outer",
        ]
        # The middle net goes first, inner first, and takes turns with the bottom
        # one; the outer net fires only once both are at rest.
        net, names = two_levels("inner_first", "take_turns")
        assert story(net, 6, names) == ["middle", "bottom", "middle", "bottom", "outer", "outer"]
        # Outer first above and inner first below: the outer net can always fire,
        # so neither inner net ever has a turn.
        net, names = two_levels("outer_first", "inner_first")
        assert story(net, 4, names) == ["outer"] * 4

    def test_a_firing_two_levels_down_is_told_by_its_path_and_counted_on_every_net_between(self):
        net, _names = two_levels("inner_first", "inner_first")
        middle = nets_in(net, "Yard")[0]
        fired = run(deep_step(net))
        assert fired == InnerFiring((PathStep("Yard", 2, ""), PathStep("Held", 3, "")), "Work")
        assert middle.fired_counts == {InnerFiring((PathStep("Held", 3, ""),), "Work"): 1}
        assert net.fired_counts == {fired: 1} and net.last_fired == fired


# --- the ring --------------------------------------------------------------------------------


def carousel():
    """An outer net that can always fire, a take-turns ``Ring`` of two workers with
    three jobs each, and an inner-first ``Pen`` that ``Shelve`` can move them to
    (switched off: shelving is the test's to do, by name)."""
    a, b = worker(3), worker(3)
    net = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Wheel", int, [0]),
        ListPlaceNode("Ring", ExecutableGraph, [a, b], nested=True, stepping="take_turns"),
        ListPlaceNode("Pen", ExecutableGraph, nested=True),
        FunctionTransitionNode("Spin", spin),
        ArgumentEdgeToTransition("Wheel", "Spin", "n"),
        ReturnedEdgeFromTransition("Spin", "Wheel"),
        FunctionTransitionNode("Shelve", lambda net: net),
        ArgumentEdgeToTransition("Ring", "Shelve", "net"),
        ReturnedEdgeFromTransition("Shelve", "Pen"),
    ])
    net.disabled = {"Shelve"}
    return net, {a.net_id: "a", b.net_id: "b"}


def shelve(net: ExecutableGraph) -> None:
    """Fire ``Shelve`` by name, as a plain step: the front of the ring to the pen."""
    def pinned(_graph, fireable):
        return next((t for t in fireable if t.name == "Shelve"), None)
    net.disabled = set()
    try:
        assert run(ExecutableGraphOperations.fire_one_transition(
            net, *ExecutableGraphOperations.maps(net), transition_selector=pinned, descend=False,
        )) == "Shelve"
    finally:
        net.disabled = {"Shelve"}


class TestTheRing:

    def test_the_ring_starts_at_the_net_and_goes_on_after_whoever_fired(self):
        net, names = carousel()
        assert story(net, 6, names) == ["outer", "a", "b", "outer", "a", "b"], (
            "the net itself first, then each member after the last to fire"
        )

    def test_a_member_that_moved_to_a_place_with_another_rule_has_left_the_ring(self):
        net, names = carousel()
        assert story(net, 2, names) == ["outer", "a"]
        # ``a`` fired last. Move it to the inner-first pen: the ring has nowhere to
        # go on from but the start, and ``a`` now goes before everyone.
        shelve(net)
        assert story(net, 5, names) == ["a", "a", "outer", "b", "outer"]
        assert len(nets_in(net, "Ring")) == 1

    def test_a_plain_step_leaves_the_cursor_where_it_is(self):
        net, names = carousel()
        assert story(net, 2, names) == ["outer", "a"]
        run(ExecutableGraphOperations.fire_one_transition(net, *ExecutableGraphOperations.maps(net), descend=False))
        assert story(net, 1, names) == ["b"], "the round goes on after a, the plain Spin not being a turn"

    def test_a_failed_turn_moves_the_cursor_and_a_set_rule_keeps_it(self):
        def fail(n: int) -> int:
            raise RuntimeError("boom")
        failing = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Todo", int, [1]),
            ListPlaceNode("Done", int),
            FunctionTransitionNode("Fail", fail),
            ArgumentEdgeToTransition("Todo", "Fail", "n"),
            ReturnedEdgeFromTransition("Fail", "Done"),
        ])
        # Restoring, so that the worker's one transition fails and puts its token
        # back, and could fail for ever, beside an outer net with work.
        net, _ = office("take_turns", 3, 0, restore_tokens_on_failure=True)
        net.place_named("Desk").tokens[:] = [failing]  # resynced at the next step
        assert run(deep_step(net)) == "Forward"
        with pytest.raises(TransitionFailedError):
            run(deep_step(net))
        assert net._ring_cursor == failing.net_id
        # The failure was the worker's turn, so the next is the net's. A rule set
        # meanwhile, and set back, leaves the cursor where it was.
        for stepping in ("outer_first", "take_turns"):
            net.place_named("Desk").stepping = stepping
        assert run(deep_step(net)) == "Forward"
        with pytest.raises(TransitionFailedError):
            run(deep_step(net))


@Templates.template("Stepping Rules Worker")
def stamped_worker() -> ExecutableGraph:
    return worker(3)


@Templates.template("Stepping Rules Carousel")
def stamped_carousel() -> ExecutableGraph:
    """``Spin`` can always fire, beside a take-turns ``Desk`` holding a worker."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Wheel", int, [0]),
        ListPlaceNode("Desk", ExecutableGraph, [stamped_worker()], nested=True, stepping="take_turns"),
        FunctionTransitionNode("Spin", spin),
        ArgumentEdgeToTransition("Wheel", "Spin", "n"),
        ReturnedEdgeFromTransition("Spin", "Wheel"),
    ])


class TestTheCursorIsNeitherSavedNorCopied:

    def test_a_fork_and_a_restored_net_start_their_rounds_afresh(self):
        net = stamped_carousel()
        assert run(deep_step(net)) == "Spin", "the net itself first"
        fork = copy.deepcopy(net)
        restored = NetJson.restore(NetJson.save(net))
        for other, what in ((fork, "forked"), (restored, "restored")):
            assert other._ring_cursor is None, what
            assert run(deep_step(other)) == "Spin", f"{what}: the round starts afresh"
        # The original keeps its cursor in memory: the worker's turn.
        fired = run(deep_step(net))
        assert isinstance(fired, InnerFiring) and fired.transition == "Work"
        assert todo(nets_in(net, "Desk")[0]) == 2

    def test_the_rule_is_the_definition_s_and_is_not_saved(self):
        net = stamped_carousel()
        net.place_named("Desk").stepping = "passive"
        saved = NetJson.save(net)
        assert "stepping" not in str(saved)
        assert NetJson.restore(saved).place_named("Desk").stepping == "take_turns", "the template's"


# --- outer first and a body in flight --------------------------------------------------------


class TestABodyInFlight:

    @pytest.mark.parametrize("stepping, goes", [
        ("inner_first", True), ("take_turns", True), ("outer_first", False), ("passive", False),
    ])
    def test_outer_first_waits_for_a_body_in_flight_and_the_other_rules_do_not(self, stepping, goes):
        net, _ = office(stepping, 1, 1)
        net.in_flight = {"Forward"}  # what a concurrent runtime shows while Forward's body runs
        fired = run(NestedNets.take_turn(net, "ahead")) or run(NestedNets.take_turn(net, "behind"))
        assert (fired is not None) == goes, stepping
        net.in_flight = set()
        if stepping == "outer_first":
            assert run(NestedNets.take_turn(net, "behind")) is not None, "once it has landed, the worker's turn"

    def test_the_concurrent_runtime_fires_a_take_turns_net_beside_a_body_and_outer_first_waits(self):
        """``Slow``'s body waits for a gate that only the ring's worker opens, so the
        run ends only if that worker fires while ``Slow`` is in flight; it then
        stays in flight a moment longer, while nothing else has work but the late
        worker, which, outer first, fires only after ``Slow`` has landed."""
        gate = {}

        async def slow(n: int) -> int:
            await gate["open"].wait()
            await asyncio.sleep(0.05)
            return n

        def opening(n: int) -> int:
            gate["open"].set()
            return n

        opener = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Todo", int, [0]),
            ListPlaceNode("Done", int),
            FunctionTransitionNode("Open", opening),
            ArgumentEdgeToTransition("Todo", "Open", "n"),
            ReturnedEdgeFromTransition("Open", "Done"),
        ])
        net = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Late", ExecutableGraph, [worker(1)], nested=True, stepping="outer_first"),
            ListPlaceNode("Ring", ExecutableGraph, [opener], nested=True, stepping="take_turns"),
            ListPlaceNode("Fast In", int, [0]),
            ListPlaceNode("Slow In", int, [0]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Fast", pass_on),
            ArgumentEdgeToTransition("Fast In", "Fast", "n"),
            ReturnedEdgeFromTransition("Fast", "Out"),
            FunctionTransitionNode("Slow", slow),
            ArgumentEdgeToTransition("Slow In", "Slow", "n"),
            ReturnedEdgeFromTransition("Slow", "Out"),
        ], record_events=True)

        async def go():
            gate["open"] = asyncio.Event()
            return await asyncio.wait_for(Runner.run(RunContext(graph=net, mode=ExecutionMode.CONCURRENT)), 5)

        summary = run(go())
        assert summary.quiesced and summary.n_fired == 4
        log = [event.event.transition if isinstance(event, Nested) else event.transition
               for event in net.drain_events() if isinstance(event, Fired) or (
                   isinstance(event, Nested) and isinstance(event.event, Fired))]
        assert log == ["Fast", "Open", "Slow", "Work"]

    def test_a_token_an_outer_body_holds_is_not_stepped_while_the_body_runs(self):
        """``Inspect`` holds the box through a mutate edge and waits for a gate the
        opener opens; the box's counter, in the ring before the opener, does not
        fire until ``Inspect`` has landed."""
        gate = {}

        async def inspect(ticket: int, box: ExecutableGraph) -> int:
            await gate["open"].wait()
            return ticket

        def opening(n: int) -> int:
            gate["open"].set()
            return n

        opener = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Todo", int, [0]),
            ListPlaceNode("Done", int),
            FunctionTransitionNode("Open", opening),
            ArgumentEdgeToTransition("Todo", "Open", "n"),
            ReturnedEdgeFromTransition("Open", "Done"),
        ])
        box = worker(2)
        net = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Shelf", ExecutableGraph, [box], nested=True, stepping="take_turns"),
            ListPlaceNode("Ring", ExecutableGraph, [opener], nested=True, stepping="take_turns"),
            ListPlaceNode("Ticket", int, [0]),
            ListPlaceNode("Clock", int, [0]),
            ListPlaceNode("Out", int),
            FunctionTransitionNode("Inspect", inspect),
            ArgumentEdgeToTransition("Ticket", "Inspect", "ticket"),
            MutateEdge("Shelf", "Inspect", "box"),
            ReturnedEdgeFromTransition("Inspect", "Out"),
            FunctionTransitionNode("Tick", pass_on),
            ArgumentEdgeToTransition("Clock", "Tick", "n"),
            ReturnedEdgeFromTransition("Tick", "Out"),
        ], record_events=True)

        async def go():
            gate["open"] = asyncio.Event()
            return await asyncio.wait_for(Runner.run(RunContext(graph=net, mode=ExecutionMode.CONCURRENT)), 5)

        summary = run(go())
        assert summary.quiesced
        log = [event.event.transition if isinstance(event, Nested) else event.transition
               for event in net.drain_events() if isinstance(event, Fired) or (
                   isinstance(event, Nested) and isinstance(event.event, Fired))]
        assert log == ["Tick", "Open", "Inspect", "Work", "Work"]


# --- the runtime -----------------------------------------------------------------------------


def concurrent_story(net: ExecutableGraph):
    """What a concurrent run did, in log order: ``F`` for an outer firing that
    landed, ``W`` for a firing inside."""
    net.record_events = True
    net.drain_events()
    summary = run(Runner.run(RunContext(graph=net, mode=ExecutionMode.CONCURRENT)))
    letters = "".join(
        "F" if isinstance(event, Fired) else "W"
        for event in net.drain_events()
        if isinstance(event, Fired) or (isinstance(event, Nested) and isinstance(event.event, Fired))
    )
    return letters, summary


class TestTheRuntime:

    @pytest.mark.parametrize("stepping, expected, left", [
        ("inner_first", "WWFFF", 0),
        ("take_turns", "FWFWF", 0),
        ("outer_first", "FFFWW", 0),
        ("passive", "FFF", 2),
    ])
    def test_the_concurrent_runtime_follows_each_rule(self, stepping, expected, left):
        net, desk_worker = office(stepping, 3, 2)
        letters, summary = concurrent_story(net)
        assert letters == expected
        assert summary.quiesced
        assert todo(desk_worker) == left
        assert len(net.place_named("Inbox").tokens) == 0

    @pytest.mark.parametrize("stepping, expected", [
        ("inner_first", "WWFFF"), ("take_turns", "FWFWF"), ("outer_first", "FFFWW"), ("passive", "FFF"),
    ])
    def test_the_sequential_runtime_follows_each_rule(self, stepping, expected):
        net, _ = office(stepping, 3, 2, record_events=True)
        summary = run(Runner.run(RunContext(graph=net)))
        letters = "".join(
            "F" if isinstance(event, Fired) else "W"
            for event in net.drain_events()
            if isinstance(event, Fired) or (isinstance(event, Nested) and isinstance(event.event, Fired))
        )
        assert letters == expected and summary.quiesced
        assert summary.n_fired == len(expected)

    @pytest.mark.parametrize("stepping, blamed", [
        ("inner_first", "an inner net"), ("take_turns", "an inner net"),
        ("outer_first", "Forward"), ("passive", "Forward"),
    ])
    @pytest.mark.parametrize("mode", [ExecutionMode.SEQUENTIAL, ExecutionMode.CONCURRENT])
    def test_the_fuse_blames_what_the_rule_would_try_next(self, stepping, blamed, mode):
        net, _ = office(stepping, 3, 3)
        ctx = RunContext(graph=net, mode=mode, error_after_n_firings=1)
        with pytest.raises(TooManyFiringsError, match=f"'{blamed}' attempted to fire"):
            run(Runner.run(ctx))


# --- the build error -------------------------------------------------------------------------


class TestTheSetting:

    def test_a_place_that_holds_no_nets_has_no_rule(self):
        with pytest.raises(ValueError, match="'Inbox' holds no nets, so it has no stepping rule"):
            ListPlaceNode("Inbox", int, stepping="inner_first")

    def test_the_four_names_and_no_other(self):
        for name in ("inner_first", "take_turns", "outer_first", "passive"):
            assert ListPlaceNode("Desk", ExecutableGraph, nested=True, stepping=name).stepping == name
        with pytest.raises(ValueError):
            ListPlaceNode("Desk", ExecutableGraph, nested=True, stepping="interleaved")
