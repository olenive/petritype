"""Closed nets: every net is closed to everyone outside it unless its definition
declares it open (since 2026-11-27; from 2026-11-23 until then, only a net that
marked a port).

From outside a closed net, tokens are put, taken and replaced only at its ports:
a body of the net around it, by any edge that hands it the net, at the ports the
edge names in its ``reaches`` (``tests/test_ports_and_reach.py``), and at no port
of a closed net inside the net it holds; the program through the library's own
functions and the runtime's commands, and the runner's ``inject``, at the top
level and by path. A write at a port of one of its subnet places is judged by its
own rule first. A closed net shields the nets inside it (since 2026-11-27): the
program and the runner write no place of a subnet of it or of a net one of its
tokens carries, at any depth, at a port either, unless every net around is
declared open. A body holding a net cannot set its knobs, fire it by name or load
a marking into it, nor into a net inside it; the program, its drivers and the
runner keep all of them. Reading, stepping and a net's own controls stay allowed.
A refusal names the net, the place and the act, and says how to put it right; for
a body it fails the firing, with what is restored as in any failure.
"""

import asyncio
import gc
import inspect
import pickle
import random
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

import pytest
from pydantic import BaseModel

from petritype import petri_net
from petritype.core.events import Failed, InnerFiring, Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ClosedNetError,
    ClosedNets,
    Containment,
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    HeldByBodyError,
    ListPlaceNode,
    MutateEdge,
    NestedNets,
    NetParams,
    ReadAllEdge,
    ReadEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    SubnetPlaceNode,
    SurveyEdge,
    TransitionFailedError,
    default_transition_selector,
)
from petritype.core.keys import Filter, MutateWhereEdge, ReadWhereEdge, TakeWhereEdge
from petritype.core.serialisation import NetJson, Templates
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry
from petritype.runtime import (
    ControlSpec, Disable, Enable, ExecutionMode, Extend, Fire, RunContext, Runner, SetNetParam, SetParam, SetTokens,
    apply_control, by_control,
)

construct = ExecutableGraphOperations.construct_graph
REMEDY = "start the net with the token in its definition, mark the place as a port, or declare the net open"


def file_it(letter: str) -> str:
    return letter


def post(open: bool = False, ports: bool = True, ledger: tuple = (), steps: bool = False,
         inbox: tuple = ()) -> ExecutableGraph:
    """A post: ``Inbox``, a port unless ``ports`` is false, and ``Ledger``, its own
    place, starting with ``inbox`` and ``ledger``; a parameter ``speed``; with
    ``steps``, ``File`` from the inbox to the ledger, which is the post's own move."""
    return construct([
        ListPlaceNode("Inbox", str, list(inbox), port=ports),
        ListPlaceNode("Ledger", str, list(ledger)),
        *([
            FunctionTransitionNode("File", file_it),
            ArgumentEdgeToTransition("Inbox", "File", "letter"),
            ReturnedEdgeFromTransition("File", "Ledger"),
        ] if steps else []),
    ], params={"speed": 1}, template="Test Post", open=open)


def run(graph: ExecutableGraph, firings=1) -> ExecutableGraph:
    asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=firings))
    return graph


def tokens(graph: ExecutableGraph, place) -> list:
    return list(graph.place_named(place).tokens)


def positionally(function, arguments: dict) -> list:
    """``arguments`` as positional arguments of ``function``, in the order its
    signature declares its parameters, up to the last one named; a parameter
    between them that is not named takes its default."""
    parameters = list(inspect.signature(function).parameters.values())
    last = max(index for index, parameter in enumerate(parameters) if parameter.name in arguments)
    values = [arguments.get(parameter.name, parameter.default) for parameter in parameters[:last + 1]]
    assert inspect.Parameter.empty not in values, "a parameter before the last one named has no default"
    return values


# --- the words -------------------------------------------------------------------------------------------


class TestWhichNetsAreClosed:

    def test_a_net_that_marks_a_port_is_closed(self):
        assert post().is_closed and not post().open

    def test_a_net_that_marks_no_port_is_closed_too(self):
        graph = post(ports=False)
        assert graph.is_closed and not graph.open

    def test_a_net_may_declare_itself_open_whether_it_marks_a_port_or_not(self):
        for graph in (post(open=True), post(ports=False, open=True)):
            assert graph.open and not graph.is_closed

    def test_open_is_part_of_the_definition_kept_by_a_copy_a_fork_and_a_restore(self):
        Templates.register("Open Test Post", lambda: construct([
            ListPlaceNode("Inbox", str, port=True), ListPlaceNode("Ledger", str),
        ], open=True))
        try:
            graph = Templates.build("Open Test Post")
            assert deepcopy(graph).open and NestedNets.fork(graph).open
            saved = NetJson.save(graph)
            assert "open" not in saved and "open" not in str(saved["marking"]), "not in a saved form"
            assert NetJson.restore(saved).open
        finally:
            Templates.forget("Open Test Post")


# --- the program ---------------------------------------------------------------------------------------------


class TestTheProgram:

    def test_a_put_at_a_port_lands(self):
        graph = post()
        assert len(NestedNets.put(graph, "Inbox", ["Letter"])) == 1 and tokens(graph, "Inbox") == ["Letter"]

    def test_a_put_at_its_own_place_is_refused_and_says_how_to_put_it_right(self):
        graph = post()
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(graph, "Ledger", ["Forgery"])
        error = refused.value
        assert (error.net, error.place, error.act) == ("Test Post", "Ledger", "a put")
        assert "place 'Ledger'" in str(error) and "'Test Post'" in str(error) and REMEDY in str(error)
        assert isinstance(error, ValueError) and tokens(graph, "Ledger") == [], "nothing was put"

    def test_a_net_without_a_template_is_named_by_its_id(self):
        graph = construct([ListPlaceNode("Inbox", str, port=True), ListPlaceNode("Ledger", str)])
        with pytest.raises(ClosedNetError, match=f"net '#{graph.net_id}'"):
            NestedNets.put(graph, "Ledger", ["Forgery"])

    def test_a_take_is_refused_at_its_own_place_and_lands_at_a_port(self):
        graph = post(ledger=("Entry",))
        with pytest.raises(ClosedNetError, match="a take"):
            NestedNets.take(graph, "Ledger")
        assert tokens(graph, "Ledger") == ["Entry"]
        NestedNets.put(graph, "Inbox", ["Letter"])
        assert NestedNets.take(graph, "Inbox") == ["Letter"]

    def test_an_open_net_takes_tokens_anywhere(self):
        for graph in (post(open=True), post(ports=False, open=True)):
            NestedNets.put(graph, "Ledger", ["Entry"])
            assert NestedNets.take(graph, "Ledger") == ["Entry"]

    def test_a_net_that_marks_no_port_takes_no_token_from_the_program(self):
        """It is closed, and has no port to take one at."""
        graph = post(ports=False, ledger=("Entry",))
        for place in ("Inbox", "Ledger"):
            with pytest.raises(ClosedNetError, match=f"a put at place '{place}'"):
                NestedNets.put(graph, place, ["Letter"])
            with pytest.raises(ClosedNetError, match=f"a take at place '{place}'"):
                NestedNets.take(graph, place)
        assert tokens(graph, "Inbox") == [] and tokens(graph, "Ledger") == ["Entry"]

    def test_a_port_pair_is_judged_by_the_net_around_the_subnet_first(self):
        """A port of a subnet place is never a port of the net that holds it."""
        def holder(open: bool, port: bool = True) -> ExecutableGraph:
            return construct([
                ListPlaceNode("Desk", str, port=port),
                SubnetPlaceNode("Branch", post()),
            ], open=open)

        for closed in (holder(open=False), holder(open=False, port=False)):
            with pytest.raises(ClosedNetError, match="port 'Inbox' of subnet place 'Branch'"):
                NestedNets.put(closed, Port("Branch", "Inbox"), ["Letter"])
            assert tokens(closed.subnet("Branch"), "Inbox") == []
        opened = holder(open=True)
        NestedNets.put(opened, Port("Branch", "Inbox"), ["Letter"])
        assert tokens(opened.subnet("Branch"), "Inbox") == ["Letter"]

    def test_a_subnet_scope_block_is_outside_the_subnet(self):
        """Through a net declared open the block meets the subnet's own rule: the port
        takes the token and its own place refuses it. A closed net shields the subnet
        (since 2026-11-27): the block puts nothing there, at the port either
        (``TestAClosedNetShieldsTheNetsInsideIt``), and sets its knobs as before."""
        world = construct([SubnetPlaceNode("Branch", post())], open=True)
        with NestedNets.subnet_scope(world, "Branch") as branch:
            NestedNets.put(branch, "Inbox", ["Letter"])
            with pytest.raises(ClosedNetError) as refused:
                NestedNets.put(branch, "Ledger", ["Forgery"])
            assert refused.value.around is None, "the subnet's own rule"
            NestedNets.set_param(branch, "speed", 3)  # knobs stay with the program
        assert tokens(world.subnet("Branch"), "Inbox") == ["Letter"] and world.subnet("Branch").params["speed"] == 3
        closed = construct([SubnetPlaceNode("Branch", post())])
        with NestedNets.subnet_scope(closed, "Branch") as branch:
            with pytest.raises(ClosedNetError) as refused:
                NestedNets.put(branch, "Inbox", ["Letter"])
            assert refused.value.around == f"#{closed.net_id}"
            NestedNets.set_param(branch, "speed", 3)
        assert tokens(closed.subnet("Branch"), "Inbox") == [] and closed.subnet("Branch").params["speed"] == 3

    def test_knobs_reading_and_stepping_stay_with_the_program(self):
        graph = post(steps=True)
        NetParams.set(graph, "speed", 2)
        assert graph.place_named("Ledger").tokens == []  # reading
        NestedNets.put(graph, "Inbox", ["Letter"])
        run(graph)
        assert graph.params["speed"] == 2 and tokens(graph, "Ledger") == ["Letter"], "its own move"

    def test_the_whole_state_acts_reach_every_place(self):
        Templates.register("Test Post", post)
        try:
            graph = post()
            graph.import_marking({"Inbox": ["Letter"], "Ledger": ["Entry"]})
            assert tokens(graph, "Ledger") == ["Entry"]
            saved = NetJson.save(graph)
            assert tokens(NetJson.restore(saved), "Ledger") == ["Entry"]
            assert tokens(NetJson.load_into(post(), saved), "Ledger") == ["Entry"]
            assert tokens(NestedNets.fork(graph), "Ledger") == ["Entry"]
        finally:
            Templates.forget("Test Post")


def loaded_post(strict: bool) -> RunContext:
    return RunContext(graph=post(steps=True), inbox=asyncio.Queue(), strict=strict)


def drained(ctx: RunContext, *commands) -> int:
    for command in commands:
        ctx.inbox.put_nowait(command)
    return Runner.drain_inbox(ctx)


class TestTheRuntimesCommands:

    @pytest.mark.parametrize("command", [Extend("Ledger", ["Forgery"]), SetTokens("Ledger", ["Forgery"])],
                             ids=["Extend", "SetTokens"])
    @pytest.mark.parametrize("strict", [True, False], ids=["strict", "not strict"])
    def test_every_context_raises_the_refusal_strict_or_not(self, command, strict):
        """A place that is not a port is a matter of the net's definition, not of the
        input: a context that drops a command it cannot decode still raises this
        one, so that no program waits for a token that was never let in."""
        async def go():
            ctx = loaded_post(strict=strict)
            with pytest.raises(ClosedNetError, match="a SetTokens|an Extend") as refused:
                drained(ctx, command)
            assert REMEDY in str(refused.value) and tokens(ctx.graph, "Ledger") == []
        asyncio.run(go())

    def test_a_run_that_is_not_strict_ends_on_it(self):
        """The refusal leaves through the run that drained the command."""
        async def go():
            ctx = loaded_post(strict=False)
            ctx.inbox.put_nowait(Extend("Ledger", ["Forgery"]))
            with pytest.raises(ClosedNetError):
                await Runner.run(ctx)
            assert tokens(ctx.graph, "Ledger") == []
        asyncio.run(go())

    @pytest.mark.parametrize("command", [Extend, SetTokens], ids=["Extend", "SetTokens"])
    def test_a_net_that_marks_a_port_takes_them_there(self, command):
        async def go():
            ctx = loaded_post(strict=True)
            assert drained(ctx, command("Inbox", ["Letter"])) == 1 and tokens(ctx.graph, "Inbox") == ["Letter"]
        asyncio.run(go())

    @pytest.mark.parametrize("command", [Extend, SetTokens], ids=["Extend", "SetTokens"])
    def test_a_net_that_marks_no_port_refuses_them_everywhere_and_one_declared_open_takes_them(self, command):
        async def go():
            for place in ("Inbox", "Ledger"):
                strict = RunContext(graph=post(ports=False), inbox=asyncio.Queue(), strict=True)
                with pytest.raises(ClosedNetError, match=f"place '{place}'"):
                    drained(strict, command(place, ["Letter"]))
                loose = RunContext(graph=post(ports=False), inbox=asyncio.Queue(), strict=False)
                with pytest.raises(ClosedNetError, match=f"place '{place}'"):
                    drained(loose, command(place, ["Letter"]))
                opened = RunContext(graph=post(ports=False, open=True), inbox=asyncio.Queue(), strict=True)
                assert drained(opened, command(place, ["Letter"])) == 1
                assert tokens(strict.graph, place) == tokens(loose.graph, place) == [] and tokens(opened.graph, place) == [
                    "Letter"]
        asyncio.run(go())

    def test_a_port_pair_on_a_closed_net_is_refused(self):
        async def go():
            graph = construct([ListPlaceNode("Desk", str, port=True), SubnetPlaceNode("Branch", post())])
            ctx = RunContext(graph=graph, inbox=asyncio.Queue(), strict=True)
            with pytest.raises(ClosedNetError):
                drained(ctx, Extend(Port("Branch", "Inbox"), ["Letter"]))
        asyncio.run(go())

    def test_the_knobs_and_firing_by_name_stay_with_the_program(self):
        async def go():
            ctx = loaded_post(strict=True)
            NestedNets.put(ctx.graph, "Inbox", ["Letter"])
            assert drained(
                ctx, SetNetParam("speed", 4), SetParam("File", {}), Disable("File"), Enable("File"), Fire("File"),
            ) == 5
            assert ctx.graph.params["speed"] == 4 and tokens(ctx.graph, "Ledger") == ["Letter"]
        asyncio.run(go())

    def test_a_control_of_the_net_is_inside_it(self):
        async def go():
            ctx = loaded_post(strict=True)
            spec = ControlSpec.inject("Ledger", lambda _clicked: "Entry", "Add an entry")
            command = apply_control(ctx, spec, True)
            assert command == Extend("Ledger", ["Entry"]), "the same command, marked as a control's"
            assert Runner.drain_inbox(ctx) == 1 and tokens(ctx.graph, "Ledger") == ["Entry"]
        asyncio.run(go())

    def test_a_control_sets_no_parameter_of_a_net_a_body_holds_until_the_hold_ends(self):
        """Until 2026-11-28 the control's command landed, and the body that held the
        net was failed for it when it returned."""
        graph = ExecutableGraphOperations.construct_graph(
            [ListPlaceNode("In", int, port=True)],
            params={"limit": 10},
        )
        hold = ClosedNets.hold(graph)
        try:
            for command in (SetNetParam("limit", 20), by_control(SetNetParam("limit", 25))):
                ctx = RunContext(graph=graph, inbox=asyncio.Queue(), strict=True)
                ctx.inbox.put_nowait(command)
                with pytest.raises(HeldByBodyError) as refused:
                    Runner.drain_inbox(ctx)
                assert refused.value.holder == "other" and graph.params["limit"] == 10
            assert hold.refusal is None, "the program's refusal is its alone"
        finally:
            ClosedNets.unhold(hold)
        ctx = RunContext(graph=graph, inbox=asyncio.Queue(), strict=True)
        ctx.inbox.put_nowait(by_control(SetNetParam("limit", 25)))
        assert Runner.drain_inbox(ctx) == 1 and graph.params["limit"] == 25


# --- a body of the net around it -----------------------------------------------------------------------------


KEYS = {"kind": lambda net: "post"}
WHERE = Filter.where(kind="post")
# Every edge kind that hands a body the live token: its name, the edge (made with
# the ``reaches`` given, none by default), and whether the argument is a list and
# whether the edge consumes.
LENDING_EDGES = (
    ("take", lambda **reach: ArgumentEdgeToTransition("Posts", "Use", "unit", **reach), False, True),
    ("take_all", lambda **reach: ArgumentEdgeToTransition("Posts", "Use", "units", **reach), True, True),
    ("take_where", lambda **reach: TakeWhereEdge("Posts", "Use", "unit", where=WHERE, **reach), False, True),
    ("drain", lambda **reach: DrainEdge("Posts", "Use", "units", **reach), True, True),
    ("mutate", lambda **reach: MutateEdge("Posts", "Use", "unit", **reach), False, False),
    ("mutate_where", lambda **reach: MutateWhereEdge("Posts", "Use", "unit", where=WHERE, **reach), False, False),
    ("read", lambda **reach: ReadEdge("Posts", "Use", "unit", **reach), False, False),
    ("read_where", lambda **reach: ReadWhereEdge("Posts", "Use", "unit", where=WHERE, **reach), False, False),
    ("read_all", lambda **reach: ReadAllEdge("Posts", "Use", "units", **reach), True, False),
)
KINDS = [kind for kind, *_ in LENDING_EDGES]


def body_doing(action, many: bool, asynchronous: bool = False):
    """A body that hands ``action`` the post it holds and produces its order."""
    if asynchronous:
        async def use(order: str, unit: ExecutableGraph) -> str:
            await action(unit)
            return order
        return use
    if many:
        def use_many(order: str, units: list[ExecutableGraph]) -> str:
            action(units[0])
            return order
        return use_many

    def use_one(order: str, unit: ExecutableGraph) -> str:
        action(unit)
        return order
    return use_one


def office(edge, body, unit: ExecutableGraph, restore: bool = True, record: bool = False,
           open: bool = False) -> ExecutableGraph:
    """A place of posts, ``Posts``, handed to ``Use`` by ``edge``, which also takes
    an order and produces it into ``Done``; ``open`` declares the office open."""
    return construct([
        ListPlaceNode("Posts", ExecutableGraph, [unit], nested=True, keys=KEYS),
        ListPlaceNode("Orders", str, ["Letter"]),
        ListPlaceNode("Done", str),
        FunctionTransitionNode("Use", body),
        ArgumentEdgeToTransition("Orders", "Use", "order"),
        edge,
        ReturnedEdgeFromTransition("Use", "Done"),
    ], restore_tokens_on_failure=restore, record_events=record, open=open)


def fails_with(graph: ExecutableGraph, kind):
    with pytest.raises(TransitionFailedError) as failed:
        run(graph)
    assert isinstance(failed.value.__cause__, kind), failed.value.__cause__
    return failed.value


class TestABodyByEachEdgeKind:

    @pytest.mark.parametrize("kind, make, many, consumes", LENDING_EDGES, ids=KINDS)
    def test_a_put_at_a_port_its_edge_reaches_lands(self, kind, make, many, consumes):
        unit = post()
        graph = run(office(make(reaches=("Inbox",)),
                           body_doing(lambda net: NestedNets.put(net, "Inbox", ["Letter"]), many), unit))
        assert tokens(unit, "Inbox") == ["Letter"] and tokens(graph, "Done") == ["Letter"]
        assert not unit.held_by_body, "released when the body returned"

    @pytest.mark.parametrize("kind, make, many, consumes", LENDING_EDGES, ids=KINDS)
    def test_a_put_at_its_own_place_fails_the_firing_and_what_is_taken_goes_back(self, kind, make, many, consumes):
        unit = post()
        graph = office(make(), body_doing(lambda net: NestedNets.put(net, "Ledger", ["Forgery"]), many), unit)
        ids = graph.place_named("Posts").token_ids
        failure = fails_with(graph, ClosedNetError)
        assert failure.restored and tokens(graph, "Orders") == ["Letter"] and tokens(graph, "Done") == []
        assert tokens(graph, "Posts") == [unit] and graph.place_named("Posts").token_ids == ids, "with its id"
        assert tokens(unit, "Ledger") == [] and not unit.held_by_body
        assert set(failure.consumed) == ({"order", "units" if many else "unit"} if consumes else {"order"})

    @pytest.mark.parametrize("kind, make, many, consumes", LENDING_EDGES, ids=KINDS)
    def test_a_refusal_the_body_caught_fails_the_firing_all_the_same(self, kind, make, many, consumes):
        def forge(net):
            try:
                NestedNets.put(net, "Ledger", ["Forgery"])
            except ClosedNetError:
                pass
        fails_with(office(make(), body_doing(forge, many), post()), ClosedNetError)

    @pytest.mark.parametrize("kind, make, many, consumes", LENDING_EDGES, ids=KINDS)
    def test_an_edit_of_its_own_places_list_is_found_when_the_body_returns(self, kind, make, many, consumes):
        unit = post()
        failure = fails_with(
            office(make(), body_doing(lambda net: net.place_named("Ledger").tokens.append("Forgery"), many), unit),
            ClosedNetError,
        )
        assert failure.__cause__.place == "Ledger" and "an edit of the place's tokens" in str(failure.__cause__)
        assert tokens(unit, "Ledger") == ["Forgery"], "what the body did stays done"

    @pytest.mark.parametrize("kind, make, many, consumes", LENDING_EDGES, ids=KINDS)
    def test_its_knobs_and_firing_it_by_name_are_refused(self, kind, make, many, consumes):
        def by_name(net):
            ctx = RunContext(graph=net, inbox=asyncio.Queue(), strict=True)
            ctx.inbox.put_nowait(Fire("File"))
            Runner.drain_inbox(ctx)

        for ports in (True, False):  # a net that marks no port keeps them from the body too
            for action in (
                lambda net: NestedNets.set_param(net, "speed", 9),
                lambda net: net.params.update(speed=9),
                lambda net: net.disabled.add("File"),
                by_name,
            ):
                unit = post(ports=ports, steps=True)
                failure = fails_with(office(make(), body_doing(action, many), unit), HeldByBodyError)
                assert "stay with the program and the runner" in str(failure.__cause__)
            assert unit.params["speed"] == 1

    @pytest.mark.parametrize("kind, make, many, consumes", LENDING_EDGES[:1] + LENDING_EDGES[4:5], ids=["take", "mutate"])
    def test_stepping_a_net_it_holds_is_the_nets_own_move(self, kind, make, many, consumes):
        async def step(net):
            NestedNets.put(net, "Inbox", ["Letter"])
            await ExecutableGraphOperations.execute_graph(net, stop_after_n_firings=None)

        unit = post(steps=True)
        run(office(make(reaches=("Inbox",)), body_doing(step, many, asynchronous=True), unit))
        assert tokens(unit, "Ledger") == ["Letter"], "File moved it into the ledger, inside the post"

    @pytest.mark.parametrize("make", [
        lambda: SnapshotEdge("Posts", "Use", "unit"), lambda: SurveyEdge("Posts", "Use", "units"),
    ], ids=["snapshot", "survey"])
    def test_a_snapshot_and_a_survey_hand_over_copies_and_hold_nothing(self, make):
        unit, seen = post(), []
        many = "units" == make().argument
        run(office(make(), body_doing(lambda net: seen.append(net.held_by_body), many), unit))
        assert seen == [False] and not unit.held_by_body


class TestABodyOnOtherNets:

    def test_an_open_net_takes_the_write_and_keeps_its_knobs_from_the_body(self):
        unit = post(open=True)
        run(office(MutateEdge("Posts", "Use", "unit"), body_doing(lambda net: NestedNets.put(net, "Ledger", ["Entry"]), False), unit))
        assert tokens(unit, "Ledger") == ["Entry"]
        fails_with(
            office(MutateEdge("Posts", "Use", "unit"), body_doing(lambda net: NestedNets.set_param(net, "speed", 9), False), post(open=True)),
            HeldByBodyError,
        )

    def test_a_net_that_marks_no_port_takes_no_token_from_a_body(self):
        """It has no port for an edge to name, and naming one of its places opens
        nothing: every write is refused as at any closed net's own place."""
        for action in (
            lambda net: NestedNets.put(net, "Inbox", ["Letter"]),
            lambda net: NestedNets.take(net, "Ledger"),
            lambda net: net.place_named("Ledger").tokens.append("Forgery"),
        ):
            unit = post(ports=False, ledger=("Entry",))
            refusal = fails_with(office(MutateEdge("Posts", "Use", "unit", reaches=("Inbox", "Ledger")),
                                        body_doing(action, False), unit), ClosedNetError).__cause__
            assert not refusal.undeclared and not refusal.deeper
            assert tokens(unit, "Inbox") == [] and tokens(unit, "Ledger")[0] == "Entry"

    @pytest.mark.parametrize("open", [False, True], ids=["closed", "open"])
    def test_a_closed_net_two_levels_down_takes_no_token_from_the_body(self, open):
        """The body holds the outer post, ``open`` or not, and reads its way down to
        a post in the outer post's own place of nets. An edge names ports of the net
        it hands over, so nothing drawn could show a reach into the post below: a
        write there is refused as ``deeper`` whatever the edge names, at its port
        and at a place of its own alike, and the refusal says to go through the net
        the body holds, since marking a port below would open nothing to the body.
        The program writes the port once every net around the post below is
        declared open; until then it is refused too, by the nearest closed net
        around (``around``, since 2026-11-27)."""
        def outer_post(sack: ExecutableGraph, declared_open: bool = open) -> ExecutableGraph:
            return construct([
                ListPlaceNode("Inbox", str, port=True),
                ListPlaceNode("Sacks", ExecutableGraph, [sack], nested=True, stepping="passive"),
            ], open=declared_open)

        def into_the_sack(place: str):
            return body_doing(lambda net: NestedNets.put(tokens(net, "Sacks")[0], place, ["Letter"]), False)

        sack = post()
        unit = outer_post(sack)
        graph = office(MutateEdge("Posts", "Use", "unit", reaches=("Inbox",)), into_the_sack("Inbox"), unit)
        refusal = fails_with(graph, ClosedNetError).__cause__
        assert refusal.deeper and not refusal.undeclared and (refusal.net, refusal.place, refusal.act) == (
            "Test Post", "Inbox", "a put")
        assert "inside a net the body holds" in str(refusal) and tokens(sack, "Inbox") == []
        refusal = fails_with(office(MutateEdge("Posts", "Use", "unit"), into_the_sack("Ledger"), outer_post(post())),
                             ClosedNetError).__cause__
        assert refusal.deeper and refusal.place == "Ledger" and "mark the place as a port" not in str(refusal)
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(sack, "Inbox", ["From the program"])
        assert refused.value.around == f"#{(graph if open else unit).net_id}" and tokens(sack, "Inbox") == []
        sack = post()
        graph = office(MutateEdge("Posts", "Use", "unit", reaches=("Inbox",)), into_the_sack("Inbox"),
                       outer_post(sack, declared_open=True), open=True)
        assert fails_with(graph, ClosedNetError).__cause__.deeper, "the body is refused as before"
        NestedNets.put(sack, "Inbox", ["From the program"])
        assert tokens(sack, "Inbox") == ["From the program"]

    @pytest.mark.parametrize("unit_open", [False, True], ids=["closed unit", "open unit"])
    def test_a_net_two_levels_down_that_is_declared_open_is_shielded_by_a_closed_net_the_body_holds(self, unit_open):
        """The sack is open, so its own rule lets a write land anywhere. The unit
        the body holds is around it: closed, it shields the sack from the body, whose
        edge names ports of the unit alone; declared open, it lets the write through
        to the sack's own rule. The office the body belongs to is closed either way,
        and does not count: the body is on the unit's side of it."""
        sack = post(open=True)
        unit = construct(
            [ListPlaceNode("Sacks", ExecutableGraph, [sack], nested=True, stepping="passive")],
            open=unit_open, template="Test Unit",
        )
        graph = office(MutateEdge("Posts", "Use", "unit"), body_doing(
            lambda net: NestedNets.put(tokens(net, "Sacks")[0], "Ledger", ["Letter"]), False), unit)
        if unit_open:
            run(graph)
            assert tokens(sack, "Ledger") == ["Letter"]
        else:
            refusal = fails_with(graph, ClosedNetError).__cause__
            assert refusal.around == "Test Unit" and not refusal.deeper and tokens(sack, "Ledger") == []

    def test_a_subnet_scope_on_a_held_net_holds_the_subnet_for_the_body(self):
        """In the block the body reads the subnet; the subnet is a net inside the one
        it holds, so a closed one takes no token from the body, at its port
        (``deeper``, whatever the edge names) or elsewhere, and keeps its knobs.
        The program's block puts a token at the port when the branch office and
        the office around it are declared open, and none otherwise."""
        def branch_office(open: bool = False) -> ExecutableGraph:
            return construct([ListPlaceNode("Desk", str, port=True), SubnetPlaceNode("Branch", post())], open=open)

        def through_scope(action):
            def act(net):
                with NestedNets.subnet_scope(net, "Branch") as branch:
                    assert branch.held_by_body
                    action(branch)
            return act

        unit, seen = branch_office(), []
        run(office(MutateEdge("Posts", "Use", "unit"),
                   body_doing(through_scope(lambda b: seen.append(tokens(b, "Inbox"))), False), unit))
        assert seen == [[]] and not unit.subnet("Branch").held_by_body
        unit = branch_office()
        graph = office(MutateEdge("Posts", "Use", "unit", reaches=("Inbox",)),
                       body_doing(through_scope(lambda b: NestedNets.put(b, "Inbox", ["Letter"])), False), unit)
        refusal = fails_with(graph, ClosedNetError).__cause__
        assert refusal.deeper and refusal.place == "Inbox" and tokens(unit.subnet("Branch"), "Inbox") == []
        for action, kind in (
            (lambda b: NestedNets.set_param(b, "speed", 9), HeldByBodyError),
            (lambda b: b.place_named("Ledger").tokens.append("Forgery"), ClosedNetError),
        ):
            fails_with(office(MutateEdge("Posts", "Use", "unit"), body_doing(through_scope(action), False),
                              branch_office()), kind)
        with NestedNets.subnet_scope(unit, "Branch") as branch:
            with pytest.raises(ClosedNetError) as refused:
                NestedNets.put(branch, "Inbox", ["From the program"])
        assert refused.value.around == f"#{unit.net_id}" and tokens(unit.subnet("Branch"), "Inbox") == []
        unit = branch_office(open=True)
        graph = office(MutateEdge("Posts", "Use", "unit"), body_doing(lambda net: None, False), unit, open=True)
        with NestedNets.subnet_scope(unit, "Branch") as branch:
            NestedNets.put(branch, "Inbox", ["From the program"])
        assert tokens(unit.subnet("Branch"), "Inbox") == ["From the program"] and tokens(graph, "Posts") == [unit]

    def test_a_port_pair_through_a_held_net_reaches_no_closed_subnet(self):
        """The port of a subnet place is a place of the subnet, a net inside the net
        the body holds. A closed held net refuses the pair as it refuses any place
        that is not its own port; an open one would let it land, but the subnet is
        closed, so the body is refused there as ``deeper``, as it is through
        ``subnet_scope``. An open subnet of an open net takes it, and so does the
        closed one from the program, when the office around the open net is
        declared open too: inside a closed office the program is refused
        (``around``, since 2026-11-27)."""
        def branch_office(open: bool, branch: ExecutableGraph) -> ExecutableGraph:
            return construct([ListPlaceNode("Desk", str, port=True), SubnetPlaceNode("Branch", branch)], open=open)

        write = body_doing(lambda net: NestedNets.put(net, Port("Branch", "Inbox"), ["Letter"]), False)
        fails_with(office(MutateEdge("Posts", "Use", "unit"), write, branch_office(False, post())), ClosedNetError)
        unit = branch_office(True, post())
        graph = office(MutateEdge("Posts", "Use", "unit"), write, unit)
        refusal = fails_with(graph, ClosedNetError).__cause__
        assert refusal.deeper and (refusal.net, refusal.place) == ("Test Post", "Inbox")
        assert tokens(unit.subnet("Branch"), "Inbox") == []
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(unit, Port("Branch", "Inbox"), ["From the program"])
        assert refused.value.around == f"#{graph.net_id}" and tokens(unit.subnet("Branch"), "Inbox") == []
        unit = branch_office(True, post())
        graph = office(MutateEdge("Posts", "Use", "unit"), write, unit, open=True)
        assert fails_with(graph, ClosedNetError).__cause__.deeper, "the body is refused as before"
        NestedNets.put(unit, Port("Branch", "Inbox"), ["From the program"])
        assert tokens(unit.subnet("Branch"), "Inbox") == ["From the program"]
        unit = branch_office(True, post(open=True))
        run(office(MutateEdge("Posts", "Use", "unit"), write, unit))
        assert tokens(unit.subnet("Branch"), "Inbox") == ["Letter"]

    def test_a_body_that_builds_a_new_net_has_made_its_own(self):
        """The new net starts with its tokens in its definition; the old one was
        not touched, so nothing is found."""
        def renew(order: str, unit: ExecutableGraph) -> ExecutableGraph:
            return post(ledger=("Fresh",))

        graph = construct([
            ListPlaceNode("Posts", ExecutableGraph, [post()], nested=True),
            ListPlaceNode("Orders", str, ["Letter"]),
            ListPlaceNode("Renewed", ExecutableGraph, nested=True),
            FunctionTransitionNode("Renew", renew),
            ArgumentEdgeToTransition("Orders", "Renew", "order"),
            ArgumentEdgeToTransition("Posts", "Renew", "unit"),
            ReturnedEdgeFromTransition("Renew", "Renewed"),
        ])
        run(graph)
        assert tokens(tokens(graph, "Renewed")[0], "Ledger") == ["Fresh"]

    def test_what_the_body_did_before_the_refusal_stays_done(self):
        def both(net):
            NestedNets.put(net, "Inbox", ["Letter"])
            NestedNets.put(net, "Ledger", ["Forgery"])

        unit = post()
        fails_with(office(ArgumentEdgeToTransition("Posts", "Use", "unit", reaches=("Inbox",)), body_doing(both, False), unit),
                   ClosedNetError)
        assert tokens(unit, "Inbox") == ["Letter"] and tokens(unit, "Ledger") == []

    def test_the_failure_is_told_with_the_named_error(self):
        graph = office(ArgumentEdgeToTransition("Posts", "Use", "unit"),
                       body_doing(lambda net: NestedNets.put(net, "Ledger", ["Forgery"]), False), post(), record=True)
        graph.drain_events()
        fails_with(graph, ClosedNetError)
        [failed] = [event for event in graph.drain_events() if isinstance(event, Failed)]
        assert failed.restored and failed.error.startswith("ClosedNetError: a put at place 'Ledger'")

    def test_without_restore_the_taken_tokens_are_lost_as_in_any_failure(self):
        unit = post()
        graph = office(ArgumentEdgeToTransition("Posts", "Use", "unit"),
                       body_doing(lambda net: NestedNets.put(net, "Ledger", ["Forgery"]), False), unit, restore=False)
        failure = fails_with(graph, ClosedNetError)
        assert not failure.restored and tokens(graph, "Posts") == [] and failure.consumed["unit"] is unit

    def test_the_concurrent_runner_fails_the_firing_the_same_way_and_lets_go(self):
        unit = post()
        graph = office(MutateEdge("Posts", "Use", "unit"),
                       body_doing(lambda net: NestedNets.put(net, "Ledger", ["Forgery"]), False), unit)
        ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)
        with pytest.raises(TransitionFailedError) as failed:
            asyncio.run(Runner.run(ctx))
        assert isinstance(failed.value.__cause__, ClosedNetError) and not unit.held_by_body
        assert tokens(graph, "Orders") == ["Letter"]

    def test_a_firing_with_a_selector_of_the_callers_own_is_refused_by_keyword_and_by_position(self):
        """Through ``fire_one_transition``, which refuses it, and ``execute_graph``,
        which passes its selector on; by position, each where its own signature
        puts ``transition_selector``. Stepping without one stays allowed."""
        def inc(x: int) -> int:
            return x + 1

        def first(graph, fireable):
            return fireable[0] if fireable else None

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
        firings = (
            (ExecutableGraphOperations.fire_one_transition, {
                "executable_graph": graph, "place_names_to_nodes": places,
                "transition_names_to_incoming_edges": incoming, "transition_names_to_outgoing_edges": outgoing,
                "transition_names_to_read_edges": reads,
            }),
            (ExecutableGraphOperations.execute_graph, {"executable_graph": graph}),
        )
        hold = ClosedNets.hold(graph)
        try:
            for function, arguments in firings:
                with pytest.raises(HeldByBodyError):
                    asyncio.run(function(**arguments, transition_selector=first))
                with pytest.raises(HeldByBodyError):
                    asyncio.run(function(*positionally(function, {**arguments, "transition_selector": first})))
                asyncio.run(function(*positionally(function, {**arguments, "transition_selector": None})))
        finally:
            ClosedNets.unhold(hold)
        assert tokens(graph, "A") == [] and tokens(graph, "B") == [2], "the step without a selector fired"


# --- the whole-state acts --------------------------------------------------------------------------------------


# Each way to load a whole state into a net: by its marking, through the graph and
# through ``NetJson``, and by a saved form.
LOADS = {
    "import_marking": lambda net: net.import_marking({"Inbox": ["Letter"], "Ledger": ["Forgery"]}),
    "NetJson.import_marking": lambda net: NetJson.import_marking(net, {"Inbox": ["Letter"], "Ledger": ["Forgery"]}),
    "load_into": lambda net: NetJson.load_into(net, NetJson.save(post(ledger=("Forgery",)))),
}


def holding_nets_inside(open: bool = True) -> ExecutableGraph:
    """A net a body may hold, with a post carried by a token of its own and a post
    in a subnet place."""
    return construct([
        ListPlaceNode("Sacks", ExecutableGraph, [post()], nested=True, stepping="passive"),
        SubnetPlaceNode("Branch", post()),
    ], open=open)


class TestTheWholeStateActs:
    """Loading a marking or a saved form replaces the tokens of every place, ports
    an edge does not name among them: the program's alone."""

    @pytest.mark.parametrize("load", LOADS.values(), ids=LOADS)
    @pytest.mark.parametrize("open", [False, True], ids=["closed", "open"])
    def test_a_body_may_not_load_a_net_it_holds(self, load, open):
        unit = post(open=open)
        refusal = fails_with(office(MutateEdge("Posts", "Use", "unit", reaches=("Inbox",)), body_doing(load, False),
                                    unit), HeldByBodyError).__cause__
        assert refusal.act.startswith("loading a") and refusal.net == "Test Post"
        assert tokens(unit, "Inbox") == [] and tokens(unit, "Ledger") == [], "nothing was loaded"

    @pytest.mark.parametrize("load", LOADS.values(), ids=LOADS)
    def test_nor_a_net_inside_one(self, load):
        """A post a token of the held net carries, and its subnet, read directly or
        through ``subnet_scope``, the held net declared open."""
        def in_scope(net):
            with NestedNets.subnet_scope(net, "Branch") as branch:
                load(branch)

        for reach in (lambda net: load(tokens(net, "Sacks")[0]), lambda net: load(net.subnet("Branch")), in_scope):
            unit = holding_nets_inside()
            fails_with(office(MutateEdge("Posts", "Use", "unit"), body_doing(reach, False), unit), HeldByBodyError)
            assert tokens(tokens(unit, "Sacks")[0], "Ledger") == [] and tokens(unit.subnet("Branch"), "Ledger") == []

    @pytest.mark.parametrize("load", LOADS.values(), ids=LOADS)
    def test_the_program_loads_the_nets_the_body_could_not_when_the_net_around_them_is_declared_open(self, load):
        """A load writes every place of a net, so a closed net shields the nets
        inside it from one as from a put (since 2026-11-27): the program loads the
        net around as a whole, or the net around is declared open."""
        for unit_open in (False, True):
            unit = holding_nets_inside(open=unit_open)
            # Kept, and open with the unit: every net around the posts counts.
            around = office(MutateEdge("Posts", "Use", "unit"),
                            body_doing(lambda net: load(net.subnet("Branch")), False), unit, open=unit_open)
            fails_with(around, HeldByBodyError)
            assert tokens(around, "Posts") == [unit], "the failed firing put the unit back"
            sack = tokens(unit, "Sacks")[0]

            def in_scope(unit=unit):
                with NestedNets.subnet_scope(unit, "Branch") as branch:
                    load(branch)

            for reach in (lambda sack=sack: load(sack), in_scope):
                if unit_open:
                    reach()
                else:
                    with pytest.raises(ClosedNetError) as refused:
                        reach()
                    assert refused.value.place is None and refused.value.around == f"#{unit.net_id}"
                    assert "Load" in str(refused.value) and "as a whole" in str(refused.value)
            filed = ["Forgery"] if unit_open else []
            assert tokens(sack, "Ledger") == tokens(unit.subnet("Branch"), "Ledger") == filed


class TestWhoIsRefusedAndWhoIsFailedForIt:

    def test_a_body_sets_no_knob_of_a_net_inside_the_net_it_holds(self):
        """As it writes no port there: a post a token of the held net carries, and
        the held net's subnet."""
        for reach in (lambda net: tokens(net, "Sacks")[0], lambda net: net.subnet("Branch")):
            unit = holding_nets_inside()
            refusal = fails_with(office(MutateEdge("Posts", "Use", "unit"), body_doing(
                lambda net, reach=reach: NestedNets.set_param(reach(net), "speed", 9), False), unit),
                HeldByBodyError).__cause__
            assert refusal.holder == "inside" and "inside a net that the running body holds" in str(refusal)
            assert reach(unit).params["speed"] == 1, "nothing was set"

    def test_the_program_is_refused_while_a_body_holds_the_net_and_the_body_is_not_failed_for_it(self):
        """A knob or a load from the program while a body holds the net would be
        found as the body's own change when it returns, so the program is refused,
        told to wait for the landing, and the refusal is the program's alone: the
        firing lands. Afterwards the program has the net again."""
        async def go():
            entered, gate = asyncio.Event(), asyncio.Event()

            async def wait(net):
                entered.set()
                await gate.wait()

            unit = post()
            # Declared open, so that once the firing has landed the program loads the
            # post where it rests: a closed office would shield it.
            graph = office(MutateEdge("Posts", "Use", "unit"), body_doing(wait, False, asynchronous=True), unit,
                           open=True)
            firing = asyncio.ensure_future(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(HeldByBodyError) as knob:
                NestedNets.set_param(unit, "speed", 9)
            with pytest.raises(HeldByBodyError) as load:
                NetJson.import_marking(unit, {"Ledger": ["Forgery"]})
            assert knob.value.holder == load.value.holder == "other"
            assert load.value.whole_state and not knob.value.whole_state
            assert "until its firing lands" in str(knob.value) and "loading replaces the tokens" in str(load.value)
            gate.set()
            await asyncio.wait_for(firing, 5)
            assert tokens(graph, "Done") == ["Letter"], "the body's firing landed"
            assert unit.params["speed"] == 1 and tokens(unit, "Ledger") == []
            NestedNets.set_param(unit, "speed", 9)
            NetJson.import_marking(unit, {"Ledger": ["Filed"]})
            assert unit.params["speed"] == 9 and tokens(unit, "Ledger") == ["Filed"]
        asyncio.run(go())

    @pytest.mark.parametrize("control", [
        ControlSpec.inject("Inbox", lambda _clicked: "Letter", "Post a letter"),
        ControlSpec.inject("Ledger", lambda _clicked: "Entry", "Add an entry"),
        ControlSpec("speed", "slider", "Speed", lambda value: SetNetParam("speed", value)),
        ControlSpec.fire("File", "File a letter"),
        ControlSpec.enable("File", "Filing"),
    ], ids=["inject at a port", "inject at its own place", "set a parameter", "fire", "switch"])
    @pytest.mark.parametrize("how", ["apply_control", "by_control"])
    @pytest.mark.parametrize("strict", [True, False], ids=["strict", "not strict"])
    def test_the_program_is_refused_a_press_while_a_body_holds_the_net_and_the_body_is_not_failed_for_it(
        self, control, how, strict,
    ):
        """Since 2026-11-28, as a knob is: pressed with ``apply_control`` it is
        refused at once; given ``by_control`` and drained, it is refused as a knob's
        command is, raised under ``strict`` and dropped otherwise. Until then the
        press landed, and the body that held the net was failed for it when it
        returned. Once the firing has landed the press lands."""
        async def go():
            entered, gate = asyncio.Event(), asyncio.Event()

            async def wait(net):
                entered.set()
                await gate.wait()

            # A letter waits in the post, which its passive place leaves to the program,
            # so that the press of ``File`` fires once the firing has landed.
            unit = post(steps=True, inbox=("Waiting",))
            graph = construct([
                ListPlaceNode("Posts", ExecutableGraph, [unit], nested=True, stepping="passive"),
                ListPlaceNode("Orders", str, ["Letter"]), ListPlaceNode("Done", str),
                FunctionTransitionNode("Use", body_doing(wait, False, asynchronous=True)),
                ArgumentEdgeToTransition("Orders", "Use", "order"), MutateEdge("Posts", "Use", "unit"),
                ReturnedEdgeFromTransition("Use", "Done"),
            ])
            firing = asyncio.ensure_future(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
            await asyncio.wait_for(entered.wait(), 5)

            def press() -> int:
                ctx = RunContext(graph=unit, inbox=asyncio.Queue(), strict=strict)
                if how == "apply_control":
                    apply_control(ctx, control, 7)
                else:
                    ctx.inbox.put_nowait(by_control(ControlSpec.command(control, 7, ctx.graph)))
                return Runner.drain_inbox(ctx)

            before = (tokens(unit, "Inbox"), tokens(unit, "Ledger"), dict(unit.params))
            if how == "apply_control" or strict:
                with pytest.raises(HeldByBodyError) as refused:
                    press()
                assert refused.value.holder == "other" and "Press it again once the firing has landed" in str(
                    refused.value)
            else:
                assert press() == 0, "dropped and logged"
            assert (tokens(unit, "Inbox"), tokens(unit, "Ledger"), dict(unit.params)) == before
            gate.set()
            await asyncio.wait_for(firing, 5)
            assert tokens(graph, "Done") == ["Letter"], "the body's firing landed"
            assert press() == 1, "the press lands once the firing has"
        asyncio.run(go())


def shred_it(letter: str) -> str:
    return letter


def two_desk_post() -> ExecutableGraph:
    """A post whose letters ``File`` and ``Shred`` take in turn (round robin), each
    into the ledger. It starts with none, so that the office around it, which steps
    its posts first, comes to ``Use``."""
    return construct([
        ListPlaceNode("Inbox", str, port=True),
        ListPlaceNode("Ledger", str),
        FunctionTransitionNode("File", file_it), FunctionTransitionNode("Shred", shred_it),
        ArgumentEdgeToTransition("Inbox", "File", "letter"), ReturnedEdgeFromTransition("File", "Ledger"),
        ArgumentEdgeToTransition("Inbox", "Shred", "letter"), ReturnedEdgeFromTransition("Shred", "Ledger"),
    ], params={"speed": 1}, template="Test Post", tie_break="round_robin")


def caught(action):
    """``action``, its refusal caught, as a body that tries to carry on would."""
    def attempt(net):
        try:
            action(net)
        except HeldByBodyError:
            pass
    return attempt


def posting_two_then(action):
    """A body that posts two letters at the held post's ``Inbox``, which its edge
    reaches, and then awaits ``action`` on the post."""
    async def body(net):
        NestedNets.put(net, "Inbox", ["First", "Second"])
        await action(net)
    return body


def holding_posts_inside() -> ExecutableGraph:
    """``holding_nets_inside`` with posts that have ``File`` of their own."""
    return construct([
        ListPlaceNode("Sacks", ExecutableGraph, [post(steps=True)], nested=True, stepping="passive"),
        SubnetPlaceNode("Branch", post(steps=True)),
    ], open=True)


# Each net a body reaches through the net it holds: the net itself, a post a token
# of it carries and its subnet.
REACHES = {
    "held": lambda net: net,
    "carried": lambda net: tokens(net, "Sacks")[0],
    "subnet": lambda net: net.subnet("Branch"),
}


class TestWhatElseABodyLeavesToTheProgram:
    """Since 2026-11-28 a body leaves three more things of a net it holds, or of a
    net inside one, to the program and the runner: its events, its tie-break cursor
    and its controls. Each refusal is a ``HeldByBodyError`` that fails the firing
    when the body returns, also when the body caught it, as a knob's does; the
    engine's own moves, a step the body makes included, are as before. And a body
    that drives a net it holds is held to the same rule in both drivers when its
    ``RunContext`` switches transitions off."""

    @pytest.mark.parametrize("reach", REACHES.values(), ids=REACHES)
    def test_a_body_drains_no_events_of_a_net_it_holds_nor_of_one_inside_it(self, reach):
        unit = holding_nets_inside() if reach is not REACHES["held"] else post()
        graph = office(MutateEdge("Posts", "Use", "unit"), body_doing(caught(lambda net: reach(net).drain_events()),
                                                                      False), unit, record=True)
        refusal = fails_with(graph, HeldByBodyError).__cause__
        assert refusal.act == "draining its events" and "the log of the net around it" in str(refusal)

    def test_the_events_of_a_step_the_body_makes_reach_the_log_of_the_net_around(self):
        """The body steps the post it holds, and its try at draining the post's
        events leaves them for the office's log, which the program drains."""
        async def step(net):
            await ExecutableGraphOperations.execute_graph(net, stop_after_n_firings=None)
            caught(lambda net: net.drain_events())(net)

        unit = post(steps=True)
        graph = office(MutateEdge("Posts", "Use", "unit", reaches=("Inbox",)),
                       body_doing(posting_two_then(step), False, asynchronous=True), unit, record=True)
        fails_with(graph, HeldByBodyError)
        nested = [event.event for event in graph.drain_events() if type(event).__name__ == "Nested"]
        assert [type(event).__name__ for event in nested] == ["Added", "Added", "Fired", "Fired"]
        assert tokens(unit, "Ledger") == ["First", "Second"]

    @pytest.mark.parametrize("reach", REACHES.values(), ids=REACHES)
    @pytest.mark.parametrize("act", ["select", "forget"])
    def test_a_body_moves_no_tie_break_cursor_of_a_net_it_holds_without_firing_it(self, reach, act):
        def move(net):
            net = reach(net)
            if act == "select":
                default_transition_selector(net, list(net.transitions))
            else:
                net.forget_cursors()

        unit = holding_nets_inside() if reach is not REACHES["held"] else two_desk_post()
        reach(unit)._last_pick = 0
        refusal = fails_with(office(MutateEdge("Posts", "Use", "unit"), body_doing(caught(move), False), unit),
                             HeldByBodyError).__cause__
        assert "tie-break cursor" in str(refusal) and reach(unit)._last_pick == 0, "the cursor did not move"

    def test_a_step_the_body_makes_still_chooses_with_the_default_selector_and_moves_its_cursor(self):
        async def step_twice(net):
            await ExecutableGraphOperations.execute_graph(net, stop_after_n_firings=2)

        unit = two_desk_post()
        run(office(MutateEdge("Posts", "Use", "unit", reaches=("Inbox",)),
                   body_doing(posting_two_then(step_twice), False, asynchronous=True), unit))
        assert unit.fired_counts == {"File": 1, "Shred": 1}, "round robin, by the net's own choice"
        assert unit._last_pick == 1

    @pytest.mark.parametrize("reach", REACHES.values(), ids=REACHES)
    @pytest.mark.parametrize("control", [
        ControlSpec.inject("Inbox", lambda _clicked: "Letter", "Post a letter"),
        ControlSpec.inject("Ledger", lambda _clicked: "Forgery", "Forge an entry"),
        ControlSpec("speed", "slider", "Speed", lambda value: SetNetParam("speed", value)),
        ControlSpec.fire("File", "File a letter"),
        ControlSpec.enable("File", "Filing"),
    ], ids=["inject at a port", "inject at its own place", "set a parameter", "fire", "switch"])
    @pytest.mark.parametrize("how", ["apply_control", "by_control"])
    @pytest.mark.parametrize("strict", [True, False], ids=["strict", "not strict"])
    def test_a_body_presses_no_control_of_a_net_it_holds_nor_of_one_inside_it(self, reach, control, how, strict):
        """Pressed with ``apply_control``, or given ``by_control`` and drained; at a
        port the edge names too. The program and the viewer keep their presses
        (``TestTheRuntimesCommands``)."""
        def press(net):
            ctx = RunContext(graph=reach(net), inbox=asyncio.Queue(), strict=strict)
            if how == "apply_control":
                apply_control(ctx, control, 7)
            else:
                ctx.inbox.put_nowait(by_control(ControlSpec.command(control, 7, ctx.graph)))
            Runner.drain_inbox(ctx)

        unit = holding_posts_inside() if reach is not REACHES["held"] else post(steps=True)
        reached = reach(unit)
        graph = office(MutateEdge("Posts", "Use", "unit", reaches=("Inbox",)), body_doing(caught(press), False), unit)
        refusal = fails_with(graph, HeldByBodyError).__cause__
        assert "its controls are pressed by the program and the viewer" in str(refusal)
        assert tokens(reached, "Inbox") == tokens(reached, "Ledger") == [] and reached.params["speed"] == 1

    @pytest.mark.parametrize("mode", [ExecutionMode.SEQUENTIAL, ExecutionMode.CONCURRENT], ids=["sequential", "concurrent"])
    def test_both_drivers_refuse_a_body_a_run_of_a_net_it_holds_with_transitions_switched_off(self, mode):
        """Switched off in a context, transitions choose which of the rest fires,
        which is firing by name; until 2026-11-28 the sequential driver refused it
        as a selector of the caller's own and the concurrent one let the switches
        choose. Without them both drivers step the net the same way."""
        def driving(disabled: set):
            async def drive(net):
                await Runner.run(RunContext(graph=net, mode=mode, disabled=set(disabled)))
            return body_doing(posting_two_then(drive), False, asynchronous=True)

        edge = MutateEdge("Posts", "Use", "unit", reaches=("Inbox",))
        unit = two_desk_post()
        refusal = fails_with(office(edge, driving({"File"}), unit), HeldByBodyError).__cause__
        assert refusal.act == "a run with transitions its RunContext switches off" and refusal.holder == "body"
        assert unit.fired_counts == {} and tokens(unit, "Ledger") == []
        unit = two_desk_post()
        run(office(edge, driving(set()), unit))
        assert unit.fired_counts == {"File": 1, "Shred": 1} and sorted(tokens(unit, "Ledger")) == ["First", "Second"]

    @pytest.mark.parametrize("mode", [ExecutionMode.SEQUENTIAL, ExecutionMode.CONCURRENT], ids=["sequential", "concurrent"])
    def test_both_drivers_refuse_the_program_such_a_run_while_a_body_holds_the_net(self, mode):
        async def go():
            entered, gate = asyncio.Event(), asyncio.Event()

            async def wait(net):
                entered.set()
                await gate.wait()

            unit = two_desk_post()
            graph = office(MutateEdge("Posts", "Use", "unit"), body_doing(wait, False, asynchronous=True), unit)
            firing = asyncio.ensure_future(ExecutableGraphOperations.execute_graph(graph))
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(HeldByBodyError) as refused:
                await Runner.run(RunContext(graph=unit, mode=mode, disabled={"File"}))
            assert refused.value.holder == "other" and unit.fired_counts == {}
            gate.set()
            await asyncio.wait_for(firing, 5)
            assert tokens(graph, "Done") == ["Letter"], "the body is not failed for the program's refusal"
        asyncio.run(go())


# --- the nets inside a closed net ------------------------------------------------------------------------


def sorting_office(sacks: Optional[list] = None, branch: Optional[ExecutableGraph] = None, open: bool = False,
                   template: Optional[str] = "Test Office") -> ExecutableGraph:
    """A sorting office: ``Desk``, a port; ``Sacks``, a port of nets at rest
    (passive) holding ``sacks``; and ``Branch``, a subnet place holding ``branch``;
    a new post each by default. ``open`` declares the office open."""
    return construct([
        ListPlaceNode("Desk", str, port=True),
        ListPlaceNode("Sacks", ExecutableGraph, [post()] if sacks is None else sacks, nested=True, stepping="passive",
                      port=True),
        SubnetPlaceNode("Branch", post() if branch is None else branch),
    ], open=open, template=template)


def inside(net: ExecutableGraph) -> dict:
    """The nets inside a sorting office, by how the program reaches them: the post
    in its first sack and the post of its subnet place."""
    return {"sack": tokens(net, "Sacks")[0], "branch": net.subnet("Branch")}


# How the program writes a port of a net, put or take.
ACTS = {
    "put": lambda net: NestedNets.put(net, "Inbox", ["Letter"]),
    "take": lambda net: NestedNets.take(net, "Inbox"),
}


def carry(sack: ExecutableGraph) -> ExecutableGraph:
    return sack


@petri_net(name="sorting-office", description="A closed office with a post in a sack and one in a subnet place")
def closed_sorting_office() -> ExecutableGraph:
    return sorting_office()


@petri_net(name="open-sorting-office", description="The office declared open")
def open_sorting_office() -> ExecutableGraph:
    return sorting_office(open=True)


class Connection:
    """The protocol runner's owner, with ``factory``'s net to load."""

    def __init__(self, factory):
        self.owner = Owner(Registry.from_factories([factory]))
        self.next_id = 0

    async def send(self, method: str, params: Optional[dict] = None) -> dict:
        """The answer to one request, once every launch it made has landed."""
        self.next_id += 1
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        out = await self.owner.handle({"id": str(self.next_id), "method": method, "params": params or {}}, now)
        lines = list(out.lines)
        while out.launch is not None:
            out = await self.owner.completed(await out.launch.coroutine, now)
            lines.extend(out.lines)
        return lines[-1]


class TestAClosedNetShieldsTheNetsInsideIt:
    """Since 2026-11-27 a closed net shields the nets inside it, a subnet or a net a
    token carries, at any depth, from the program and the runner, so that its
    drawing shows every way a token comes in: a write there is refused at a port
    too, its ``around`` the nearest closed net around, unless every net around is
    declared open; the inner net's own rule applies on top. A net is inside the
    net whose place the engine last saw it come to rest in, while that place
    still holds it. Bodies and the engine's own moves are not affected."""

    @pytest.mark.parametrize("act", ACTS)
    def test_a_port_of_a_subnet_takes_nothing_through_subnet_scope_or_through_the_subnet(self, act):
        world = sorting_office(branch=post(inbox=("Waiting",)))
        with NestedNets.subnet_scope(world, "Branch") as branch:
            with pytest.raises(ClosedNetError) as scoped:
                ACTS[act](branch)
        with pytest.raises(ClosedNetError) as direct:
            ACTS[act](world.subnet("Branch"))
        for refused in (scoped, direct):
            assert (refused.value.net, refused.value.place, refused.value.act, refused.value.around) == (
                "Test Post", "Inbox", f"a {act}", "Test Office")
        assert tokens(world.subnet("Branch"), "Inbox") == ["Waiting"], "nothing was put or taken"

    @pytest.mark.parametrize("act", ACTS)
    def test_nor_does_a_port_of_a_net_a_token_carries_resting_in_a_port_of_the_closed_net(self, act):
        """``Sacks`` is a port of the office: a token may come to rest there from
        outside, and the net it carries is inside the office all the same."""
        sack = post(inbox=("Waiting",))
        world = sorting_office(sacks=[sack])
        with pytest.raises(ClosedNetError) as refused:
            ACTS[act](sack)
        assert refused.value.around == "Test Office" and tokens(sack, "Inbox") == ["Waiting"]
        assert tokens(world, "Sacks") == [sack]

    @pytest.mark.parametrize("command", [Extend, SetTokens], ids=["Extend", "SetTokens"])
    @pytest.mark.parametrize("strict", [True, False], ids=["strict", "not strict"])
    def test_extend_and_set_tokens_on_a_context_of_the_net_inside_are_refused_strict_or_not(self, command, strict):
        world = sorting_office(sacks=[post(inbox=("Waiting",))], branch=post(inbox=("Waiting",)))
        for name, net in inside(world).items():
            ctx = RunContext(graph=net, inbox=asyncio.Queue(), strict=strict)
            with pytest.raises(ClosedNetError, match="an Extend|a SetTokens") as refused:
                drained(ctx, command("Inbox", ["Letter"]))
            assert refused.value.around == "Test Office" and tokens(net, "Inbox") == ["Waiting"], name

    @pytest.mark.parametrize("top_open, middle_open, around", [
        (False, True, "Test Top"), (True, False, "Test Middle"), (False, False, "Test Middle"), (True, True, None),
    ], ids=["closed top, open middle", "open top, closed middle", "both closed", "both open"])
    def test_two_levels_down_every_net_around_must_be_declared_open(self, top_open, middle_open, around):
        """The top carries the middle net in a token, and the middle holds the post
        in a subnet place. A refusal names the nearest closed net around. A port
        pair on the middle is a write at the middle: a closed middle refuses it by
        its own rule, and an open one as the nets around it say."""
        bottom = post()
        middle = construct([ListPlaceNode("Hall", str, port=True), SubnetPlaceNode("Branch", bottom)],
                           open=middle_open, template="Test Middle")
        top = construct([ListPlaceNode("Floors", ExecutableGraph, [middle], nested=True, stepping="passive")],
                        open=top_open, template="Test Top")
        if around is None:
            NestedNets.put(bottom, "Inbox", ["Letter"])
            NestedNets.put(middle, Port("Branch", "Inbox"), ["By the pair"])
            assert tokens(bottom, "Inbox") == ["Letter", "By the pair"]
        else:
            with pytest.raises(ClosedNetError) as refused:
                NestedNets.put(bottom, "Inbox", ["Letter"])
            assert refused.value.around == around
            with pytest.raises(ClosedNetError) as refused:
                NestedNets.put(middle, Port("Branch", "Inbox"), ["By the pair"])
            assert refused.value.around == ("Test Top" if middle_open else None)
            assert tokens(bottom, "Inbox") == []
        assert tokens(top, "Floors") == [middle]

    def test_an_open_net_around_lets_the_write_land_at_the_port_and_the_inner_nets_own_rule_still_refuses_its_own_place(
            self):
        world = sorting_office(open=True)
        for name, net in inside(world).items():
            NestedNets.put(net, "Inbox", ["Letter"])
            assert NestedNets.take(net, "Inbox") == ["Letter"], name
            with pytest.raises(ClosedNetError) as refused:
                NestedNets.put(net, "Ledger", ["Forgery"])
            error = refused.value
            assert error.around is None and not error.deeper and not error.undeclared, name
            assert REMEDY in str(error) and tokens(net, "Ledger") == []

    def test_inside_a_closed_net_the_inner_nets_own_rule_is_judged_first(self):
        """At a place the inner net would refuse anyway, the refusal is its own, and
        says to mark the place as a port or to declare that net open."""
        world = sorting_office()
        for name, net in inside(world).items():
            with pytest.raises(ClosedNetError) as refused:
                NestedNets.put(net, "Ledger", ["Forgery"])
            assert refused.value.around is None and REMEDY in str(refused.value), name

    def test_the_refusal_names_both_nets_and_says_how_to_put_it_right(self):
        world = sorting_office()
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(inside(world)["sack"], "Inbox", ["Letter"])
        error = refused.value
        assert (error.net, error.place, error.act, error.around) == ("Test Post", "Inbox", "a put", "Test Office")
        assert not error.undeclared and not error.deeper and isinstance(error, ValueError)
        assert str(error) == (
            "a put at place 'Inbox' of net 'Test Post' is refused: the net is inside net 'Test Office', which is "
            "closed, so from outside 'Test Office' nothing inside it is written. Put the token at a port of "
            "'Test Office' and let one of its transitions carry it in, or declare 'Test Office' open."
        )
        unnamed = sorting_office(template=None)
        with pytest.raises(ClosedNetError, match=f"inside net '#{unnamed.net_id}'"):
            NestedNets.put(inside(unnamed)["branch"], "Inbox", ["Letter"])

    def test_a_net_the_program_took_out_of_its_place_is_written_again_and_shielded_once_put_back(self):
        sack = post()
        world = sorting_office(sacks=[sack])
        assert NestedNets.take(world, "Sacks") == [sack], "a port of the office"
        NestedNets.put(sack, "Inbox", ["Letter"])
        NestedNets.put(world, "Sacks", [sack])
        with pytest.raises(ClosedNetError):
            NestedNets.put(sack, "Inbox", ["Another"])
        assert tokens(sack, "Inbox") == ["Letter"]

    def test_a_net_a_firing_moves_to_another_place_of_the_closed_net_is_still_inside_it(self):
        sack = post()
        world = construct([
            ListPlaceNode("Sacks", ExecutableGraph, [sack], nested=True, stepping="passive"),
            ListPlaceNode("Sent", ExecutableGraph, nested=True, stepping="passive"),
            FunctionTransitionNode("Send", carry),
            ArgumentEdgeToTransition("Sacks", "Send", "sack"),
            ReturnedEdgeFromTransition("Send", "Sent"),
        ], template="Test Office")
        run(world)
        assert tokens(world, "Sent") == [sack]
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(sack, "Inbox", ["Letter"])
        assert refused.value.around == "Test Office"

    def test_a_net_a_firing_moves_into_another_net_is_judged_by_that_net(self):
        """An open depot locks the sack into its closed vault's bay, by the pair, and
        takes it out again: the program writes the sack before and after, and not
        while it rests in the vault."""
        sack = post()
        vault = construct([ListPlaceNode("Bay", ExecutableGraph, nested=True, stepping="passive", port=True)],
                          template="Test Vault")
        depot = construct([
            ListPlaceNode("Sacks", ExecutableGraph, [sack], nested=True, stepping="passive"),
            SubnetPlaceNode("Vault", vault),
            ListPlaceNode("Out", ExecutableGraph, nested=True, stepping="passive"),
            FunctionTransitionNode("Lock", carry),
            ArgumentEdgeToTransition("Sacks", "Lock", "sack"),
            ReturnedEdgeFromTransition("Lock", Port("Vault", "Bay")),
            FunctionTransitionNode("Unlock", carry),
            ArgumentEdgeToTransition(Port("Vault", "Bay"), "Unlock", "sack"),
            ReturnedEdgeFromTransition("Unlock", "Out"),
        ], open=True)
        NestedNets.put(sack, "Inbox", ["Before"])
        assert run(depot).last_fired == "Lock" and tokens(vault, "Bay") == [sack]
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(sack, "Inbox", ["In the vault"])
        assert refused.value.around == "Test Vault"
        assert run(depot).last_fired == "Unlock" and tokens(depot, "Out") == [sack]
        NestedNets.put(sack, "Inbox", ["After"])
        assert tokens(sack, "Inbox") == ["Before", "After"]

    def test_a_net_in_flight_held_by_a_body_is_shielded_until_the_firing_lands(self):
        """The body took the post from the office, so it rests in no place; it is
        inside the office until the firing lands, and inside nothing afterwards,
        the firing having put it nowhere."""
        async def go():
            entered, gate = asyncio.Event(), asyncio.Event()

            async def wait(net):
                entered.set()
                await gate.wait()

            unit = post()
            graph = office(ArgumentEdgeToTransition("Posts", "Use", "unit"), body_doing(wait, False, asynchronous=True),
                           unit)
            firing = asyncio.ensure_future(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
            await asyncio.wait_for(entered.wait(), 5)
            assert tokens(graph, "Posts") == [] and unit.held_by_body
            with pytest.raises(ClosedNetError) as refused:
                NestedNets.put(unit, "Inbox", ["Letter"])
            assert refused.value.around == f"#{graph.net_id}"
            gate.set()
            await asyncio.wait_for(firing, 5)
            assert tokens(graph, "Done") == ["Letter"], "the body's firing landed"
            NestedNets.put(unit, "Inbox", ["Letter"])
            assert tokens(unit, "Inbox") == ["Letter"]
        asyncio.run(go())

    def test_the_nets_inside_a_deep_copy_and_a_fork_are_inside_the_copy(self):
        """And a copy of the inner net alone rests in no place."""
        world = sorting_office(template=None)
        for copy in (deepcopy(world), NestedNets.fork(world)):
            for name, net in inside(copy).items():
                assert net is not inside(world)[name]
                with pytest.raises(ClosedNetError) as refused:
                    NestedNets.put(net, "Inbox", ["Letter"])
                assert refused.value.around == f"#{copy.net_id}", name
        loose = deepcopy(inside(world)["sack"])
        NestedNets.put(loose, "Inbox", ["Letter"])
        assert tokens(loose, "Inbox") == ["Letter"] and tokens(inside(world)["sack"], "Inbox") == []

    def test_the_nets_inside_a_net_restored_or_loaded_with_net_json_are_inside_it(self):
        Templates.register("Test Post", post)
        Templates.register("Test Office", sorting_office)
        try:
            saved = NetJson.save(sorting_office(sacks=[post(inbox=("Waiting",))]))
            restored = NetJson.restore(saved)
            loaded = NetJson.load_into(sorting_office(sacks=[], template=None), saved)
            for net, around in ((restored, "Test Office"), (loaded, f"#{loaded.net_id}")):
                sack = inside(net)["sack"]
                assert tokens(sack, "Inbox") == ["Waiting"] and Containment.around(sack) is net
                with pytest.raises(ClosedNetError) as refused:
                    NestedNets.take(sack, "Inbox")
                assert refused.value.around == around
        finally:
            Templates.forget("Test Post")
            Templates.forget("Test Office")

    def test_a_net_put_into_a_places_list_directly_is_shielded_once_an_engine_entry_point_re_ids_the_place(self):
        """Until then it is not seen, as a token put there so has no id until then
        (``TokenIds``): which net a net is inside is noted where tokens get their
        ids. The write that lands meanwhile is pinned here, so that a change to the
        gap is noticed."""
        sack = post()
        world = sorting_office(sacks=[])
        world.place("Sacks").tokens.append(sack)
        NestedNets.put(sack, "Inbox", ["Unseen"])
        NestedNets.put(world, "Desk", ["Memo"])  # an entry point: it re-ids the office's places
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(sack, "Inbox", ["Letter"])
        assert refused.value.around == "Test Office" and tokens(sack, "Inbox") == ["Unseen"]
        sack = post()
        world.place("Sacks").tokens.append(sack)
        run(world, firings=None)  # a step is one too, firing nothing
        with pytest.raises(ClosedNetError):
            NestedNets.put(sack, "Inbox", ["Letter"])

    def test_a_net_whose_outer_net_is_gone_is_inside_nothing(self):
        """The note on a net names the net around it by its id, and does not keep
        that net alive: once nothing else refers to it, no one can write it, and
        the net that was inside it is inside nothing."""
        sack = post()
        world = sorting_office(sacks=[sack])
        with pytest.raises(ClosedNetError):
            NestedNets.put(sack, "Inbox", ["Letter"])
        del world
        gc.collect()
        NestedNets.put(sack, "Inbox", ["Letter"])
        assert tokens(sack, "Inbox") == ["Letter"]

    def test_the_knobs_of_a_net_inside_stay_with_the_program_and_a_load_into_it_does_not(self):
        """The shield is on tokens. Setting a parameter puts no token anywhere, and
        is the program's on a net inside a closed net as on any net no body holds.
        Loading a marking or a saved form writes every place of the net, so it is
        refused as a put is (Oleg, 2026-11-27), and lands when the net around is
        declared open."""
        loads = {
            "import_marking": lambda net: NetJson.import_marking(net, {"Inbox": ["Loaded"], "Ledger": ["Loaded"]}),
            "the net's own import_marking": lambda net: net.import_marking({"Inbox": ["Loaded"], "Ledger": ["Loaded"]}),
            "load_into": lambda net: NetJson.load_into(net, NetJson.save(post(ledger=("Loaded",)))),
        }
        for how, load in loads.items():
            world = sorting_office()
            for name, net in inside(world).items():
                NestedNets.set_param(net, "speed", 3)
                with pytest.raises(ClosedNetError) as refused:
                    load(net)
                assert refused.value.around == "Test Office" and refused.value.place is None, (how, name)
                assert "loading" in refused.value.act and "'Test Office' as a whole" in str(refused.value)
                assert net.params["speed"] == 3 and tokens(net, "Ledger") == [], (how, name)
            world = sorting_office(open=True)
            for name, net in inside(world).items():
                load(net)
                assert tokens(net, "Ledger") == ["Loaded"], (how, name)

    def test_a_body_that_does_not_hold_the_net_is_refused_the_load_as_the_program_is(self):
        sack = post()
        office = sorting_office([sack])

        def meddle(order: str) -> str:
            NetJson.import_marking(sack, {"Ledger": [order]})
            return order

        other = construct([
            ListPlaceNode("Orders", str, ["Letter"]),
            ListPlaceNode("Done", str),
            FunctionTransitionNode("Meddle", meddle),
            ArgumentEdgeToTransition("Orders", "Meddle", "order"),
            ReturnedEdgeFromTransition("Meddle", "Done"),
        ], restore_tokens_on_failure=True)
        refusal = fails_with(other, ClosedNetError).__cause__
        assert refusal.around == "Test Office" and refusal.place is None
        assert tokens(sack, "Ledger") == [] and tokens(office, "Sacks") == [sack]

    def test_a_body_whose_edge_names_the_port_writes_the_net_it_holds_inside_a_closed_net(self):
        """The office is carried by a token of a closed top, and its ``Use`` fires
        as a deep step of the top: the body is governed by its edge's reach, not by
        the nets around, while the program is refused at the same port."""
        unit = post()
        graph = office(MutateEdge("Posts", "Use", "unit", reaches=("Inbox",)),
                       body_doing(lambda net: NestedNets.put(net, "Inbox", ["From the body"]), False), unit)
        top = construct([ListPlaceNode("Offices", ExecutableGraph, [graph], nested=True)], template="Test Top")
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(unit, "Inbox", ["From the program"])
        assert refused.value.around == f"#{graph.net_id}"
        fired = asyncio.run(ExecutableGraphOperations.fire_one_transition(top, *ExecutableGraphOperations.maps(top)))
        assert isinstance(fired, InnerFiring) and fired.transition == "Use"
        assert tokens(unit, "Inbox") == ["From the body"] and tokens(graph, "Done") == ["Letter"]

    @staticmethod
    def builder(build) -> ExecutableGraph:
        return construct([
            ListPlaceNode("Orders", str, ["Letter"]),
            ListPlaceNode("Built", ExecutableGraph, nested=True, stepping="passive"),
            FunctionTransitionNode("Build", build),
            ArgumentEdgeToTransition("Orders", "Build", "order"),
            ReturnedEdgeFromTransition("Build", "Built"),
        ], restore_tokens_on_failure=True)

    def test_a_body_writes_the_ports_of_a_net_it_built_and_inside_it_only_if_it_built_it_open(self):
        """A net a body builds is inside nothing, so its ports take the body's
        tokens. The nets inside it are inside a closed net unless the body built it
        open, and the body is outside that net as the program is. Once the firing
        has put the office into a place, the program is refused there too."""
        def build(order: str) -> ExecutableGraph:
            built = sorting_office(open=True, template="Open Office")
            NestedNets.put(built, "Desk", [order])
            NestedNets.put(inside(built)["sack"], "Inbox", [order])
            with NestedNets.subnet_scope(built, "Branch") as branch:
                NestedNets.put(branch, "Inbox", [order])
            return built

        graph = run(self.builder(build))  # kept: a net whose outer net is gone is inside nothing
        [built] = tokens(graph, "Built")
        assert tokens(built, "Desk") == ["Letter"]
        for name, net in inside(built).items():
            assert tokens(net, "Inbox") == ["Letter"], name
            with pytest.raises(ClosedNetError) as refused:
                NestedNets.put(net, "Inbox", ["From the program"])
            assert refused.value.around == f"#{graph.net_id}", "the net around the open office, which has no template"

        for reach in ("sack", "branch"):
            def build_closed(order: str, reach=reach) -> ExecutableGraph:
                built = sorting_office()
                NestedNets.put(built, "Desk", [order])
                NestedNets.put(inside(built)[reach], "Inbox", [order])
                return built

            refusal = fails_with(self.builder(build_closed), ClosedNetError).__cause__
            assert refusal.around == "Test Office" and refusal.place == "Inbox", reach

    @pytest.mark.parametrize("office_open", [False, True], ids=["closed office", "open office"])
    def test_a_body_that_reaches_a_net_by_a_closure_is_outside_the_nets_around_it_as_the_program_is(self, office_open):
        """No edge handed the body the sack, so it holds nothing: the office around
        the sack shields it from the body exactly as from the program."""
        sack = post()
        office = sorting_office([sack], open=office_open)  # kept: a net whose outer net is gone is inside nothing

        def meddle(order: str) -> str:
            NestedNets.put(sack, "Inbox", ["From a body"])
            return order

        other = construct([
            ListPlaceNode("Orders", str, ["Letter"]),
            ListPlaceNode("Done", str),
            FunctionTransitionNode("Meddle", meddle),
            ArgumentEdgeToTransition("Orders", "Meddle", "order"),
            ReturnedEdgeFromTransition("Meddle", "Done"),
        ], restore_tokens_on_failure=True)
        if office_open:
            run(other)
            assert tokens(sack, "Inbox") == ["From a body"]
        else:
            refusal = fails_with(other, ClosedNetError).__cause__
            assert refusal.around == "Test Office" and tokens(sack, "Inbox") == []
        assert tokens(office, "Sacks") == [sack]

    def test_a_net_loaded_by_pickle_is_a_new_net_and_the_nets_inside_it_are_inside_it(self):
        office = sorting_office()
        loaded = pickle.loads(pickle.dumps(office))
        assert loaded.net_id != office.net_id and not loaded.held_by_body
        for name, net in inside(loaded).items():
            with pytest.raises(ClosedNetError) as refused:
                NestedNets.put(net, "Inbox", ["Letter"])
            assert refused.value.around == "Test Office", name
        del office
        with pytest.raises(ClosedNetError):
            NestedNets.put(inside(loaded)["sack"], "Inbox", ["Letter"])
        NestedNets.put(loaded, "Desk", ["Letter"])
        assert tokens(loaded, "Desk") == ["Letter"]

    @pytest.mark.parametrize("how", ["a new subnet place", "the place's own graph"])
    def test_a_subnet_put_in_place_of_another_is_shielded_from_the_next_engine_entry_point(self, how):
        """Code that swaps a subnet does so behind the engine's back, as code that
        appends to a place's list does: the new subnet is known to be inside when
        the net next runs."""
        office = sorting_office()
        newcomer = post()
        if how == "a new subnet place":
            index = next(i for i, place in enumerate(office.places) if place.name == "Branch")
            office.places[index] = SubnetPlaceNode("Branch", newcomer)
            office.structure_changed()
        else:
            office.place_named("Branch").graph = newcomer
        run(office)
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(newcomer, "Inbox", ["Letter"])
        assert refused.value.around == "Test Office"

    def test_the_engines_own_deposit_into_a_subnets_port_lands_in_a_deep_step_too(self):
        """The world, closed, is carried by a token of a closed top; its ``Enter``
        puts the letter into its branch's port by the pair, as a deep step of the
        top."""
        branch = post()
        world = construct([
            ListPlaceNode("Door", str, ["Letter"]),
            SubnetPlaceNode("Branch", branch),
            FunctionTransitionNode("Enter", file_it),
            ArgumentEdgeToTransition("Door", "Enter", "letter"),
            ReturnedEdgeFromTransition("Enter", Port("Branch", "Inbox")),
        ], template="Test World")
        top = construct([ListPlaceNode("Worlds", ExecutableGraph, [world], nested=True)], template="Test Top")
        fired = asyncio.run(ExecutableGraphOperations.fire_one_transition(top, *ExecutableGraphOperations.maps(top)))
        assert isinstance(fired, InnerFiring) and fired.transition == "Enter"
        assert tokens(branch, "Inbox") == ["Letter"] and tokens(world, "Door") == []

    @pytest.mark.parametrize("open", [False, True], ids=["closed", "open"])
    def test_the_runners_inject_by_path_and_by_nested_id_lands_only_inside_an_open_net(self, open):
        """Into the post in a sack and the post in the subnet place, each addressed
        both ways: inside the closed office each answers ``closed_net``, naming the
        office, and nothing is added; inside the open one each lands."""
        async def go():
            connection = Connection(open_sorting_office if open else closed_sorting_office)
            name = "open-sorting-office" if open else "sorting-office"
            assert "result" in await connection.send("load", {"net": name})
            places = (await connection.send("get_state"))["result"]["marking"]["places"]
            [carried] = places["Sacks"]["tokens"][0]["nets"]
            subnet = places["Branch"]["subnet"]
            for net in (carried, subnet):
                for address in ({"path": net["path"]}, {"nested_id": net["nested_id"]}):
                    answer = await connection.send("inject", {"place": "Inbox", "token": "Letter", **address})
                    if open:
                        assert answer["result"]["status"] == "injected", address
                    else:
                        assert answer["error"]["code"] == "closed_net", address
                        assert answer["error"]["message"].startswith(
                            "an inject at place 'Inbox' of net 'Test Post' is refused: the net is inside net "
                            "'Test Office', which is closed")
                state = (await connection.send("get_state", {"nested_id": net["nested_id"]}))["result"]
                assert state["marking"]["places"]["Inbox"]["count"] == (2 if open else 0)
        asyncio.run(go())


class Courier(BaseModel):
    """A token that carries a net, its round."""

    name: str
    round: ExecutableGraph

    def __petritype_nets__(self) -> dict[str, ExecutableGraph]:
        return {"round": self.round}


def new_round(courier: Courier, order: str) -> str:
    """A courier starts a new round: the body puts a new net in the token it was lent."""
    courier.round = post(steps=True, inbox=("Parcel",))
    return order


def depot() -> ExecutableGraph:
    """A closed depot whose ``Restart`` gives the courier it is lent a new round."""
    return construct([
        ListPlaceNode("Couriers", Courier, [Courier(name="Ada", round=post(steps=True))], nested=True),
        ListPlaceNode("Orders", str, ["Restart"], port=True),
        ListPlaceNode("Done", str),
        FunctionTransitionNode("Restart", new_round),
        MutateEdge("Couriers", "Restart", "courier"),
        ArgumentEdgeToTransition("Orders", "Restart", "order"),
        ReturnedEdgeFromTransition("Restart", "Done"),
    ], record_events=True, template="Test Depot")


@petri_net(name="test-depot", description="A courier whose round a body replaces")
def depot_entry() -> ExecutableGraph:
    return depot()


class TestABodyReplacesTheNetOfATokenItWasLent:
    """A body may put a new net in a token it was lent through a mutate edge (D2,
    2026-11-28): the net is part of the token, as when a game respawns a character.
    The new net is told as a new net, with its own ``net_id``, the old id gone from
    the state, the log and the wire, and it is inside the place as the old one was,
    shielded by the closed net around (since 2026-11-28; until then the program
    wrote any port of it, nothing having noted where it rests)."""

    def test_in_the_engine_the_new_net_has_its_own_id_and_the_old_one_is_gone(self):
        graph = depot()
        courier = tokens(graph, "Couriers")[0]
        old = courier.round
        run(graph, firings=2)  # Restart, then the new round's File, the round stepped inner first
        new = courier.round
        assert new is not old and new.net_id != old.net_id
        assert NestedNets.find(graph, old.net_id) is None and NestedNets.find(graph, new.net_id)[1] is new
        nested = [event for event in graph.drain_events() if type(event).__name__ == "Nested"]
        assert [(event.nested_id, event.event.transition) for event in nested] == [(new.net_id, "File")]
        assert tokens(new, "Ledger") == ["Parcel"]

    def test_the_new_net_is_shielded_by_the_closed_net_around_it(self):
        graph = depot()
        run(graph)
        courier = tokens(graph, "Couriers")[0]
        assert Containment.around(courier.round) is graph
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(courier.round, "Inbox", ["Letter"])
        assert refused.value.around == "Test Depot"

    def test_on_the_wire_the_new_net_has_its_own_nested_id_and_the_old_one_answers_unknown_net(self):
        async def go():
            connection = Connection(depot_entry)
            assert "result" in await connection.send("load", {"net": "test-depot"})

            async def round_of_ada() -> dict:
                state = (await connection.send("get_state"))["result"]
                [carried] = state["marking"]["places"]["Couriers"]["tokens"][0]["nets"]
                return carried

            before = await round_of_ada()
            assert (await connection.send("step"))["result"]["transition"] == "Restart"
            after = await round_of_ada()
            assert after["nested_id"] != before["nested_id"] and after["path"] == before["path"]
            old = await connection.send("get_state", {"nested_id": before["nested_id"]})
            assert old["error"]["code"] == "unknown_net"
            new = (await connection.send("get_state", {"nested_id": after["nested_id"]}))["result"]
            assert new["marking"]["places"]["Inbox"]["count"] == 1
            stepped = (await connection.send("step"))["result"]
            assert stepped["nested_id"] == after["nested_id"] and stepped["transition"] == "File"
        asyncio.run(go())


# --- what the record of the knobs keeps --------------------------------------------------------------------


def file_keeping(letter: str, **knobs) -> str:
    return letter


def post_keeping(params: dict | None = None, kwargs: dict | None = None) -> ExecutableGraph:
    """A post with a ``File`` of its own, with ``params`` among its parameters and
    ``kwargs`` as the keywords of ``File``."""
    return construct([
        ListPlaceNode("Inbox", str, port=True),
        ListPlaceNode("Ledger", str),
        FunctionTransitionNode("File", file_keeping, kwargs=kwargs),
        ArgumentEdgeToTransition("Inbox", "File", "letter"),
        ReturnedEdgeFromTransition("File", "Ledger"),
    ], params={"speed": 1, **(params or {})}, template="Test Post")


# Where a knob's value is kept: how to make a post with it there, and how a body
# holding the post reaches it.
KNOBS = {
    "params": (lambda value: post_keeping(params={"knob": value}), lambda net: net.params["knob"]),
    "kwargs": (lambda value: post_keeping(kwargs={"knob": value}), lambda net: net.transition_named("File").kwargs["knob"]),
}


def held(unit: ExecutableGraph, action) -> ExecutableGraph:
    """The office with ``unit``, whose body does ``action`` to the post it holds."""
    return office(MutateEdge("Posts", "Use", "unit"), body_doing(action, False), unit)


class Opaque:
    """No ``==`` of its own: equal to itself alone."""

    def __init__(self):
        self.rate = 3


class EqualityRaises:
    def __eq__(self, other):
        raise RuntimeError("cannot compare")

    __hash__ = object.__hash__


class EqualityVague:
    """An ``==`` whose answer has no truth value, as a numpy array's has not."""

    def __eq__(self, other):
        return self

    def __bool__(self):
        raise ValueError("the truth value is ambiguous")

    __hash__ = object.__hash__


class Tally(list):
    """A subclass of a plain container, which the record keeps as a leaf."""


UNCHANGING = {
    "an object without equality": Opaque,
    "a random.Random": lambda: random.Random(1),
    "NaN": lambda: float("nan"),
    "an object whose equality raises": EqualityRaises,
    "an object whose equality is no bool": EqualityVague,
    "a lock, which cannot be deep-copied": threading.Lock,
    "all of them in plain containers": lambda: {
        "list": [Opaque(), float("nan"), random.Random(1)],
        "set": {EqualityRaises(), EqualityVague(), float("nan")},
        "tuple": (Opaque(), [EqualityVague()], frozenset({EqualityRaises()})),
        EqualityRaises(): {EqualityVague(): [float("nan")]},
    },
}


def ended(action, seconds: float = 30):
    """``action()`` run where a check that never ends cannot hang the suite: what
    it returns, or what it raises raised here."""
    outcome = []

    def attempt():
        try:
            outcome.append((True, action()))
        except BaseException as error:
            outcome.append((False, error))

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(seconds)
    assert outcome, f"still running after {seconds} seconds"
    [(returned, value)] = outcome
    if not returned:
        raise value
    return value


class TestTheRecordOfItsKnobs:

    @pytest.mark.parametrize("where", KNOBS)
    @pytest.mark.parametrize("make", UNCHANGING.values(), ids=UNCHANGING)
    def test_a_body_that_changes_nothing_never_fails_whatever_the_values_are(self, where, make):
        keep, _reach = KNOBS[where]
        unit = keep(make())
        graph = run(held(unit, lambda net: None))
        assert tokens(graph, "Done") == ["Letter"] and not unit.held_by_body

    @pytest.mark.parametrize("where", KNOBS)
    def test_a_numpy_array_is_no_change_while_it_is_the_same_array(self, where):
        numpy = pytest.importorskip("numpy")
        keep, _reach = KNOBS[where]
        graph = run(held(keep({"weights": numpy.zeros(3), "rows": [numpy.ones(2)]}), lambda net: None))
        assert tokens(graph, "Done") == ["Letter"]

    def test_an_append_to_a_list_in_its_parameters_is_found(self):
        unit = post_keeping(params={"tags": ["a"]})
        graph = office(ArgumentEdgeToTransition("Posts", "Use", "unit"),
                       body_doing(lambda net: net.params["tags"].append("sneaky"), False), unit)
        failure = fails_with(graph, HeldByBodyError)
        assert "a change by a body to its parameters" in str(failure.__cause__)
        assert unit.params["tags"] == ["a", "sneaky"], "what the body did stays done"

    @pytest.mark.parametrize("where", KNOBS)
    @pytest.mark.parametrize("make, edit", [
        (lambda: [{"limits": [1, 2]}], lambda knob: knob[0]["limits"].append(3)),
        (lambda: {"inner": {"a": 1}}, lambda knob: knob["inner"].update(b=2)),
        (lambda: {"inner": {"a": 1}}, lambda knob: knob["inner"].update(a=2)),
        (lambda: [(1, [2])], lambda knob: knob[0][1].pop()),
        (lambda: ({"a"},), lambda knob: knob[0].add("b")),
        (lambda: [frozenset({1}), [Opaque()]], lambda knob: knob[1].__setitem__(0, Opaque())),
    ], ids=[
        "a list in a dict in a list", "a key put in a dict in a dict", "a value set in a dict in a dict",
        "a list in a tuple in a list", "a set in a tuple", "an item replaced in a list in a list",
    ])
    def test_an_edit_in_place_of_a_plain_container_is_found_at_any_depth(self, where, make, edit):
        keep, reach = KNOBS[where]
        fails_with(held(keep(make()), lambda net: edit(reach(net))), HeldByBodyError)

    def test_a_container_nested_deeper_than_python_recurses_is_kept_whole(self):
        def nest(depth: int) -> tuple[list, list]:
            top = bottom = []
            for _ in range(depth):
                bottom.append([])
                bottom = bottom[0]
            return top, bottom

        top, _bottom = nest(5000)
        run(held(post_keeping(params={"knob": top}), lambda net: None))
        top, bottom = nest(5000)
        fails_with(held(post_keeping(params={"knob": top}), lambda net: bottom.append("deep")), HeldByBodyError)

    @pytest.mark.parametrize("where", KNOBS)
    def test_a_value_put_in_place_of_another_is_found_and_an_equal_one_is_not(self, where):
        keep, reach = KNOBS[where]

        def put(value):
            def act(net):
                (net.params if where == "params" else net.transition_named("File").kwargs)["knob"] = value
            return act

        for value in (2, "two", [1, 2, 3], (1, 2), Opaque()):
            fails_with(held(keep([1, 2]), put(value)), HeldByBodyError)
        for value in ([1, 2], [1.0, 2]):
            unit = keep([1, 2])
            run(held(unit, put(value)))
            assert reach(unit) is value, "an equal value is no change"

    @pytest.mark.parametrize("where", KNOBS)
    def test_a_change_inside_another_object_where_it_lies_goes_unseen_as_inside_a_token(self, where):
        keep, reach = KNOBS[where]

        def edit(knob):
            knob["opaque"].rate = 4
            knob["tally"].append(1)

        unit = keep({"opaque": Opaque(), "tally": Tally()})
        graph = run(held(unit, lambda net: edit(reach(net))))
        assert tokens(graph, "Done") == ["Letter"] and reach(unit)["opaque"].rate == 4 and reach(unit)["tally"] == [1]

    @pytest.mark.parametrize("where", KNOBS)
    def test_a_container_that_holds_itself_is_kept_round_the_cycle_and_the_check_ends(self, where):
        keep, reach = KNOBS[where]

        def cycle():
            loop, table = [], {}
            loop.append(loop)
            table["self"] = table
            table["loop"] = (loop,)
            return {"table": table, "loop": loop}

        graph = ended(lambda: run(held(keep(cycle()), lambda net: None)))
        assert tokens(graph, "Done") == ["Letter"]
        ended(lambda: fails_with(held(keep(cycle()), lambda net: reach(net)["loop"].append(1)), HeldByBodyError))
