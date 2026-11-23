"""Keyed edges that choose their token by another input of the same firing
(``Clause(key, input=place, input_key=key)``, since 2026-11-23): a *bound edge*
takes or mutates the token whose key equals a key of the token its transition
gets from its *source*. The oldest source token that has a match fires, with
the oldest match for it; every other token keeps its place and its id."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from petritype import petri_net
from petritype.core.events import Failed, Fired, Mutated, Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    NetParams,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    SubnetPlaceNode,
    SurveyEdge,
    TransitionFailedError,
)
from petritype.core.keys import Clause, Filter, MutateWhereEdge, Query, TakeWhereEdge
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry
from petritype.runtime import ExecutionMode, RunContext, Runner


def run(coroutine):
    return asyncio.run(coroutine)


@dataclass
class Unit:
    name: str
    hp: int = 10
    team: str = "red"


@dataclass
class Order:
    unit: str
    medic: str = ""
    urgent: bool = False
    note: str = ""


UNIT_KEYS = ("name", "team", "hp")
ORDER_KEYS = ("unit", "medic", "urgent")
BY_ORDER = Filter((Clause("name", input="Orders", input_key="unit"),))


def build(*nodes, units=(), orders=(), restore=False, params=None, places=()):
    """Units and orders, the nodes given, and a ``Done`` place for units and one for orders."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Units", Unit, list(units), keys=UNIT_KEYS),
        ListPlaceNode("Orders", Order, list(orders), keys=ORDER_KEYS),
        ListPlaceNode("Done", Unit),
        ListPlaceNode("Filed", Order),
        *places,
        *nodes,
    ], restore_tokens_on_failure=restore, params=params, record_events=True)


def send(order: Order, unit: Unit) -> Unit:
    return unit


def send_net(units, orders, **rest):
    """``Send`` takes the front order and the unit it names, and puts the unit in ``Done``."""
    return build(
        FunctionTransitionNode("Send", send),
        ArgumentEdgeToTransition("Orders", "Send", "order"),
        TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
        ReturnedEdgeFromTransition("Send", "Done"),
        units=units, orders=orders, **rest,
    )


def fire(graph) -> int:
    _, fired = run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    return fired


def names(graph, place):
    return [token.name for token in graph.place_named(place).tokens]


def ids(graph, place):
    return list(graph.place_named(place).token_ids)


def events_of(graph, kind):
    return [event for event in graph.drain_events() if isinstance(event, kind)]


# --- the clause ---------------------------------------------------------------------------------


class TestTheClause:

    def test_exactly_one_of_values_param_and_input(self):
        Clause("name", input="Orders", input_key="unit")
        Clause("name", input=Port("Depot", "Yard"), input_key="unit")
        for bad in (
            dict(),
            dict(values=("Ada",), input="Orders", input_key="unit"),
            dict(param="who", input="Orders", input_key="unit"),
            dict(input="Orders"),
            dict(values=("Ada",), input_key="unit"),
            dict(param="who", input_key="unit"),
        ):
            with pytest.raises(ValueError):
                Clause("name", **bad)
        with pytest.raises(TypeError):
            Clause("name", input=3, input_key="unit")
        with pytest.raises(TypeError):
            Clause("name", input="Orders", input_key=3)

    def test_it_accepts_nothing_outside_a_firing(self):
        graph = send_net([Unit("Ada")], [Order("Ada")])
        units = graph.place_named("Units")
        ada = units.tokens[0]
        clause = Clause("name", input="Orders", input_key="unit")
        assert not clause.accepts({"name": "Ada"}, {})
        assert not BY_ORDER.keeps(units, ada, {})
        assert not BY_ORDER.matches(graph, units, ada)
        assert Query.matches(graph, BY_ORDER) == []
        assert Query.first(graph, units, BY_ORDER) is None
        beside = Filter((Clause("team", ("red",)), clause))
        assert Query.matches(graph, beside) == [], "a constant clause beside it does not help"
        assert Query.count(graph, units, beside) == 0


# --- the take and the mutate form ---------------------------------------------------------------


class TestTheTakeForm:

    def test_the_unit_an_order_names_and_an_order_with_no_match_waits(self):
        graph = send_net([Unit("Ada"), Unit("Ben"), Unit("Cy")], [Order("Zed"), Order("Ben"), Order("Ada")])
        (ada, ben, cy), (zed_order, ben_order, ada_order) = ids(graph, "Units"), ids(graph, "Orders")
        graph.drain_events()
        assert fire(graph) == 1
        (fired,) = events_of(graph, Fired)
        assert fired.consumed == (("Orders", ben_order), ("Units", ben)), "the source first, as declared"
        assert names(graph, "Done") == ["Ben"]
        assert ids(graph, "Orders") == [zed_order, ada_order], "Zed's order waits where it was, with its id"
        assert ids(graph, "Units") == [ada, cy], "the others never moved"
        assert fire(graph) == 1 and names(graph, "Done") == ["Ben", "Ada"]
        assert fire(graph) == 0, "Zed has no unit here"
        assert ids(graph, "Orders") == [zed_order] and ids(graph, "Units") == [cy]

    def test_the_oldest_match_for_the_oldest_source_token(self):
        graph = send_net([Unit("Ben", hp=1), Unit("Ada", hp=2), Unit("Ada", hp=3)], [Order("Ada"), Order("Ben")])
        fire(graph)
        assert [unit.hp for unit in graph.place_named("Done").tokens] == [2], "Ada's order first, the oldest Ada"
        assert [order.unit for order in graph.place_named("Orders").tokens] == ["Ben"]

    def test_a_later_unit_brings_a_waiting_order_back(self):
        graph = send_net([], [Order("Ada")])
        transition = graph.transition_named("Send")
        maps = ExecutableGraphOperations.maps(graph)
        assert not ExecutableGraphCheck.transition_is_fireable(transition, graph, maps[1], maps[0], maps[3])
        graph.place_named("Units").tokens.append(Unit("Ada"))
        assert fire(graph) == 1 and names(graph, "Done") == ["Ada"]

    def test_without_the_graph_presence_alone_is_judged(self):
        graph = send_net([Unit("Ben")], [Order("Ada")])
        transition = graph.transition_named("Send")
        maps = ExecutableGraphOperations.maps(graph)
        assert ExecutableGraphCheck.sufficient_tokens_are_available(transition, maps[1], maps[0], maps[3])
        assert not ExecutableGraphCheck.transition_is_fireable(transition, graph, maps[1], maps[0], maps[3])


class TestTheMutateForm:

    def test_the_unit_an_order_names_is_changed_in_place(self):
        def wound(order: Order, unit: Unit) -> Order:
            unit.hp -= 1
            return order

        graph = build(
            FunctionTransitionNode("Wound", wound),
            ArgumentEdgeToTransition("Orders", "Wound", "order"),
            MutateWhereEdge("Units", "Wound", "unit", where=BY_ORDER),
            ReturnedEdgeFromTransition("Wound", "Filed"),
            units=[Unit("Ada"), Unit("Ben"), Unit("Cy")], orders=[Order("Zed"), Order("Cy")],
        )
        units, (zed_order, cy_order) = ids(graph, "Units"), ids(graph, "Orders")
        graph.drain_events()
        assert fire(graph) == 1
        assert [unit.hp for unit in graph.place_named("Units").tokens] == [10, 10, 9]
        assert ids(graph, "Units") == units, "every unit keeps its place and its id"
        assert ids(graph, "Orders") == [zed_order]
        mutated, fired = graph.drain_events()
        assert mutated == Mutated("Units", units[2])
        assert isinstance(fired, Fired) and fired.consumed == (("Orders", cy_order),)


# --- several edges, a filtered source, other source kinds ---------------------------------------------


class TestTwoEdgesBoundToOneSource:

    def test_the_source_token_needs_a_match_in_both(self):
        def treat(order: Order, unit: Unit, medic: Unit) -> Unit:
            medic.hp -= 1
            return unit

        graph = build(
            FunctionTransitionNode("Treat", treat),
            ArgumentEdgeToTransition("Orders", "Treat", "order"),
            TakeWhereEdge("Units", "Treat", "unit", where=BY_ORDER),
            MutateWhereEdge("Medics", "Treat", "medic", where=Filter((Clause("name", input="Orders", input_key="medic"),))),
            ReturnedEdgeFromTransition("Treat", "Done"),
            units=[Unit("Ada"), Unit("Ben")], orders=[Order("Ada", medic="Zed"), Order("Ben", medic="Mo")],
            places=[ListPlaceNode("Medics", Unit, [Unit("Mo"), Unit("Kay")], keys=("name",))],
        )
        assert fire(graph) == 1
        assert names(graph, "Done") == ["Ben"], "Ada's order names a medic who is not there"
        assert [(m.name, m.hp) for m in graph.place_named("Medics").tokens] == [("Mo", 9), ("Kay", 10)]
        assert fire(graph) == 0


class TestASourceWithItsOwnFilter:

    def test_only_its_own_matches_are_candidates(self):
        graph = build(
            FunctionTransitionNode("Send", send),
            TakeWhereEdge("Orders", "Send", "order", where=Filter.where(urgent=True)),
            TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            ReturnedEdgeFromTransition("Send", "Done"),
            units=[Unit("Ada"), Unit("Ben")],
            orders=[Order("Ada"), Order("Zed", urgent=True), Order("Ben", urgent=True)],
        )
        assert fire(graph) == 1
        assert names(graph, "Done") == ["Ben"], "Ada's order is not urgent; Zed's has no unit"
        assert [order.unit for order in graph.place_named("Orders").tokens] == ["Ada", "Zed"]
        assert fire(graph) == 0, "Ada's order and unit are both there, but the order is not urgent"


class TestOtherSources:

    def test_a_snapshot_source_copies_the_chosen_token(self):
        seen = []

        def look(order: Order, unit: Unit) -> Unit:
            seen.append(order)
            return unit

        graph = build(
            FunctionTransitionNode("Look", look),
            SnapshotEdge("Orders", "Look", "order"),
            TakeWhereEdge("Units", "Look", "unit", where=BY_ORDER),
            ReturnedEdgeFromTransition("Look", "Done"),
            units=[Unit("Ben")], orders=[Order("Zed"), Order("Ben")],
        )
        orders = ids(graph, "Orders")
        assert fire(graph) == 1
        assert seen[0].unit == "Ben" and seen[0] is not graph.place_named("Orders").tokens[1]
        assert ids(graph, "Orders") == orders, "a snapshot leaves its place as it was"

    def test_a_mutate_source_hands_over_the_chosen_token_live(self):
        def note(order: Order, unit: Unit) -> None:
            order.note = f"seen {unit.name}"
            unit.hp -= 1

        graph = build(
            FunctionTransitionNode("Note", note),
            MutateEdge("Orders", "Note", "order"),
            MutateWhereEdge("Units", "Note", "unit", where=BY_ORDER),
            units=[Unit("Ada"), Unit("Ben")], orders=[Order("Zed"), Order("Ben")],
        )
        units, orders = ids(graph, "Units"), ids(graph, "Orders")
        graph.drain_events()
        assert fire(graph) == 1
        assert [order.note for order in graph.place_named("Orders").tokens] == ["", "seen Ben"]
        assert [unit.hp for unit in graph.place_named("Units").tokens] == [10, 9]
        assert ids(graph, "Units") == units and ids(graph, "Orders") == orders
        events = graph.drain_events()
        assert events[:2] == [Mutated("Orders", orders[1]), Mutated("Units", units[1])]


# --- ports ----------------------------------------------------------------------------------------


def depot(port: str, place_type, tokens, keys):
    return SubnetPlaceNode("Depot", ExecutableGraphOperations.construct_graph([
        ListPlaceNode(port, place_type, tokens, keys=keys, port=True),
    ]))


class TestPorts:

    def test_a_bound_edge_on_a_port(self):
        yard = Port("Depot", "Yard")
        graph = build(
            FunctionTransitionNode("Send", send),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            TakeWhereEdge(yard, "Send", "unit", where=BY_ORDER),
            ReturnedEdgeFromTransition("Send", "Done"),
            orders=[Order("Zed"), Order("Ben")],
            places=[depot("Yard", Unit, [Unit("Ada"), Unit("Ben")], UNIT_KEYS)],
        )
        assert fire(graph) == 1 and names(graph, "Done") == ["Ben"]
        assert [unit.name for unit in graph.subnet("Depot").place_named("Yard").tokens] == ["Ada"]

    def test_a_source_on_a_port(self):
        inbox = Port("Depot", "Inbox")
        graph = build(
            FunctionTransitionNode("Send", send),
            ArgumentEdgeToTransition(inbox, "Send", "order"),
            TakeWhereEdge("Units", "Send", "unit", where=Filter((Clause("name", input=inbox, input_key="unit"),))),
            ReturnedEdgeFromTransition("Send", "Done"),
            units=[Unit("Ada"), Unit("Ben")],
            places=[depot("Inbox", Order, [Order("Zed"), Order("Ada")], ORDER_KEYS)],
        )
        zed = graph.subnet("Depot").place_named("Inbox").token_ids[0]
        assert fire(graph) == 1 and names(graph, "Done") == ["Ada"]
        assert graph.subnet("Depot").place_named("Inbox").token_ids == (zed,)


# --- failures, ids, other clauses -------------------------------------------------------------------------


class TestAFailedFiring:

    def test_both_tokens_go_back_to_the_front_with_their_ids(self):
        def refuse(order: Order, unit: Unit) -> Unit:
            raise RuntimeError(f"{unit.name} refuses")

        graph = build(
            FunctionTransitionNode("Send", refuse),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            ReturnedEdgeFromTransition("Send", "Done"),
            units=[Unit("Ada"), Unit("Ben"), Unit("Cy")], orders=[Order("Zed"), Order("Ben")], restore=True,
        )
        (ada, ben, cy), (zed_order, ben_order) = ids(graph, "Units"), ids(graph, "Orders")
        graph.drain_events()
        with pytest.raises(TransitionFailedError) as raised:
            fire(graph)
        assert raised.value.restored
        assert ids(graph, "Units") == [ben, ada, cy] and names(graph, "Units") == ["Ben", "Ada", "Cy"]
        assert ids(graph, "Orders") == [ben_order, zed_order]
        (failed,) = events_of(graph, Failed)
        assert failed.consumed == (("Orders", ben_order), ("Units", ben)) and failed.restored


class TestOtherClausesBesideIt:

    def test_a_param_and_a_constant_clause_in_the_same_filter(self):
        chosen = Filter((
            Clause("name", input="Orders", input_key="unit"),
            Clause("team", param="side"),
            Clause("hp", (10,)),
        ))
        graph = build(
            FunctionTransitionNode("Send", send),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            TakeWhereEdge("Units", "Send", "unit", where=chosen),
            ReturnedEdgeFromTransition("Send", "Done"),
            units=[Unit("Ada", team="blue"), Unit("Ada", hp=5), Unit("Ben"), Unit("Ada")],
            orders=[Order("Ada"), Order("Ada")], params={"side": "red"},
        )
        fire(graph)
        assert [(unit.team, unit.hp) for unit in graph.place_named("Done").tokens] == [("red", 10)]
        assert [unit.name for unit in graph.place_named("Units").tokens] == ["Ada", "Ada", "Ben"]
        NetParams.set(graph, "side", "blue")
        fire(graph)
        assert [unit.team for unit in graph.place_named("Done").tokens] == ["red", "blue"]

    def test_one_is_not_true(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Flags", Unit, [Unit("Ada")], keys={"on": lambda unit: True}),
            ListPlaceNode("Orders", Order, [Order("one"), Order("yes")],
                          keys={"on": lambda order: 1 if order.unit == "one" else True}),
            ListPlaceNode("Done", Unit),
            FunctionTransitionNode("Send", send),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            TakeWhereEdge("Flags", "Send", "unit", where=Filter((Clause("on", input="Orders", input_key="on"),))),
            ReturnedEdgeFromTransition("Send", "Done"),
        ])
        fire(graph)
        assert [order.unit for order in graph.place_named("Orders").tokens] == ["one"], "1 found no flag that is true"

    def test_two_clauses_on_one_source(self):
        both = Filter((Clause("name", input="Orders", input_key="unit"), Clause("team", input="Orders", input_key="medic")))
        graph = build(
            FunctionTransitionNode("Send", send),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            TakeWhereEdge("Units", "Send", "unit", where=both),
            ReturnedEdgeFromTransition("Send", "Done"),
            units=[Unit("Ada", team="red"), Unit("Ada", team="blue")], orders=[Order("Ada", medic="blue")],
        )
        fire(graph)
        assert [unit.team for unit in graph.place_named("Done").tokens] == ["blue"]


class TestTheConcurrentRuntime:

    def test_it_fires_the_same_pairs(self):
        chosen = Filter((Clause("name", input="Orders", input_key="unit"), Clause("team", param="side")))
        graph = build(
            FunctionTransitionNode("Send", send),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            TakeWhereEdge("Units", "Send", "unit", where=chosen),
            ReturnedEdgeFromTransition("Send", "Done"),
            units=[Unit("Ada", team="blue"), Unit("Ben"), Unit("Ada")],
            orders=[Order("Zed"), Order("Ada"), Order("Ben")], params={"side": "red"},
        )
        summary = run(Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)))
        assert summary.quiesced
        assert sorted((unit.name, unit.team) for unit in graph.place_named("Done").tokens) == [
            ("Ada", "red"), ("Ben", "red"),
        ]
        assert [order.unit for order in graph.place_named("Orders").tokens] == ["Zed"]


# --- over the wire ----------------------------------------------------------------------------------------


@petri_net(name="send", description="Orders send the unit they name")
def send_factory() -> ExecutableGraph:
    return send_net([Unit("Ada"), Unit("Ben"), Unit("Cy")], [Order("Zed"), Order("Ben")])


@petri_net(name="depot", description="Orders in a port send the unit they name from another port")
def depot_factory() -> ExecutableGraph:
    inbox, yard = Port("Depot", "Inbox"), Port("Depot", "Yard")
    return ExecutableGraphOperations.construct_graph([
        SubnetPlaceNode("Depot", ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Inbox", Order, [Order("Ada")], keys=ORDER_KEYS, port=True),
            ListPlaceNode("Yard", Unit, [Unit("Ada")], keys=UNIT_KEYS, port=True),
        ])),
        ListPlaceNode("Done", Unit),
        FunctionTransitionNode("Send", send),
        ArgumentEdgeToTransition(inbox, "Send", "order"),
        TakeWhereEdge(yard, "Send", "unit", where=Filter((Clause("name", input=inbox, input_key="unit"),))),
        ReturnedEdgeFromTransition("Send", "Done"),
    ])


class Wire:
    def __init__(self, factory):
        self.owner = Owner(Registry.from_factories([factory]))
        self.next_id = 0

    async def send(self, method, params=None):
        self.next_id += 1
        now = datetime(2026, 11, 23, tzinfo=timezone.utc)
        out = await self.owner.handle({"id": str(self.next_id), "method": method, "params": params or {}}, now)
        lines = list(out.lines)
        while out.launch is not None:
            out = await self.owner.completed(await out.launch.coroutine, now)
            lines.extend(out.lines)
        return lines


def bound_edge(state, transition):
    return next(
        edge for edge in state["topology"]["edges"]
        if edge["transition"] == transition and edge.get("filter") is not None
    )


class TestOverTheWire:

    @pytest.mark.asyncio
    async def test_the_topology_shows_the_clause_as_declared(self):
        wire = Wire(send_factory)
        await wire.send("load", {"net": "send"})
        state = (await wire.send("get_state"))[-1]["result"]
        assert bound_edge(state, "Send") == {
            "place": "Units", "transition": "Send", "direction": "in", "kind": "take_where",
            "filter": {"clauses": [{"key": "name", "input": {"place": "Orders", "key": "unit"}}]},
        }

    @pytest.mark.asyncio
    async def test_a_port_source_is_named_by_its_pair(self):
        wire = Wire(depot_factory)
        await wire.send("load", {"net": "depot"})
        state = (await wire.send("get_state"))[-1]["result"]
        assert bound_edge(state, "Send") == {
            "place": "Depot", "port": "Yard", "transition": "Send", "direction": "in", "kind": "take_where",
            "filter": {"clauses": [{"key": "name", "input": {"place": "Depot", "port": "Inbox", "key": "unit"}}]},
        }
        lines = await wire.send("step")
        assert lines[-1]["result"]["fired"] is True

    @pytest.mark.asyncio
    async def test_launched_names_the_pair_and_the_others_keep_their_ids(self):
        wire = Wire(send_factory)
        await wire.send("load", {"net": "send"})
        state = (await wire.send("get_state"))[-1]["result"]
        ada, ben, cy = (token["id"] for token in state["marking"]["places"]["Units"]["tokens"])
        zed_order, ben_order = (token["id"] for token in state["marking"]["places"]["Orders"]["tokens"])
        lines = await wire.send("step")
        assert lines[0]["method"] == "launched"
        assert lines[0]["params"]["consumed"] == [["Orders", ben_order], ["Units", ben]]
        after = (await wire.send("get_state"))[-1]["result"]["marking"]["places"]
        assert [token["id"] for token in after["Units"]["tokens"]] == [ada, cy]
        assert [token["id"] for token in after["Orders"]["tokens"]] == [zed_order]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["query", "set_filter"])
    async def test_a_client_filter_with_the_clause_is_invalid_params(self, method):
        wire = Wire(send_factory)
        await wire.send("load", {"net": "send"})
        clause = {"key": "name", "input": {"place": "Orders", "key": "unit"}}
        (line,) = await wire.send(method, {"filter": {"clauses": [clause]}})
        assert line["error"]["code"] == "invalid_params"
        assert "keyed edge" in line["error"]["message"]


# --- the drawing ------------------------------------------------------------------------------------------


class TestTheDrawing:

    def test_the_label_of_each_clause(self):
        pytest.importorskip("rustworkx")
        from petritype.plotting.rustworkx_to_graphviz import NodeLabel
        assert NodeLabel.filter_label(Filter()) == "any"
        assert NodeLabel.filter_label(Filter.where(class_=["Bird", "Mammal"], flying=True, legs=2)) == (
            "class in {Bird, Mammal} and flying in {true} and legs in {2}"
        )
        assert NodeLabel.filter_label(Filter((Clause("class", param="season"), *BY_ORDER.clauses))) == (
            "class = $season and name = Orders.unit"
        )
        on_a_port = Filter((Clause("name", input=Port("Level 1", "Lobby"), input_key="courier"),))
        assert NodeLabel.filter_label(on_a_port) == "name = Level 1/Lobby.courier"

    def test_a_keyed_edge_carries_its_filter_as_its_label(self):
        pytest.importorskip("rustworkx")
        from petritype.plotting.rustworkx_graph import RustworkxGraph
        from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz

        def wound(order: Order, unit: Unit) -> None:
            pass

        graph = build(
            FunctionTransitionNode("Send", send),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            TakeWhereEdge("Units", "Send", "unit", where=Filter((Clause("team", ('say "red"',)), *BY_ORDER.clauses))),
            ReturnedEdgeFromTransition("Send", "Done"),
            FunctionTransitionNode("Wound", wound),
            ArgumentEdgeToTransition("Orders", "Wound", "order"),
            MutateWhereEdge("Units", "Wound", "unit", where=BY_ORDER),
        )
        pydigraph = RustworkxGraph.from_executable_graph(graph)
        _, edge_attrs = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
        labels = {
            (pydigraph[source].name, pydigraph[target].name): edge_attrs(data).get("label")
            for source, target, data in pydigraph.weighted_edge_list()
        }
        assert labels[("Units", "Send")] == 'team in {say \\"red\\"} and name = Orders.unit'
        assert labels[("Units", "Wound")] == "name = Orders.unit"
        assert labels[("Orders", "Send")] is None and labels[("Send", "Done")] is None
        assert 'label="name = Orders.unit"' in RustworkxToGraphviz.dot_source(pydigraph, None, edge_attrs)


# --- what the build refuses ---------------------------------------------------------------------------


def refused(*nodes, match, **rest):
    with pytest.raises(ValueError, match=match):
        build(*nodes, **rest)


class TestTheBuildRefuses:

    def test_a_source_the_transition_does_not_read(self):
        refused(
            FunctionTransitionNode("Send", lambda unit: None),
            TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            match="transition 'Send' chooses its token from 'Units' by 'Orders', which it does not read",
        )

    @pytest.mark.parametrize("source", ["drain", "list", "survey"])
    def test_a_source_that_gives_every_token(self, source):
        def every(order: list[Order], unit: Unit) -> None:
            pass

        edge = {
            "drain": DrainEdge("Orders", "Send", "order"),
            "list": ArgumentEdgeToTransition("Orders", "Send", "order"),
            "survey": SurveyEdge("Orders", "Send", "order"),
        }[source]
        refused(
            FunctionTransitionNode("Send", every),
            edge,
            TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            match="chooses its token from 'Units' by 'Orders', whose edge gives every token rather than one",
        )

    def test_a_chain(self):
        refused(
            FunctionTransitionNode("Send", lambda call, order, unit: None),
            ListPlaceNode("Calls", Order, keys=("unit",)),
            ArgumentEdgeToTransition("Calls", "Send", "call"),
            TakeWhereEdge("Orders", "Send", "order", where=Filter((Clause("unit", input="Calls", input_key="unit"),))),
            TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            match="chooses its token from 'Units' by 'Orders', whose own token is chosen by an input",
        )

    def test_a_cycle_and_an_edge_that_names_its_own_place(self):
        refused(
            FunctionTransitionNode("Send", lambda order, unit: None),
            TakeWhereEdge("Orders", "Send", "order", where=Filter((Clause("unit", input="Units", input_key="name"),))),
            TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            match="whose own token is chosen by an input",
        )
        refused(
            FunctionTransitionNode("Send", lambda unit: None),
            TakeWhereEdge("Units", "Send", "unit", where=Filter((Clause("name", input="Units", input_key="team"),))),
            match="chooses its token from 'Units' by 'Units', whose own token is chosen by an input",
        )

    def test_two_sources_in_one_filter(self):
        refused(
            FunctionTransitionNode("Send", lambda order, call, unit: None),
            ListPlaceNode("Calls", Order, keys=("unit",)),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            ArgumentEdgeToTransition("Calls", "Send", "call"),
            TakeWhereEdge("Units", "Send", "unit", where=Filter((
                Clause("name", input="Orders", input_key="unit"), Clause("team", input="Calls", input_key="unit"),
            ))),
            match="chooses its token from 'Units' by two inputs, 'Orders' and 'Calls'",
        )

    def test_a_key_either_place_does_not_declare(self):
        refused(
            FunctionTransitionNode("Send", lambda order, unit: None),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            TakeWhereEdge("Units", "Send", "unit", where=Filter((Clause("rank", input="Orders", input_key="unit"),))),
            match="chooses its token from 'Units' by key 'rank', which 'Units' does not declare",
        )
        refused(
            FunctionTransitionNode("Send", lambda order, unit: None),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            TakeWhereEdge("Units", "Send", "unit", where=Filter((Clause("name", input="Orders", input_key="who"),))),
            match="chooses its token from 'Units' by key 'who', which 'Orders' does not declare",
        )

    def test_a_constant_clause_on_an_undeclared_key_builds_as_before(self):
        build(
            FunctionTransitionNode("Send", lambda unit: None),
            TakeWhereEdge("Units", "Send", "unit", where=Filter.where(rank="General")),
        )

    def test_a_source_or_bound_place_read_by_two_edges(self):
        refused(
            FunctionTransitionNode("Send", lambda order, again, unit: None),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            SnapshotEdge("Orders", "Send", "again"),
            TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            match="transition 'Send' reads 'Orders' through more than one edge, and a token there chooses or is "
                  "chosen by another input",
        )
        refused(
            FunctionTransitionNode("Send", lambda order, unit, other: None),
            ArgumentEdgeToTransition("Orders", "Send", "order"),
            TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            MutateEdge("Units", "Send", "other"),
            match="transition 'Send' reads 'Units' through more than one edge",
        )

    def test_a_place_read_twice_elsewhere_builds_as_before(self):
        build(
            FunctionTransitionNode("Pair", lambda first, second: None),
            ArgumentEdgeToTransition("Units", "Pair", "first"),
            ArgumentEdgeToTransition("Units", "Pair", "second"),
        )

    def test_a_port_is_named_by_its_pair(self):
        refused(
            FunctionTransitionNode("Send", lambda unit: None),
            TakeWhereEdge("Units", "Send", "unit", where=Filter((
                Clause("name", input=Port("Depot", "Inbox"), input_key="unit"),
            ))),
            places=[depot("Inbox", Order, [], ORDER_KEYS)],
            match="chooses its token from 'Units' by port 'Inbox' of 'Depot', which it does not read",
        )
