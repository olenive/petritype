"""Ports on every net, and the reach an edge declares (since 2026-11-23).

A net's topology shows ``port`` on its port places whichever net it is: the
loaded net, a net a token carries, a subnet (PROTOCOL.md §5.1, §12.7). A place of
net-carrying tokens declares no ports of its own: each token's net shows its own
when it is read by path or nested id. The Graphviz drawing rings a port in the
drawing of its own net, whichever net that is.

An input edge on a place of net-carrying tokens may name the ports its body
reaches in those tokens' nets, ``reaches=("Orders In",)``. It is a declaration:
the build refuses it only on a place that holds no nets and for an empty name,
and a firing is the same with it as without it, whatever ports it names.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from petritype import petri_net
from petritype.core.events import Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    NestedNets,
    ReadAllEdge,
    ReadEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    SubnetPlaceNode,
    SurveyEdge,
)
from petritype.core.keys import Filter, MutateWhereEdge, ReadWhereEdge, TakeWhereEdge
from petritype.runner import protocol as wire
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry


def shoot(order: str) -> str:
    return f"Shot: {order}"


def unit() -> ExecutableGraph:
    """A unit: an order comes in through one tray and its shot goes out through
    another, both ports, with ``Fire`` between them and a ``Log`` of its own."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders In", str, port=True),
        ListPlaceNode("Shots Out", str, port=True),
        ListPlaceNode("Log", str),
        FunctionTransitionNode("Fire", shoot),
        ArgumentEdgeToTransition("Orders In", "Fire", "order"),
        ReturnedEdgeFromTransition("Fire", "Shots Out"),
    ])


@petri_net(name="unit", description="A unit loaded on its own")
def unit_on_its_own() -> ExecutableGraph:
    return unit()


@petri_net(name="field", description="Units carried by tokens, and one in a subnet place")
def field() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Field", ExecutableGraph, [unit()], nested=True),
        SubnetPlaceNode("Reserve", unit()),
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

    async def state(self, **address) -> dict:
        lines = await self.send("get_state", address)
        assert "result" in lines[-1], lines
        return lines[-1]["result"]


async def loaded(factory, name: str) -> Wire:
    wire_ = Wire(factory)
    assert "result" in (await wire_.send("load", {"net": name}))[-1]
    return wire_


def places(topology) -> dict:
    return {place["name"]: place for place in topology["places"]}


# --- 3a: every net shows its ports ------------------------------------------------------------------------


class TestPortsShown:

    @pytest.mark.asyncio
    async def test_the_loaded_net_shows_its_ports(self):
        """A net loaded on its own has no net around it, and shows its ports all the same."""
        shown = places((await (await loaded(unit_on_its_own, "unit")).state())["topology"])
        assert shown["Orders In"]["port"] is True and shown["Shots Out"]["port"] is True
        assert "port" not in shown["Log"]

    @pytest.mark.asyncio
    async def test_a_tokens_net_read_by_path_and_by_nested_id_shows_its_ports(self):
        wire_ = await loaded(field, "field")
        outer = await wire_.state()
        [held] = outer["marking"]["places"]["Field"]["tokens"][0]["nets"]
        for address in ({"path": held["path"]}, {"nested_id": held["nested_id"]}):
            shown = places((await wire_.state(**address))["topology"])
            assert shown["Orders In"]["port"] is True and shown["Shots Out"]["port"] is True
            assert "port" not in shown["Log"]

    @pytest.mark.asyncio
    async def test_a_place_of_nets_declares_no_ports(self):
        """Nets arrive in tokens at run time and may differ: the place of nets says
        nothing of the ports its tokens' nets offer."""
        outer = await (await loaded(field, "field")).state()
        assert places(outer["topology"])["Field"] == {
            "name": "Field", "type_name": "ExecutableGraph", "serializable": True, "nested": True,
            "stepping": "inner_first",
        }

    @pytest.mark.asyncio
    async def test_a_subnet_shows_its_ports_and_its_place_lists_them_without_port(self):
        wire_ = await loaded(field, "field")
        outer = await wire_.state()
        reserve = places(outer["topology"])["Reserve"]
        assert [port["name"] for port in reserve["ports"]] == ["Orders In", "Shots Out"]
        assert all("port" not in port for port in reserve["ports"])
        shown = places((await wire_.state(path=[{"place": "Reserve"}]))["topology"])
        assert shown["Orders In"]["port"] is True and shown["Shots Out"]["port"] is True
        assert "port" not in shown["Log"]


class TestPortsDrawn:

    def test_a_net_drawn_on_its_own_rings_its_ports(self):
        """The Graphviz drawing gives a port a second ring in the drawing of its own net,
        whether that net is loaded on its own, carried by a token or a subnet."""
        pytest.importorskip("rustworkx")
        from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz

        outer = field()
        carried = outer.place_named("Field").tokens[0]
        for net in (unit(), carried, outer.subnet("Reserve")):
            node_attrs, _ = RustworkxToGraphviz.activation_coloured_attr_functions(net)
            assert node_attrs(net.place_named("Orders In"))["peripheries"] == "2"
            assert node_attrs(net.place_named("Shots Out"))["peripheries"] == "2"
            assert "peripheries" not in node_attrs(net.place_named("Log"))

    def test_the_simple_drawing_rings_them_too(self):
        pytest.importorskip("rustworkx")
        pytest.importorskip("matplotlib")
        from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

        net = field().place_named("Field").tokens[0]
        assert SimpleGraphvizVisualization.flow_node_attr_fn(net.place_named("Orders In"))["peripheries"] == "2"
        assert "peripheries" not in SimpleGraphvizVisualization.flow_node_attr_fn(net.place_named("Log"))


# --- 3b: the reach declared -------------------------------------------------------------------------------


SIZE_KEYS = {"size": lambda net: len(net.place_named("Log").tokens)}
ANY_SIZE = Filter.where(size=0)


def one(order: str, unit: ExecutableGraph) -> str:
    return order


def every(order: str, units: list[ExecutableGraph]) -> str:
    return order


# Each input edge kind, as it stands on the place of units ``Field``, with the body it needs.
INPUT_EDGES = (
    (lambda **reach: ArgumentEdgeToTransition("Field", "Use", "unit", **reach), one, "take"),
    (lambda **reach: ArgumentEdgeToTransition("Field", "Use", "units", **reach), every, "take_all"),
    (lambda **reach: TakeWhereEdge("Field", "Use", "unit", where=ANY_SIZE, **reach), one, "take_where"),
    (lambda **reach: DrainEdge("Field", "Use", "units", **reach), every, "drain"),
    (lambda **reach: SnapshotEdge("Field", "Use", "unit", **reach), one, "snapshot"),
    (lambda **reach: SurveyEdge("Field", "Use", "units", **reach), every, "survey"),
    (lambda **reach: MutateEdge("Field", "Use", "unit", **reach), one, "mutate"),
    (lambda **reach: MutateWhereEdge("Field", "Use", "unit", where=ANY_SIZE, **reach), one, "mutate_where"),
    (lambda **reach: ReadEdge("Field", "Use", "unit", **reach), one, "read"),
    (lambda **reach: ReadWhereEdge("Field", "Use", "unit", where=ANY_SIZE, **reach), one, "read_where"),
    (lambda **reach: ReadAllEdge("Field", "Use", "units", **reach), every, "read_all"),
)
KIND_IDS = [kind for _, _, kind in INPUT_EDGES]


def field_with(edge, body, nested: bool = True) -> ExecutableGraph:
    """A place of units, ``Field``, read by ``Use`` through ``edge``, which also takes
    an order and produces it into ``Done``."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Field", ExecutableGraph, [unit()] if nested else [], nested=nested, keys=SIZE_KEYS),
        ListPlaceNode("Orders", str, ["Hold"]),
        ListPlaceNode("Done", str),
        FunctionTransitionNode("Use", body),
        ArgumentEdgeToTransition("Orders", "Use", "order"),
        edge,
        ReturnedEdgeFromTransition("Use", "Done"),
    ])


class TestTheReachDeclared:

    @pytest.mark.parametrize("make, body, kind", INPUT_EDGES, ids=KIND_IDS)
    def test_every_input_edge_kind_declares_it_in_order(self, make, body, kind):
        edge = make(reaches=("Shots Out", "Orders In"))
        assert edge.reaches == ("Shots Out", "Orders In"), "kept in the order declared"
        assert make().reaches == (), "empty by default"
        field_with(edge, body)

    def test_it_is_given_by_keyword_alone(self):
        """A keyed edge's filter is still its fourth positional argument."""
        edge = TakeWhereEdge("Field", "Use", "unit", ANY_SIZE, reaches=("Orders In",))
        assert edge.where is ANY_SIZE and edge.reaches == ("Orders In",)
        assert MutateEdge("Field", "Use", "unit", reaches=["Orders In"]).reaches == ("Orders In",)

    @pytest.mark.parametrize("make, body, kind", INPUT_EDGES, ids=KIND_IDS)
    def test_the_build_refuses_it_on_a_place_that_holds_no_nets(self, make, body, kind):
        with pytest.raises(ValueError, match="holds no nets"):
            field_with(make(reaches=("Orders In",)), body, nested=False)

    def test_a_port_of_a_subnet_place_counts_by_its_own_nested(self):
        def subnet(nested: bool) -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Bay", ExecutableGraph, [unit()] if nested else [], nested=nested, port=True),
            ])

        def around(nested: bool) -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                SubnetPlaceNode("Hangar", subnet(nested)),
                ListPlaceNode("Done", str),
                FunctionTransitionNode("Use", lambda unit: "used"),
                MutateEdge(Port("Hangar", "Bay"), "Use", "unit", reaches=("Orders In",)),
                ReturnedEdgeFromTransition("Use", "Done"),
            ])

        assert around(nested=True).mutate_edges[0].reaches == ("Orders In",)
        with pytest.raises(ValueError, match="port 'Bay' of 'Hangar'.*holds no nets"):
            around(nested=False)

    @pytest.mark.parametrize("names", [("",), ("Orders In", "")])
    def test_the_build_refuses_an_empty_name(self, names):
        with pytest.raises(ValueError, match="empty string"):
            field_with(MutateEdge("Field", "Use", "unit", reaches=names), one)

    def test_a_name_is_a_string(self):
        with pytest.raises(ValueError):
            MutateEdge("Field", "Use", "unit", reaches="Orders In")

    def test_an_output_edge_has_none(self):
        with pytest.raises(ValueError):
            ReturnedEdgeFromTransition("Use", "Field", reaches=("Orders In",))


def hand_over(order: str, unit: ExecutableGraph) -> None:
    NestedNets.put(unit, "Orders In", [order])


def trays(reaches: tuple[str, ...]) -> ExecutableGraph:
    """An order handed to the unit in ``Field`` through its ``Orders In``, by an
    edge that declares ``reaches``; the unit's ``Fire`` then shoots it out."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders", str, ["Hold the bridge"]),
        ListPlaceNode("Field", ExecutableGraph, [unit()], nested=True),
        FunctionTransitionNode("Hand Over", hand_over),
        ArgumentEdgeToTransition("Orders", "Hand Over", "order"),
        MutateEdge("Field", "Hand Over", "unit", reaches=reaches),
    ], record_events=True)


class TestNoBehaviourChanges:

    @pytest.mark.parametrize("reaches", [("Orders In",), ("Nowhere",), ("Shots Out", "Nowhere")])
    def test_a_firing_is_the_same_whatever_ports_it_names(self, reaches):
        """Naming a port the token's net lacks, or one the body does not touch, fires
        exactly as declaring nothing does: the same firings, marking, ids and events."""

        def outcome(graph: ExecutableGraph):
            asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=10))
            inner = graph.place_named("Field").tokens[0]
            return (
                graph.export_marking()["Orders"], graph.place_named("Field").token_ids, graph.fired_counts,
                inner.export_marking(), inner.place_named("Shots Out").token_ids, inner.fired_counts,
                [type(event).__name__ for event in graph.drain_events()],
            )

        declared, plain = outcome(trays(reaches)), outcome(trays(()))
        assert declared == plain
        assert declared[3]["Shots Out"] == ["Shot: Hold the bridge"]


# --- 3b: the reach on the wire ----------------------------------------------------------------------------


def served(edge, body):
    """A net ``use`` whose ``Use`` reads the place of units through ``edge``."""

    @petri_net(name="use", description="One edge on a place of units")
    def use() -> ExecutableGraph:
        return field_with(edge, body)

    return use


class TestTheReachOnTheWire:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("make, body, kind", INPUT_EDGES, ids=KIND_IDS)
    async def test_an_input_edge_writes_it_in_order_and_leaves_it_out_when_empty(self, make, body, kind):
        expected = {"place": "Field", "transition": "Use", "direction": "in", "kind": kind}
        if kind.endswith("_where"):
            expected["filter"] = {"clauses": [{"key": "size", "values": [0]}]}
        for reaches, written in ((("Shots Out", "Orders In"), {"reaches": ["Shots Out", "Orders In"]}), ((), {})):
            edges = (await (await loaded(served(make(reaches=reaches), body), "use")).state())["topology"]["edges"]
            assert [edge for edge in edges if edge["place"] == "Field"] == [{**expected, **written}]
            assert all("reaches" not in edge for edge in edges if edge["place"] != "Field")

    @pytest.mark.asyncio
    async def test_an_edge_on_a_port_writes_it_beside_the_port(self):
        @petri_net(name="hangar", description="Units in the bay of a hangar")
        def hangar() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                SubnetPlaceNode("Hangar", ExecutableGraphOperations.construct_graph([
                    ListPlaceNode("Bay", ExecutableGraph, [unit()], nested=True, port=True),
                ])),
                ListPlaceNode("Done", str),
                FunctionTransitionNode("Use", lambda unit: "used"),
                MutateEdge(Port("Hangar", "Bay"), "Use", "unit", reaches=("Orders In",)),
                ReturnedEdgeFromTransition("Use", "Done"),
            ])

        edges = (await (await loaded(hangar, "hangar")).state())["topology"]["edges"]
        assert {"place": "Hangar", "port": "Bay", "transition": "Use", "direction": "in", "kind": "mutate",
                "reaches": ["Orders In"]} in edges


class TestTolerantReading:

    def test_a_topology_with_reaches_and_fields_it_does_not_know_is_read_whole(self):
        """The topology's models keep a field they do not know as it came and read the
        rest; ``reaches`` is read into ``Edge.reaches``."""
        raw = {
            "places": [
                {"name": "Field", "type_name": "Unit", "serializable": True, "nested": True,
                 "stepping": "inner_first", "shade": "olive"},
                {"name": "Hangar", "type_name": "Hangar", "subnet": True, "stepping": "inner_first",
                 "ports": [{"name": "Bay", "type_name": "Unit", "serializable": True, "depth": 2}], "doors": 2},
            ],
            "transitions": [{"name": "Hand Over", "cost": 1}],
            "edges": [
                {"place": "Field", "transition": "Hand Over", "direction": "in", "kind": "mutate",
                 "reaches": ["Orders In"], "weight": 3},
                {"place": "Field", "transition": "Hand Over", "direction": "out"},
            ],
            "layers": ["ground"],
        }
        topology = wire.Topology.model_validate(raw)
        first = topology.edges[0]
        assert first.reaches == ["Orders In"] and first.model_extra == {"weight": 3}
        assert topology.edges[1].reaches is None
        assert isinstance(topology.places[1], wire.SubnetPlace) and topology.places[1].ports[0].model_extra == {
            "depth": 2,
        }
        assert topology.wire() == raw

    @pytest.mark.asyncio
    async def test_a_state_whose_edge_has_a_field_it_does_not_know_is_read_whole(self):
        state = await (await loaded(served(MutateEdge("Field", "Use", "unit", reaches=("Orders In",)), one), "use")).state()
        for edge in state["topology"]["edges"]:
            edge["weight"] = 3
        assert wire.State.model_validate(state).wire() == state

    def test_the_other_models_still_refuse_a_field_they_do_not_know(self):
        with pytest.raises(ValueError):
            wire.Token.model_validate({"id": "1", "label": "Ada", "weight": 3})


# --- 3b: the reach drawn ----------------------------------------------------------------------------------


def drawn_edge(graph: ExecutableGraph, place: str = "Field") -> dict:
    """The Graphviz attributes of the one edge from ``place`` to ``Use``."""
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz

    pydigraph = RustworkxGraph.from_executable_graph(graph)
    _, edge_attrs = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
    [attributes] = [
        edge_attrs(data) for source, target, data in pydigraph.weighted_edge_list()
        if (pydigraph[source].name, pydigraph[target].name) == (place, "Use")
    ]
    return attributes


class TestTheReachDrawn:

    @pytest.mark.parametrize("make, body, kind", INPUT_EDGES, ids=KIND_IDS)
    def test_every_input_edge_kind_writes_it_as_its_labels_last_line(self, make, body, kind):
        first = ["*"] if kind == "read_all" else ["size in {0}"] if kind.endswith("_where") else []
        declared = drawn_edge(field_with(make(reaches=("Orders In", "Shots Out")), body))
        assert declared["label"] == "\\n".join([*first, "reaches Orders In, Shots Out"])
        plain = drawn_edge(field_with(make(), body))
        assert plain.get("label") == ("\\n".join(first) if first else None), "an edge that reaches nothing as before"
        assert {key: value for key, value in declared.items() if key != "label"} == {
            key: value for key, value in plain.items() if key != "label"
        }

    def test_a_name_is_written_as_it_is(self):
        graph = field_with(MutateEdge("Field", "Use", "unit", reaches=('Tray "A"', "In\\Out")), one)
        assert drawn_edge(graph)["label"] == 'reaches Tray \\"A\\", In\\\\Out'

    def test_the_dot_source_carries_the_line(self):
        pytest.importorskip("rustworkx")
        from petritype.plotting.rustworkx_graph import RustworkxGraph
        from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz

        graph = field_with(MutateWhereEdge("Field", "Use", "unit", where=ANY_SIZE, reaches=("Orders In",)), one)
        _, edge_attrs = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
        source = RustworkxToGraphviz.dot_source(RustworkxGraph.from_executable_graph(graph), None, edge_attrs)
        assert 'label="size in {0}\\nreaches Orders In"' in source
