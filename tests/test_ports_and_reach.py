"""Ports on every net, and the reach an edge declares (since 2026-11-23).

A net's topology shows ``port`` on its port places whichever net it is: the
loaded net, a net a token carries, a subnet (PROTOCOL.md §5.1, §12.7). A place of
net-carrying tokens declares no ports of its own: each token's net shows its own
when it is read by path or nested id. The Graphviz drawing gives a port one dotted
ring in the drawing of its own net, whichever net that is.

An input edge on a place of net-carrying tokens may name the ports its body
reaches in those tokens' nets, ``reaches=("Orders In",)``. The build refuses it
only on a place that holds no nets and for an empty name. Since 2026-11-27 it is
enforced, so that the drawing shows every way a body reaches into a closed net
(every net not declared open): a body puts tokens in, or takes them from, a port
of a closed net it holds only if its edge names the port. A write at another port
is refused with ``ClosedNetError``, its ``undeclared`` true, and fails the firing;
a write at a port of a closed net inside the net it holds is refused whatever the
edge names (``deeper``, ``tests/test_closed_nets.py``). The program, the runner, a
body writing a net it built and a net declared open are not affected, and naming
a port the body never uses, or one the net lacks, changes nothing. The program and
the runner write a unit's port only when the field around it is declared open,
a closed net shielding the nets inside it (since 2026-11-27).
"""

import asyncio
import threading
from datetime import datetime, timezone

import pytest

from petritype import petri_net
from petritype.core.events import Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ClosedNetError,
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
    TransitionFailedError,
)
from petritype.core.keys import Filter, MutateWhereEdge, ReadWhereEdge, TakeWhereEdge
from petritype.runner import protocol as wire
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry
from petritype.runtime import ExecutionMode, Extend, RunContext, Runner, SetTokens


def shoot(order: str) -> str:
    return f"Shot: {order}"


def unit(shots: tuple[str, ...] = (), ports: bool = True, open: bool = False) -> ExecutableGraph:
    """A unit: an order comes in through one tray and its shot goes out through
    another, both ports unless ``ports`` is false, with ``Fire`` between them and a
    ``Log`` of its own. ``shots`` start in ``Shots Out``; ``open`` declares it open."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders In", str, port=ports),
        ListPlaceNode("Shots Out", str, list(shots), port=ports),
        ListPlaceNode("Log", str),
        FunctionTransitionNode("Fire", shoot),
        ArgumentEdgeToTransition("Orders In", "Fire", "order"),
        ReturnedEdgeFromTransition("Fire", "Shots Out"),
    ], open=open)


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

    def test_a_net_drawn_on_its_own_dots_its_ports(self):
        """The Graphviz drawing gives a port one dotted ring in the drawing of its own net,
        whether that net is loaded on its own, carried by a token or a subnet; since
        2026-11-28, where it gave a port a second ring."""
        pytest.importorskip("rustworkx")
        from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz

        outer = field()
        carried = outer.place_named("Field").tokens[0]
        for net in (unit(), carried, outer.subnet("Reserve")):
            node_attrs, _ = RustworkxToGraphviz.activation_coloured_attr_functions(net)
            for port in ("Orders In", "Shots Out"):
                attrs = node_attrs(net.place_named(port))
                assert attrs["style"] == '"filled,dotted"' and "peripheries" not in attrs
            log = node_attrs(net.place_named("Log"))
            assert log["style"] == "filled" and "peripheries" not in log

    def test_the_simple_drawing_dots_them_too(self):
        pytest.importorskip("rustworkx")
        pytest.importorskip("matplotlib")
        from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

        net = field().place_named("Field").tokens[0]
        orders_in = SimpleGraphvizVisualization.flow_node_attr_fn(net.place_named("Orders In"))
        assert orders_in["style"] == '"filled,dotted"' and "peripheries" not in orders_in
        assert SimpleGraphvizVisualization.flow_node_attr_fn(net.place_named("Log"))["style"] == "filled"


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


def field_with(edge, body, nested: bool = True, net: ExecutableGraph | None = None,
               execution: str = "inline", open: bool = False) -> ExecutableGraph:
    """A place of units, ``Field``, holding ``net`` (a new unit by default), read by
    ``Use`` through ``edge``, which also takes an order and produces it into ``Done``;
    ``Use`` runs its body as ``execution`` says, and ``open`` declares the field open."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Field", ExecutableGraph, [net if net is not None else unit()] if nested else [], nested=nested, keys=SIZE_KEYS),
        ListPlaceNode("Orders", str, ["Hold"]),
        ListPlaceNode("Done", str),
        FunctionTransitionNode("Use", body, execution=execution),
        ArgumentEdgeToTransition("Orders", "Use", "order"),
        edge,
        ReturnedEdgeFromTransition("Use", "Done"),
    ], open=open)


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
    def test_it_is_a_tuple_or_a_list_of_names_and_never_a_bare_string(self, make, body, kind):
        """A list is kept as the tuple. A string alone is refused, so that
        ``reaches=("Orders In")``, a string for want of a comma, does not pass as
        the ports ``"O"``, ``"r"`` and so on."""
        assert make(reaches=["Orders In", "Shots Out"]).reaches == ("Orders In", "Shots Out")
        assert make(reaches=("Orders In",)).reaches == ("Orders In",)
        with pytest.raises(ValueError, match="valid tuple"):
            make(reaches="Orders In")

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


# --- the reach enforced (since 2026-11-27) --------------------------------------------------------------


REACHES_ORDERS_IN = ("Orders In",)
LIVE_EDGES = [entry for entry in INPUT_EDGES if entry[2] not in ("snapshot", "survey")]
COPYING_EDGES = [entry for entry in INPUT_EDGES if entry[2] in ("snapshot", "survey")]


def putting(port: str, catch: bool = False):
    """An action that puts the order ``"Hold"`` at ``port`` of the net it is given;
    with ``catch``, it catches a refusal itself and carries on."""
    def act(net: ExecutableGraph) -> None:
        try:
            NestedNets.put(net, port, ["Hold"])
        except ClosedNetError:
            if not catch:
                raise
    return act


def body_doing(action, many: bool = False):
    """A body that hands ``action`` the unit it is given (the first, for a list) and
    produces its order."""
    if many:
        def use_every(order: str, units: list[ExecutableGraph]) -> str:
            action(units[0])
            return order
        return use_every

    def use_one(order: str, unit: ExecutableGraph) -> str:
        action(unit)
        return order
    return use_one


def run(graph: ExecutableGraph) -> ExecutableGraph:
    """One firing of ``graph``: its outer transition's, since no unit can fire before it."""
    asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    return graph


def fails(graph: ExecutableGraph) -> TransitionFailedError:
    """The failure of the one firing of ``graph``, whose cause is a ``ClosedNetError``
    for a port the edge does not reach, as a refusal fails any firing."""
    with pytest.raises(TransitionFailedError) as failed:
        run(graph)
    refusal = failed.value.__cause__
    assert isinstance(refusal, ClosedNetError) and refusal.undeclared, refusal
    return failed.value


def tokens(net: ExecutableGraph, place: str) -> list:
    return list(net.place(place).tokens)


class TestTheReachIsEnforced:

    def test_a_put_lands_at_a_port_the_edge_reaches_and_is_refused_at_another(self):
        net = unit()
        graph = run(field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN),
                               body_doing(putting("Orders In")), net=net))
        assert tokens(net, "Orders In") == ["Hold"] and tokens(graph, "Done") == ["Hold"]
        assert not net.held_by_body
        other = unit()
        fails(field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN),
                         body_doing(putting("Shots Out")), net=other))
        assert tokens(other, "Shots Out") == [], "nothing was put"

    def test_the_refusal_names_the_port_and_how_to_name_it_and_fails_the_firing_as_any_refusal(self):
        net = unit()
        graph = field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN),
                           body_doing(putting("Shots Out")), net=net)
        graph.restore_tokens_on_failure = True
        failure = fails(graph)
        refusal = failure.__cause__
        assert isinstance(refusal, ClosedNetError)
        assert (refusal.net, refusal.place, refusal.act) == (f"#{net.net_id}", "Shots Out", "a put")
        assert isinstance(refusal, ValueError) and "place 'Shots Out'" in str(refusal)
        assert "reaches=('Shots Out',)" in str(refusal), "says what to name"
        assert failure.restored and tokens(graph, "Orders") == ["Hold"] and tokens(graph, "Done") == []
        assert tokens(graph, "Field") == [net] and not net.held_by_body

    def test_a_refusal_the_body_caught_fails_the_firing_all_the_same(self):
        net = unit()
        fails(field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN),
                         body_doing(putting("Shots Out", catch=True)), net=net))
        assert tokens(net, "Shots Out") == []

    @pytest.mark.parametrize("port", ["Orders In", "Shots Out"])
    def test_an_edge_that_reaches_nothing_may_write_no_port(self, port):
        net = unit()
        failure = fails(field_with(MutateEdge("Field", "Use", "unit"), body_doing(putting(port)), net=net))
        assert failure.__cause__.place == port and tokens(net, port) == []

    def test_a_take_is_refused_at_a_port_the_edge_does_not_reach_and_lands_at_one_it_does(self):
        taken = []

        def take_a_shot(net: ExecutableGraph) -> None:
            taken.extend(NestedNets.take(net, "Shots Out"))

        net = unit(shots=("Shot: Ready",))
        failure = fails(field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN),
                                   body_doing(take_a_shot), net=net))
        assert failure.__cause__.act == "a take" and tokens(net, "Shots Out") == ["Shot: Ready"] and taken == []
        run(field_with(MutateEdge("Field", "Use", "unit", reaches=("Shots Out",)), body_doing(take_a_shot), net=net))
        assert taken == ["Shot: Ready"] and tokens(net, "Shots Out") == []

    @pytest.mark.parametrize("command", [Extend("Shots Out", ["Hold"]), SetTokens("Shots Out", ["Hold"])],
                             ids=["Extend", "SetTokens"])
    def test_the_runtimes_commands_from_a_body_are_held_to_it_even_when_dropped(self, command):
        """A context that is not strict drops the refused command and logs it, and
        the firing fails all the same."""
        def command_it(net: ExecutableGraph) -> None:
            ctx = RunContext(graph=net, inbox=asyncio.Queue(), strict=False)
            ctx.inbox.put_nowait(command)
            Runner.drain_inbox(ctx)

        net = unit()
        run(field_with(MutateEdge("Field", "Use", "unit", reaches=("Shots Out",)), body_doing(command_it), net=net))
        assert tokens(net, "Shots Out") == ["Hold"]
        other = unit()
        fails(field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN), body_doing(command_it),
                         net=other))
        assert tokens(other, "Shots Out") == []

    @pytest.mark.parametrize("make, body, kind", LIVE_EDGES, ids=[kind for _, _, kind in LIVE_EDGES])
    def test_every_edge_that_hands_the_net_over_live_is_held_to_its_reach(self, make, body, kind):
        many = body is every
        net = unit()
        run(field_with(make(reaches=REACHES_ORDERS_IN), body_doing(putting("Orders In"), many), net=net))
        assert tokens(net, "Orders In") == ["Hold"] and not net.held_by_body
        other = unit()
        fails(field_with(make(reaches=REACHES_ORDERS_IN), body_doing(putting("Shots Out"), many), net=other))
        assert tokens(other, "Shots Out") == [] and not other.held_by_body

    @pytest.mark.parametrize("make, body, kind", COPYING_EDGES, ids=[kind for _, _, kind in COPYING_EDGES])
    def test_a_snapshot_and_a_survey_hand_over_copies_which_the_reach_does_not_bind(self, make, body, kind):
        """The body holds no copy, so it writes any port of one; the net in the place
        is untouched. The same body handed the net itself by a mutate edge is refused."""
        copies = []

        def keep_and_put(net: ExecutableGraph) -> None:
            copies.append(net)
            NestedNets.put(net, "Shots Out", ["Hold"])

        net = unit()
        graph = run(field_with(make(reaches=REACHES_ORDERS_IN), body_doing(keep_and_put, body is every), net=net))
        [copy] = copies
        assert copy is not net and tokens(copy, "Shots Out") == ["Hold"]
        assert tokens(net, "Shots Out") == [] and tokens(graph, "Done") == ["Hold"]
        fails(field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN), body_doing(keep_and_put),
                         net=unit()))

    def test_each_edge_of_a_firing_opens_only_its_own_tokens_nets_ports(self):
        def writing(**ports: str):
            def use(order: str, front: ExecutableGraph, back: ExecutableGraph) -> str:
                for name, net in (("front", front), ("back", back)):
                    if name in ports:
                        NestedNets.put(net, ports[name], [order])
                return order
            return use

        def two_fields(body, front: ExecutableGraph, back: ExecutableGraph) -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Orders", str, ["Hold"]),
                ListPlaceNode("Front", ExecutableGraph, [front], nested=True),
                ListPlaceNode("Back", ExecutableGraph, [back], nested=True),
                ListPlaceNode("Done", str),
                FunctionTransitionNode("Use", body),
                ArgumentEdgeToTransition("Orders", "Use", "order"),
                MutateEdge("Front", "Use", "front", reaches=("Orders In",)),
                MutateEdge("Back", "Use", "back", reaches=("Shots Out",)),
                ReturnedEdgeFromTransition("Use", "Done"),
            ])

        front, back = unit(), unit()
        run(two_fields(writing(front="Orders In", back="Shots Out"), front, back))
        assert tokens(front, "Orders In") == ["Hold"] and tokens(back, "Shots Out") == ["Hold"]
        for crossed in ({"front": "Shots Out"}, {"back": "Orders In"}):
            front, back = unit(), unit()
            failure = fails(two_fields(writing(**crossed), front, back))
            [port] = crossed.values()
            assert failure.__cause__.place == port
            assert tokens(front, port) == [] and tokens(back, port) == [], crossed

    @pytest.mark.parametrize("catch", [False, True], ids=["raised", "caught"])
    def test_a_body_on_a_thread_is_held_to_it(self, catch):
        threads = []

        def on_a_thread(action):
            def act(net: ExecutableGraph) -> None:
                threads.append(threading.current_thread())
                action(net)
            return act

        net = unit()
        run(field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN),
                       body_doing(on_a_thread(putting("Orders In"))), net=net, execution="thread"))
        assert tokens(net, "Orders In") == ["Hold"]
        other = unit()
        fails(field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN),
                         body_doing(on_a_thread(putting("Shots Out", catch))), net=other, execution="thread"))
        assert tokens(other, "Shots Out") == []
        assert len(threads) == 2 and threading.main_thread() not in threads

    def test_the_concurrent_runner_holds_a_body_to_it(self):
        net = unit()
        graph = field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN),
                           body_doing(putting("Shots Out")), net=net)
        with pytest.raises(TransitionFailedError) as failed:
            asyncio.run(Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)))
        assert isinstance(failed.value.__cause__, ClosedNetError) and failed.value.__cause__.undeclared
        assert tokens(net, "Shots Out") == [] and not net.held_by_body

    def test_a_body_seeds_a_net_it_builds_at_any_port_but_not_the_net_it_holds(self):
        """A net the body built is a new net, which no hold covers."""
        def built_beside(seed_the_held_one: bool) -> ExecutableGraph:
            def build(order: str, held: ExecutableGraph) -> ExecutableGraph:
                fresh = unit()
                NestedNets.put(held if seed_the_held_one else fresh, "Orders In", [order])
                return fresh

            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Orders", str, ["Hold"]),
                ListPlaceNode("Field", ExecutableGraph, [unit()], nested=True),
                ListPlaceNode("Built", ExecutableGraph, nested=True),
                FunctionTransitionNode("Use", build),
                ArgumentEdgeToTransition("Orders", "Use", "order"),
                MutateEdge("Field", "Use", "held"),
                ReturnedEdgeFromTransition("Use", "Built"),
            ])

        [fresh] = tokens(run(built_beside(False)), "Built")
        assert fresh.is_closed and tokens(fresh, "Orders In") == ["Hold"]
        graph = built_beside(True)
        fails(graph)
        assert tokens(tokens(graph, "Field")[0], "Orders In") == []

    def test_the_program_writes_any_port_where_the_body_was_refused(self):
        """Once the field around the unit is declared open: a closed field shields
        the unit from the program too (since 2026-11-27), which is refused there
        as the net around, ``around``, not as an undeclared port."""
        net = unit(shots=("Shot: Ready",))
        graph = field_with(MutateEdge("Field", "Use", "unit"), body_doing(putting("Orders In")), net=net)
        fails(graph)
        with pytest.raises(ClosedNetError) as refused:
            NestedNets.put(net, "Orders In", ["From the program"])
        assert refused.value.around == f"#{graph.net_id}" and not refused.value.undeclared
        assert tokens(net, "Orders In") == []
        net = unit(shots=("Shot: Ready",))
        graph = field_with(MutateEdge("Field", "Use", "unit"), body_doing(putting("Orders In")), net=net, open=True)
        fails(graph)
        NestedNets.put(net, "Orders In", ["From the program"])
        assert NestedNets.take(net, "Shots Out") == ["Shot: Ready"]
        ctx = RunContext(graph=net, inbox=asyncio.Queue(), strict=True)
        ctx.inbox.put_nowait(Extend("Shots Out", ["Shot: Again"]))
        assert Runner.drain_inbox(ctx) == 1
        assert tokens(net, "Orders In") == ["From the program"] and tokens(net, "Shots Out") == ["Shot: Again"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("open", [False, True], ids=["closed field", "open field"])
    async def test_the_runner_fails_the_bodys_write_and_injects_at_the_same_port(self, open):
        """The inject lands when the field around the unit is declared open, and is
        refused, ``closed_net``, inside a closed field (since 2026-11-27)."""
        edge = MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN)
        wire_ = await loaded(served(edge, body_doing(putting("Shots Out")), open=open), "use")
        [failed] = [line for line in await wire_.send("step") if line.get("method") == "failed"]
        assert failed["params"]["error"].startswith("ClosedNetError: a put at place 'Shots Out'")
        [held] = (await wire_.state())["marking"]["places"]["Field"]["tokens"][0]["nets"]
        answer = (await wire_.send("inject", {"place": "Shots Out", "path": held["path"], "token": "Shot"}))[-1]
        if open:
            assert answer["result"]["status"] == "injected"
        else:
            assert answer["error"]["code"] == "closed_net" and "is inside net" in answer["error"]["message"]
        assert (await wire_.state(path=held["path"]))["marking"]["places"]["Shots Out"]["count"] == (1 if open else 0)

    def test_only_a_net_declared_open_does_without_it(self):
        """A net declared open, whether it marks ports or not, is written by a body
        with no reach at every place; the same body is refused at a port of a closed
        net its edge does not name, and at a place of a net that marks no port,
        which is closed with nothing an edge could name."""
        def everywhere(net: ExecutableGraph) -> None:
            for place in ("Orders In", "Shots Out", "Log"):
                NestedNets.put(net, place, ["Hold"])

        for net in (unit(open=True), unit(ports=False, open=True)):
            run(field_with(MutateEdge("Field", "Use", "unit"), body_doing(everywhere), net=net))
            assert [tokens(net, place) for place in ("Orders In", "Shots Out", "Log")] == [["Hold"]] * 3
        closed = unit()
        failure = fails(field_with(MutateEdge("Field", "Use", "unit"), body_doing(everywhere), net=closed))
        assert failure.__cause__.place == "Orders In" and tokens(closed, "Orders In") == []
        portless = unit(ports=False)
        with pytest.raises(TransitionFailedError) as failed:
            run(field_with(MutateEdge("Field", "Use", "unit", reaches=REACHES_ORDERS_IN), body_doing(everywhere),
                           net=portless))
        refusal = failed.value.__cause__
        assert isinstance(refusal, ClosedNetError) and not refusal.undeclared and refusal.place == "Orders In"
        assert tokens(portless, "Orders In") == []

    def test_a_place_that_is_no_port_stays_closed_whatever_the_edge_names(self):
        """A reach opens ports alone: naming a private place opens nothing, and the
        write is refused as any write there is, not as an undeclared port."""
        net = unit()
        graph = field_with(MutateEdge("Field", "Use", "unit", reaches=("Log",)), body_doing(putting("Log")), net=net)
        with pytest.raises(TransitionFailedError) as failed:
            run(graph)
        refusal = failed.value.__cause__
        assert isinstance(refusal, ClosedNetError) and not refusal.undeclared and tokens(net, "Log") == []
        fails(field_with(MutateEdge("Field", "Use", "unit", reaches=("Log",)), body_doing(putting("Orders In")),
                         net=unit()))

    def test_naming_a_port_the_body_never_uses_or_one_the_net_lacks_changes_nothing(self):
        """``reaches`` is an upper bound on what the body may write: naming more fires
        exactly as naming what it writes, with the same firings, marking, ids and
        events, and leaving out what it writes is refused."""

        def outcome(graph: ExecutableGraph):
            asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=10))
            inner = graph.place_named("Field").tokens[0]
            return (
                graph.export_marking()["Orders"], graph.place_named("Field").token_ids, graph.fired_counts,
                inner.export_marking(), inner.place_named("Shots Out").token_ids, inner.fired_counts,
                [type(event).__name__ for event in graph.drain_events()],
            )

        exact = outcome(trays(("Orders In",)))
        assert exact[3]["Shots Out"] == ["Shot: Hold the bridge"]
        for reaches in (("Orders In", "Shots Out"), ("Orders In", "Nowhere"), ("Nowhere", "Orders In")):
            assert outcome(trays(reaches)) == exact, reaches
        failure = fails(trays(("Shots Out", "Nowhere")))
        assert failure.__cause__.place == "Orders In"


# --- 3b: the reach on the wire ----------------------------------------------------------------------------


def served(edge, body, open: bool = False):
    """A net ``use`` whose ``Use`` reads the place of units through ``edge``; ``open``
    declares it open."""

    @petri_net(name="use", description="One edge on a place of units")
    def use() -> ExecutableGraph:
        return field_with(edge, body, open=open)

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
