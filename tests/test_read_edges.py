"""Read edges (since 2026-11-23): ``ReadEdge``, ``ReadWhereEdge`` and ``ReadAllEdge``
hand the body tokens to look at, live, without taking or copying them. No token
moves, no id changes and no event tells of them; a token that carries a net keeps
its net and the net its ``net_id``."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from petritype import petri_net
from petritype.core.events import Fired, Mutated, Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FiringTokenIds,
    FunctionTransitionNode,
    ListPlaceNode,
    MapPlaceNames,
    MapTransitionNames,
    MutateEdge,
    NestedNets,
    ReadAllEdge,
    ReadEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    SubnetPlaceNode,
    TransitionFailedError,
)
from petritype.core.keys import Clause, Filter, ReadWhereEdge, TakeWhereEdge
from petritype.runner import protocol as wire
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry
from petritype.runtime import ExecutionMode, RunContext, Runner


def run(coroutine):
    return asyncio.run(coroutine)


@dataclass
class Unit:
    name: str
    hp: int = 10


@dataclass
class Order:
    unit: str


UNIT_KEYS = ("name", "hp")
BY_ORDER = Filter((Clause("name", input="Orders", input_key="unit"),))


def build(*nodes, units=(), orders=(), restore=False, places=()):
    """Units, orders, the nodes given, and a ``Seen`` place for what the bodies saw."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Units", Unit, list(units), keys=UNIT_KEYS),
        ListPlaceNode("Orders", Order, list(orders), keys=("unit",)),
        ListPlaceNode("Seen", str),
        *places,
        *nodes,
    ], restore_tokens_on_failure=restore, record_events=True)


def fire(graph) -> int:
    _, fired = run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    return fired


def tokens(graph, place):
    return list(graph.place_named(place).tokens)


def ids(graph, place):
    return list(graph.place_named(place).token_ids)


def look(order: Order, unit: Unit) -> str:
    return f"{order.unit} saw {unit.name}"


def look_at_front(order: Order, unit: Unit) -> str:
    return f"front {unit.name}"


def count(order: Order, units: list[Unit]) -> str:
    return ", ".join(unit.name for unit in units) or "nobody"


# --- the three forms ------------------------------------------------------------------------------


class TestTheThreeForms:

    def test_a_read_edge_hands_over_the_front_token_live(self):
        seen = []

        def body(order: Order, unit: Unit) -> str:
            seen.append(unit)
            return unit.name

        graph = build(
            FunctionTransitionNode("Look", body),
            ArgumentEdgeToTransition("Orders", "Look", "order"),
            ReadEdge("Units", "Look", "unit"),
            ReturnedEdgeFromTransition("Look", "Seen"),
            units=[Unit("Ada"), Unit("Ben")], orders=[Order("x")],
        )
        before = ids(graph, "Units")
        assert fire(graph) == 1
        assert tokens(graph, "Seen") == ["Ada"]
        assert seen[0] is graph.place_named("Units").tokens[0], "the live token, not a copy"
        assert ids(graph, "Units") == before

    def test_a_read_where_edge_hands_over_the_oldest_match_live(self):
        graph = build(
            FunctionTransitionNode("Look", look),
            ArgumentEdgeToTransition("Orders", "Look", "order"),
            ReadWhereEdge("Units", "Look", "unit", where=Filter.where(hp=3)),
            ReturnedEdgeFromTransition("Look", "Seen"),
            units=[Unit("Ada"), Unit("Ben", hp=3), Unit("Cy", hp=3)], orders=[Order("x")],
        )
        before = ids(graph, "Units")
        assert fire(graph) == 1
        assert tokens(graph, "Seen") == ["x saw Ben"]
        assert ids(graph, "Units") == before

    def test_a_read_all_edge_hands_over_every_token_live_oldest_first(self):
        seen = []

        def body(order: Order, units: list[Unit]) -> str:
            seen.append(units)
            return count(order, units)

        graph = build(
            FunctionTransitionNode("Count", body),
            ArgumentEdgeToTransition("Orders", "Count", "order"),
            ReadAllEdge("Units", "Count", "units"),
            ReturnedEdgeFromTransition("Count", "Seen"),
            units=[Unit("Ada"), Unit("Ben")], orders=[Order("x")],
        )
        assert fire(graph) == 1
        assert tokens(graph, "Seen") == ["Ada, Ben"]
        place = graph.place_named("Units")
        assert all(a is b for a, b in zip(seen[0], place.tokens)), "the live tokens"
        assert seen[0] is not place.tokens, "in a list of the body's own"


class TestWhatHoldsATransitionBack:

    def test_a_read_edge_and_a_read_where_edge_wait_for_a_token(self):
        for edge in (ReadEdge("Units", "Look", "unit"), ReadWhereEdge("Units", "Look", "unit", where=Filter.where(hp=3))):
            graph = build(
                FunctionTransitionNode("Look", look),
                ArgumentEdgeToTransition("Orders", "Look", "order"),
                edge,
                ReturnedEdgeFromTransition("Look", "Seen"),
                units=[Unit("Ada")], orders=[Order("x")],
            )
            if isinstance(edge, ReadWhereEdge):
                assert fire(graph) == 0, "no unit with hp 3"
                graph.place_named("Units").tokens.append(Unit("Ben", hp=3))
                assert fire(graph) == 1
            else:
                graph.place_named("Units").tokens.clear()
                assert fire(graph) == 0, "an empty place"

    def test_a_read_all_edge_on_an_empty_place_fires_with_an_empty_list(self):
        seen = []

        def body(order: Order, units: list[Unit]) -> str:
            seen.append(units)
            return count(order, units)

        graph = build(
            FunctionTransitionNode("Count", body),
            ArgumentEdgeToTransition("Orders", "Count", "order"),
            ReadAllEdge("Units", "Count", "units"),
            ReturnedEdgeFromTransition("Count", "Seen"),
            orders=[Order("x")],
        )
        assert not ExecutableGraphCheck.requires_presence(graph.read_edges[0])
        assert fire(graph) == 1
        assert seen == [[]] and tokens(graph, "Seen") == ["nobody"]


# --- a place of tokens that carry nets ----------------------------------------------------------


def squad(*names: str) -> ExecutableGraph:
    net = ExecutableGraphOperations.construct_graph([ListPlaceNode("Members", str)])
    NestedNets.put(net, "Members", list(names))
    return net


def barracks(*nodes, calls=("Sarge",)):
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Barracks", ExecutableGraph, [squad("Ada", "Ben"), squad("Cy")], nested=True),
        ListPlaceNode("Calls", str, list(calls)),
        ListPlaceNode("Roll", str),
        *nodes,
    ], record_events=True)


def size(squad: ExecutableGraph) -> int:
    return len(squad.place_named("Members").tokens)


class TestAPlaceOfNets:

    @pytest.mark.parametrize("form", ["read", "read_all"])
    def test_the_nets_are_read_where_they_lie_with_their_ids(self, form):
        handed = []

        def one(caller: str, squad: ExecutableGraph) -> str:
            handed.append(squad)
            return f"{caller}: {size(squad)}"

        def every(caller: str, squads: list[ExecutableGraph]) -> str:
            handed.extend(squads)
            return f"{caller}: " + ", ".join(str(size(squad)) for squad in squads)

        body, edge = {"read": (one, ReadEdge), "read_all": (every, ReadAllEdge)}[form]
        graph = barracks(
            FunctionTransitionNode("Call", body),
            ArgumentEdgeToTransition("Calls", "Call", "caller"),
            edge("Barracks", "Call", "squad" if form == "read" else "squads"),
            ReturnedEdgeFromTransition("Call", "Roll"),
        )
        place = graph.place_named("Barracks")
        token_ids, net_ids = ids(graph, "Barracks"), [net.net_id for net in place.tokens]
        graph.drain_events()
        assert fire(graph) == 1
        assert tokens(graph, "Roll") == (["Sarge: 2"] if form == "read" else ["Sarge: 2, 1"])
        assert all(net is token for net, token in zip(handed, place.tokens)), "not copied"
        assert ids(graph, "Barracks") == token_ids
        assert [net.net_id for net in place.tokens] == net_ids
        events = graph.drain_events()
        assert [type(event) for event in events] == [Fired]
        assert all(place_name != "Barracks" for place_name, _ in events[0].consumed)

    def test_a_snapshot_copies_the_net_under_a_new_id_where_a_read_does_not(self):
        handed = []

        def body(caller: str, squad: ExecutableGraph) -> str:
            handed.append(squad)
            return caller

        graph = barracks(
            FunctionTransitionNode("Call", body),
            ArgumentEdgeToTransition("Calls", "Call", "caller"),
            SnapshotEdge("Barracks", "Call", "squad"),
            ReturnedEdgeFromTransition("Call", "Roll"),
        )
        front = graph.place_named("Barracks").tokens[0]
        assert fire(graph) == 1
        assert handed[0] is not front and handed[0].net_id != front.net_id


class TestThePromise:

    def test_a_body_that_changes_what_it_reads_changes_it_untold(self):
        """Python hands over the live token and cannot stop a body changing it: leaving
        it unchanged is the author's promise. A change shows in the place, and no
        ``Mutated`` tells of it."""

        def wound(order: Order, unit: Unit) -> str:
            unit.hp -= 1
            return unit.name

        graph = build(
            FunctionTransitionNode("Wound", wound),
            ArgumentEdgeToTransition("Orders", "Wound", "order"),
            ReadEdge("Units", "Wound", "unit"),
            ReturnedEdgeFromTransition("Wound", "Seen"),
            units=[Unit("Ada")], orders=[Order("x")],
        )
        graph.drain_events()
        assert fire(graph) == 1
        assert tokens(graph, "Units") == [Unit("Ada", hp=9)]
        assert not [event for event in graph.drain_events() if isinstance(event, Mutated)]


class TestAFailedFiring:

    def test_the_read_token_stays_and_only_the_taken_one_goes_back(self):
        def fail(order: Order, unit: Unit) -> str:
            raise RuntimeError("no")

        graph = build(
            FunctionTransitionNode("Look", fail),
            ArgumentEdgeToTransition("Orders", "Look", "order"),
            ReadEdge("Units", "Look", "unit"),
            ReturnedEdgeFromTransition("Look", "Seen"),
            units=[Unit("Ada")], orders=[Order("x")], restore=True,
        )
        unit_ids, order_ids = ids(graph, "Units"), ids(graph, "Orders")
        with pytest.raises(TransitionFailedError) as caught:
            fire(graph)
        assert set(caught.value.consumed) == {"order"}
        assert ids(graph, "Units") == unit_ids and ids(graph, "Orders") == order_ids


# --- bound clauses --------------------------------------------------------------------------------


class TestBoundClauses:

    def test_a_read_edge_as_the_source_of_a_bound_take(self):
        """The front order is read, not taken, and the unit it names is taken."""
        graph = build(
            FunctionTransitionNode("Send", look),
            ReadEdge("Orders", "Send", "order"),
            TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            ReturnedEdgeFromTransition("Send", "Seen"),
            units=[Unit("Ada"), Unit("Ben")], orders=[Order("Ben"), Order("Ada")],
        )
        order_ids = ids(graph, "Orders")
        assert fire(graph) == 1
        assert tokens(graph, "Seen") == ["Ben saw Ben"]
        assert [unit.name for unit in tokens(graph, "Units")] == ["Ada"]
        assert ids(graph, "Orders") == order_ids, "the order was read, not taken"

    def test_a_read_where_source_offers_only_its_own_matches(self):
        graph = build(
            FunctionTransitionNode("Send", look),
            ReadWhereEdge("Orders", "Send", "order", where=Filter.where(unit="Ada")),
            TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            ReturnedEdgeFromTransition("Send", "Seen"),
            units=[Unit("Ada"), Unit("Ben")], orders=[Order("Ben"), Order("Ada")],
        )
        assert fire(graph) == 1
        assert tokens(graph, "Seen") == ["Ada saw Ada"]

    def test_a_bound_read_where_edge_reads_the_unit_the_order_names(self):
        graph = build(
            FunctionTransitionNode("Look", look),
            ArgumentEdgeToTransition("Orders", "Look", "order"),
            ReadWhereEdge("Units", "Look", "unit", where=BY_ORDER),
            ReturnedEdgeFromTransition("Look", "Seen"),
            units=[Unit("Ada"), Unit("Ben")], orders=[Order("Zed"), Order("Ben")],
        )
        unit_ids, zed = ids(graph, "Units"), ids(graph, "Orders")[0]
        assert fire(graph) == 1
        assert tokens(graph, "Seen") == ["Ben saw Ben"]
        assert ids(graph, "Units") == unit_ids, "read, not taken"
        assert ids(graph, "Orders") == [zed], "the order with no unit waits"
        assert fire(graph) == 0

    def test_a_read_all_edge_is_refused_as_a_source(self):
        def every(order: list[Order], unit: Unit) -> None:
            pass

        with pytest.raises(ValueError, match="by 'Orders', whose edge gives every token rather than one"):
            build(
                FunctionTransitionNode("Send", every),
                ReadAllEdge("Orders", "Send", "order"),
                TakeWhereEdge("Units", "Send", "unit", where=BY_ORDER),
            )


# --- ports ------------------------------------------------------------------------------------------------


def depot(port: str, place_type, tokens_, keys):
    return SubnetPlaceNode("Depot", ExecutableGraphOperations.construct_graph([
        ListPlaceNode(port, place_type, tokens_, keys=keys, port=True),
    ]))


class TestPorts:

    @pytest.mark.parametrize("form, seen", [("read", "front Ada"), ("read_where", "Ben saw Ben"), ("read_all", "Ada, Ben")])
    def test_each_form_on_a_port(self, form, seen):
        yard = Port("Depot", "Yard")
        body, edge = {
            "read": (look_at_front, ReadEdge(yard, "Look", "unit")),
            "read_where": (look, ReadWhereEdge(yard, "Look", "unit", where=BY_ORDER)),
            "read_all": (count, ReadAllEdge(yard, "Look", "units")),
        }[form]
        graph = build(
            FunctionTransitionNode("Look", body),
            ArgumentEdgeToTransition("Orders", "Look", "order"),
            edge,
            ReturnedEdgeFromTransition("Look", "Seen"),
            orders=[Order("Ben")],
            places=[depot("Yard", Unit, [Unit("Ada"), Unit("Ben")], UNIT_KEYS)],
        )
        port = graph.subnet("Depot").place_named("Yard")
        port_ids = list(port.token_ids)
        assert fire(graph) == 1
        assert tokens(graph, "Seen") == [seen]
        assert list(port.token_ids) == port_ids


# --- what the build refuses ------------------------------------------------------------------------------


class TestTheBuildRefuses:

    @pytest.mark.parametrize("other", ["take", "snapshot", "mutate", "read", "read_all"])
    def test_another_input_edge_on_a_read_edges_place(self, other):
        def body(order: Order, unit: Unit, again) -> None:
            pass

        def every(order: Order, unit: Unit, again: list[Unit]) -> None:
            pass

        second = {
            "take": ArgumentEdgeToTransition("Units", "Look", "again"),
            "snapshot": SnapshotEdge("Units", "Look", "again"),
            "mutate": MutateEdge("Units", "Look", "again"),
            "read": ReadEdge("Units", "Look", "again"),
            "read_all": ReadAllEdge("Units", "Look", "again"),
        }[other]
        with pytest.raises(ValueError, match="transition 'Look' reads 'Units' through a ReadEdge and another input "
                                             "edge; a place a read edge reads is read by no other input edge"):
            build(
                FunctionTransitionNode("Look", every if other == "read_all" else body),
                ArgumentEdgeToTransition("Orders", "Look", "order"),
                ReadEdge("Units", "Look", "unit"),
                second,
            )

    def test_a_port_is_named_by_its_pair(self):
        yard = Port("Depot", "Yard")
        def body(order: Unit, units: list[Unit]) -> None:
            pass

        with pytest.raises(ValueError, match="reads port 'Yard' of 'Depot' through a ReadAllEdge"):
            build(
                FunctionTransitionNode("Count", body),
                ArgumentEdgeToTransition(yard, "Count", "order"),
                ReadAllEdge(yard, "Count", "units"),
                places=[depot("Yard", Unit, [], UNIT_KEYS)],
            )

    def test_a_place_it_reads_may_be_a_place_it_produces_into(self):
        build(
            FunctionTransitionNode("Echo", lambda order, unit: unit),
            ArgumentEdgeToTransition("Orders", "Echo", "order"),
            ReadEdge("Units", "Echo", "unit"),
            ReturnedEdgeFromTransition("Echo", "Units"),
        )

    def test_a_read_all_argument_not_annotated_as_a_list(self):
        def body(order: Order, units: Unit) -> None:
            pass

        with pytest.raises(TypeError, match="ReadAllEdge .* argument 'units' must be annotated list\\[Unit\\]"):
            build(
                FunctionTransitionNode("Count", body),
                ArgumentEdgeToTransition("Orders", "Count", "order"),
                ReadAllEdge("Units", "Count", "units"),
            )

    def test_a_read_edge_argument_annotated_as_a_list(self):
        def body(order: Order, unit: list[Unit]) -> None:
            pass

        with pytest.raises(TypeError, match="ReadEdge .* must be annotated Unit, the place's type, since the edge "
                                            "hands the body one token"):
            build(
                FunctionTransitionNode("Look", body),
                ArgumentEdgeToTransition("Orders", "Look", "order"),
                ReadEdge("Units", "Look", "unit"),
            )

    def test_a_place_of_lists_reads_one_list(self):
        def body(order: Order, row: list[int]) -> str:
            return str(sum(row))

        graph = build(
            FunctionTransitionNode("Sum", body),
            ArgumentEdgeToTransition("Orders", "Sum", "order"),
            ReadEdge("Rows", "Sum", "row"),
            ReturnedEdgeFromTransition("Sum", "Seen"),
            orders=[Order("x")], places=[ListPlaceNode("Rows", list[int], [[1, 2], [5]])],
        )
        assert fire(graph) == 1 and tokens(graph, "Seen") == ["3"]

    def test_a_filter_that_is_not_one(self):
        with pytest.raises(TypeError, match="where must be a Filter"):
            ReadWhereEdge("Units", "Look", "unit", where="hp = 3")


# --- the concurrent runtime -----------------------------------------------------------------------------


class TestTheConcurrentRuntime:

    def test_it_fires_them_and_holds_the_read_tokens(self):
        def call(caller: str, squads: list[ExecutableGraph]) -> str:
            return f"{caller}: {len(squads)}"

        graph = barracks(
            FunctionTransitionNode("Call", call),
            ArgumentEdgeToTransition("Calls", "Call", "caller"),
            ReadAllEdge("Barracks", "Call", "squads"),
            ReturnedEdgeFromTransition("Call", "Roll"),
            calls=("Sarge", "Corporal"),
        )
        summary = run(Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)))
        assert summary.quiesced and sorted(tokens(graph, "Roll")) == ["Corporal: 2", "Sarge: 2"]
        # Stage 1 notes the read tokens, which the concurrent runtime holds with the mutated ones.
        firing_ids = FiringTokenIds()
        graph.place_named("Calls").tokens.append("Clerk")
        ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
            transition=graph.transitions[0],
            transition_names_to_incoming_edges=MapTransitionNames.to_incoming_edges(graph),
            place_names_to_nodes=MapPlaceNames.to_list_place_nodes(graph),
            transition_names_to_read_edges=MapTransitionNames.to_read_edges(graph),
            firing_ids=firing_ids,
        )
        assert firing_ids.read == [("Barracks", token_id) for token_id in ids(graph, "Barracks")]
        assert firing_ids.mutated == []
        from petritype.runtime import _held
        assert _held({"Call": (graph.transitions[0], {}, firing_ids)}) == frozenset(firing_ids.read)


# --- over the wire ----------------------------------------------------------------------------------------


@petri_net(name="lookout", description="Orders look at units")
def lookout_factory() -> ExecutableGraph:
    """``Look`` takes an order and reads the unit it names; ``Front`` and ``Count``
    take a call, of which there is none, and read the front unit and every unit."""
    return build(
        FunctionTransitionNode("Look", look),
        ArgumentEdgeToTransition("Orders", "Look", "order"),
        ReadWhereEdge("Units", "Look", "unit", where=BY_ORDER),
        ReturnedEdgeFromTransition("Look", "Seen"),
        FunctionTransitionNode("Front", look_at_front),
        ArgumentEdgeToTransition("Calls", "Front", "order"),
        ReadEdge("Units", "Front", "unit"),
        ReturnedEdgeFromTransition("Front", "Seen"),
        FunctionTransitionNode("Count", count),
        ArgumentEdgeToTransition("Calls", "Count", "order"),
        ReadAllEdge("Units", "Count", "units"),
        ReturnedEdgeFromTransition("Count", "Seen"),
        units=[Unit("Ada"), Unit("Ben")], orders=[Order("Ben")], places=[ListPlaceNode("Calls", Order)],
    )


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


class TestOverTheWire:

    @pytest.mark.asyncio
    async def test_the_topology_shows_the_three_kinds(self):
        wire_ = Wire(lookout_factory)
        await wire_.send("load", {"net": "lookout"})
        state = (await wire_.send("get_state"))[-1]["result"]
        reads = [edge for edge in state["topology"]["edges"] if edge["place"] == "Units"]
        assert reads == [
            {"place": "Units", "transition": "Look", "direction": "in", "kind": "read_where",
             "filter": {"clauses": [{"key": "name", "input": {"place": "Orders", "key": "unit"}}]}},
            {"place": "Units", "transition": "Front", "direction": "in", "kind": "read"},
            {"place": "Units", "transition": "Count", "direction": "in", "kind": "read_all"},
        ]

    @pytest.mark.asyncio
    async def test_a_firing_names_only_what_it_took_and_tells_no_mutation(self):
        wire_ = Wire(lookout_factory)
        await wire_.send("load", {"net": "lookout"})
        state = (await wire_.send("get_state"))[-1]["result"]
        units = [token["id"] for token in state["marking"]["places"]["Units"]["tokens"]]
        (order,) = [token["id"] for token in state["marking"]["places"]["Orders"]["tokens"]]
        lines = await wire_.send("step")
        methods = [line.get("method") for line in lines]
        assert "mutated" not in methods
        launched = next(line for line in lines if line.get("method") == "launched")
        assert launched["params"]["consumed"] == [["Orders", order]]
        assert lines[-1]["result"]["fired"] is True and lines[-1]["result"]["transition"] == "Look"
        after = (await wire_.send("get_state"))[-1]["result"]["marking"]["places"]
        assert [token["id"] for token in after["Units"]["tokens"]] == units


class TestTolerantReading:

    def test_a_topology_with_an_unknown_kind_and_clause_is_read_whole(self):
        """The wire types read a topology whose edge has a kind and whose filter has a
        clause this version does not know: both are kept as they came, and the rest
        of the topology is read (PROTOCOL.md §5.1, §13.2)."""
        made_up = {"key": "rank", "nearest": {"place": "Flags", "by": "distance"}}
        raw = {
            "places": [{"name": "Units", "type_name": "Unit", "serializable": True, "key_names": ["rank"]}],
            "transitions": [{"name": "Rally"}],
            "edges": [
                {"place": "Units", "transition": "Rally", "direction": "in", "kind": "teleport",
                 "filter": {"clauses": [made_up, {"key": "rank", "values": [1]}]}},
                {"place": "Units", "transition": "Rally", "direction": "in", "kind": "read_all"},
                {"place": "Units", "transition": "Rally", "direction": "out"},
            ],
        }
        topology = wire.Topology.model_validate(raw)
        first = topology.edges[0]
        assert first.kind == "teleport" and first.filter["clauses"][0] == made_up
        assert [edge.kind for edge in topology.edges] == ["teleport", "read_all", None]
        assert topology.wire() == raw
        assert set(wire.EDGE_KINDS) >= {"read", "read_where", "read_all"}


# --- the drawing ------------------------------------------------------------------------------------------


class TestTheDrawing:

    def test_each_form_is_dotted_and_recoloured_on_a_firing(self):
        pytest.importorskip("rustworkx")
        from petritype.plotting.rustworkx_graph import RustworkxGraph
        from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz

        graph = lookout_factory()
        pydigraph = RustworkxGraph.from_executable_graph(graph)

        def attributes():
            _, edge_attrs = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
            return {
                (pydigraph[source].name, pydigraph[target].name): edge_attrs(data)
                for source, target, data in pydigraph.weighted_edge_list()
            }

        drawn = attributes()
        assert drawn[("Units", "Front")] == {
            "style": "dotted", "color": "dimgray", "arrowhead": "normal", "penwidth": "1",
        }
        assert drawn[("Units", "Look")] == {
            "style": "dotted", "color": "dimgray", "arrowhead": "normal", "penwidth": "1",
            "label": "name = Orders.unit",
        }
        assert drawn[("Units", "Count")] == {
            "style": "dotted", "color": "dimgray", "arrowhead": "empty", "penwidth": "1", "label": "*",
        }
        assert fire(graph) == 1
        after = attributes()
        assert after[("Units", "Look")]["color"] == "magenta"
        assert after[("Units", "Front")]["color"] == "dimgray"
        _, edge_attrs = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
        assert "style=dotted" in RustworkxToGraphviz.dot_source(pydigraph, None, edge_attrs)
