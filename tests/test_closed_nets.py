"""Closed nets (since 2026-11-23): a net that marks a port is closed to everyone
outside it, unless its definition declares it open.

From outside a closed net, tokens are put, taken and replaced only at its ports:
a body of the net around it, by any edge that hands it the net, the program
through the library's own functions and the runtime's commands, and the runner's
``inject``, at the top level and by path. A write at a port of one of its subnet
places is judged by its own rule first. A body holding a net that marks a port
cannot set its knobs or fire it by name; the program, its drivers and the runner
keep them. Reading, stepping, the whole-state acts and a net's own controls stay
allowed. A refusal names the net, the place and the act, and says how to put it
right; for a body it fails the firing, with what is restored as in any failure.
"""

import asyncio
import logging
from copy import deepcopy

import pytest

import petritype.core.executable_graph_components as components
from petritype.core.events import Failed, Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ClosedNetError,
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
)
from petritype.core.keys import Filter, MutateWhereEdge, ReadWhereEdge, TakeWhereEdge
from petritype.core.serialisation import NetJson, Templates
from petritype.runtime import (
    ControlSpec, Disable, Enable, ExecutionMode, Extend, Fire, RunContext, Runner, SetNetParam, SetParam, SetTokens,
    apply_control,
)

construct = ExecutableGraphOperations.construct_graph
REMEDY = "mark the place as a port, start the net with the token in its definition, or declare the net open"


def file_it(letter: str) -> str:
    return letter


def post(open: bool = False, ports: bool = True, ledger: tuple = (), steps: bool = False) -> ExecutableGraph:
    """A post: ``Inbox``, a port unless ``ports`` is false, and ``Ledger``, its own
    place; a parameter ``speed``; with ``steps``, ``File`` from the inbox to the
    ledger, which is the post's own move."""
    return construct([
        ListPlaceNode("Inbox", str, port=ports),
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


# --- the words -------------------------------------------------------------------------------------------


class TestWhichNetsAreClosed:

    def test_a_net_that_marks_a_port_is_closed(self):
        assert post().is_closed and not post().open

    def test_a_net_that_marks_no_port_is_open_as_before(self):
        assert not post(ports=False).is_closed

    def test_a_net_may_declare_itself_open(self):
        graph = post(open=True)
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

    def test_stage_two_is_one_default(self, monkeypatch):
        """Stage two closes every net that does not declare itself open."""
        monkeypatch.setattr(components, "CLOSED_WITHOUT_PORTS", True)
        assert post(ports=False).is_closed and not post(ports=False, open=True).is_closed
        with pytest.raises(ClosedNetError):
            NestedNets.put(post(ports=False), "Inbox", ["Letter"])


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

    def test_an_open_net_and_a_net_with_no_port_take_tokens_anywhere(self):
        for graph in (post(open=True), post(ports=False)):
            NestedNets.put(graph, "Ledger", ["Entry"])
            assert NestedNets.take(graph, "Ledger") == ["Entry"]

    def test_a_port_pair_is_judged_by_the_net_around_the_subnet_first(self):
        """A port of a subnet place is never a port of the net that holds it."""
        def holder(open: bool, port: bool = True) -> ExecutableGraph:
            return construct([
                ListPlaceNode("Desk", str, port=port),
                SubnetPlaceNode("Branch", post()),
            ], open=open)

        closed = holder(open=False)
        with pytest.raises(ClosedNetError, match="port 'Inbox' of subnet place 'Branch'"):
            NestedNets.put(closed, Port("Branch", "Inbox"), ["Letter"])
        assert tokens(closed.subnet("Branch"), "Inbox") == []
        for graph in (holder(open=True), holder(open=False, port=False)):
            NestedNets.put(graph, Port("Branch", "Inbox"), ["Letter"])
            assert tokens(graph.subnet("Branch"), "Inbox") == ["Letter"]

    def test_a_subnet_scope_block_is_outside_the_subnet(self):
        world = construct([SubnetPlaceNode("Branch", post())])
        with NestedNets.subnet_scope(world, "Branch") as branch:
            NestedNets.put(branch, "Inbox", ["Letter"])
            with pytest.raises(ClosedNetError):
                NestedNets.put(branch, "Ledger", ["Forgery"])
            NestedNets.set_param(branch, "speed", 3)  # knobs stay with the program
        assert tokens(world.subnet("Branch"), "Inbox") == ["Letter"] and world.subnet("Branch").params["speed"] == 3

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
    def test_a_strict_context_raises_the_refusal(self, command):
        async def go():
            ctx = loaded_post(strict=True)
            with pytest.raises(ClosedNetError, match="a SetTokens|an Extend"):
                drained(ctx, command)
            assert tokens(ctx.graph, "Ledger") == []
        asyncio.run(go())

    def test_any_other_context_drops_it_and_logs_why(self, caplog):
        async def go():
            ctx = loaded_post(strict=False)
            with caplog.at_level(logging.WARNING, logger="petritype.runtime"):
                assert drained(ctx, Extend("Ledger", ["Forgery"]), Extend("Inbox", ["Letter"])) == 1
            assert tokens(ctx.graph, "Ledger") == [] and tokens(ctx.graph, "Inbox") == ["Letter"]
            assert "closed" in caplog.text and REMEDY in caplog.text
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


# --- a body of the net around it -----------------------------------------------------------------------------


KEYS = {"kind": lambda net: "post"}
WHERE = Filter.where(kind="post")
# Every edge kind that hands a body the live token: its name, the edge, and whether
# the argument is a list and whether the edge consumes.
LENDING_EDGES = (
    ("take", lambda: ArgumentEdgeToTransition("Posts", "Use", "unit"), False, True),
    ("take_all", lambda: ArgumentEdgeToTransition("Posts", "Use", "units"), True, True),
    ("take_where", lambda: TakeWhereEdge("Posts", "Use", "unit", where=WHERE), False, True),
    ("drain", lambda: DrainEdge("Posts", "Use", "units"), True, True),
    ("mutate", lambda: MutateEdge("Posts", "Use", "unit"), False, False),
    ("mutate_where", lambda: MutateWhereEdge("Posts", "Use", "unit", where=WHERE), False, False),
    ("read", lambda: ReadEdge("Posts", "Use", "unit"), False, False),
    ("read_where", lambda: ReadWhereEdge("Posts", "Use", "unit", where=WHERE), False, False),
    ("read_all", lambda: ReadAllEdge("Posts", "Use", "units"), True, False),
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


def office(edge, body, unit: ExecutableGraph, restore: bool = True, record: bool = False) -> ExecutableGraph:
    """A place of posts, ``Posts``, handed to ``Use`` by ``edge``, which also takes
    an order and produces it into ``Done``."""
    return construct([
        ListPlaceNode("Posts", ExecutableGraph, [unit], nested=True, keys=KEYS),
        ListPlaceNode("Orders", str, ["Letter"]),
        ListPlaceNode("Done", str),
        FunctionTransitionNode("Use", body),
        ArgumentEdgeToTransition("Orders", "Use", "order"),
        edge,
        ReturnedEdgeFromTransition("Use", "Done"),
    ], restore_tokens_on_failure=restore, record_events=record)


def fails_with(graph: ExecutableGraph, kind):
    with pytest.raises(TransitionFailedError) as failed:
        run(graph)
    assert isinstance(failed.value.__cause__, kind), failed.value.__cause__
    return failed.value


class TestABodyByEachEdgeKind:

    @pytest.mark.parametrize("kind, make, many, consumes", LENDING_EDGES, ids=KINDS)
    def test_a_put_at_a_port_lands(self, kind, make, many, consumes):
        unit = post()
        graph = run(office(make(), body_doing(lambda net: NestedNets.put(net, "Inbox", ["Letter"]), many), unit))
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

        for action in (
            lambda net: NestedNets.set_param(net, "speed", 9),
            lambda net: net.params.update(speed=9),
            lambda net: net.disabled.add("File"),
            by_name,
        ):
            unit = post(steps=True)
            failure = fails_with(office(make(), body_doing(action, many), unit), HeldByBodyError)
            assert "stay with the program and the runner" in str(failure.__cause__)
        assert unit.params["speed"] == 1

    @pytest.mark.parametrize("kind, make, many, consumes", LENDING_EDGES[:1] + LENDING_EDGES[4:5], ids=["take", "mutate"])
    def test_stepping_a_net_it_holds_is_the_nets_own_move(self, kind, make, many, consumes):
        async def step(net):
            NestedNets.put(net, "Inbox", ["Letter"])
            await ExecutableGraphOperations.execute_graph(net, stop_after_n_firings=None)

        unit = post(steps=True)
        run(office(make(), body_doing(step, many, asynchronous=True), unit))
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

    def test_a_net_with_no_port_is_as_before_its_knobs_included(self):
        unit = post(ports=False)

        def write(net):
            NestedNets.put(net, "Ledger", ["Entry"])
            NestedNets.set_param(net, "speed", 9)
            net.place_named("Inbox").tokens.append("Note")

        run(office(MutateEdge("Posts", "Use", "unit"), body_doing(write, False), unit))
        assert tokens(unit, "Ledger") == ["Entry"] and unit.params["speed"] == 9

    def test_a_net_two_levels_down_has_its_own_rule(self):
        """The body reads its way down into a closed net inside the post's own
        place of nets: that net takes tokens at its ports alone."""
        def outer_post() -> ExecutableGraph:
            return construct([
                ListPlaceNode("Inbox", str, port=True),
                ListPlaceNode("Sacks", ExecutableGraph, [post()], nested=True, stepping="passive"),
            ], open=True)

        unit = outer_post()
        sack = tokens(unit, "Sacks")[0]
        run(office(MutateEdge("Posts", "Use", "unit"), body_doing(
            lambda net: NestedNets.put(tokens(net, "Sacks")[0], "Inbox", ["Letter"]), False), unit))
        assert tokens(sack, "Inbox") == ["Letter"]
        fails_with(office(MutateEdge("Posts", "Use", "unit"), body_doing(
            lambda net: NestedNets.put(tokens(net, "Sacks")[0], "Ledger", ["Forgery"]), False), outer_post()),
            ClosedNetError)

    def test_a_subnet_scope_on_a_held_net_holds_the_subnet_for_the_body(self):
        def branch_office(open: bool = False) -> ExecutableGraph:
            return construct([ListPlaceNode("Desk", str, port=True), SubnetPlaceNode("Branch", post())], open=open)

        def through_scope(action):
            def act(net):
                with NestedNets.subnet_scope(net, "Branch") as branch:
                    assert branch.held_by_body
                    action(branch)
            return act

        unit = branch_office()
        run(office(MutateEdge("Posts", "Use", "unit"),
                   body_doing(through_scope(lambda b: NestedNets.put(b, "Inbox", ["Letter"])), False), unit))
        assert tokens(unit.subnet("Branch"), "Inbox") == ["Letter"] and not unit.subnet("Branch").held_by_body
        for action, kind in (
            (lambda b: NestedNets.set_param(b, "speed", 9), HeldByBodyError),
            (lambda b: b.place_named("Ledger").tokens.append("Forgery"), ClosedNetError),
        ):
            fails_with(office(MutateEdge("Posts", "Use", "unit"), body_doing(through_scope(action), False),
                              branch_office()), kind)

    def test_a_port_pair_through_a_held_net_follows_that_nets_rule(self):
        def branch_office(open: bool) -> ExecutableGraph:
            return construct([ListPlaceNode("Desk", str, port=True), SubnetPlaceNode("Branch", post())], open=open)

        write = body_doing(lambda net: NestedNets.put(net, Port("Branch", "Inbox"), ["Letter"]), False)
        fails_with(office(MutateEdge("Posts", "Use", "unit"), write, branch_office(open=False)), ClosedNetError)
        unit = branch_office(open=True)
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
        fails_with(office(ArgumentEdgeToTransition("Posts", "Use", "unit"), body_doing(both, False), unit), ClosedNetError)
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
