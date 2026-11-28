"""A marking is not loaded under a firing in flight (since 2026-11-28). Loading a
marking or a saved form (``import_marking``, ``NetJson.load_into``) is refused with
``HeldByBodyError``, its ``holder`` ``"in_flight"``, while a firing of the net is in
flight, or of a subnet of it at any depth, since a load fills its subnets, or of
the net around it on one of its ports: the landing would deposit into the marking
loaded. Until then the load was taken and the landing put the firing's outputs
into the new marking. A net a token carries is replaced by a load, not filled, so
nothing of it is refused. The protocol runner's ``load`` and ``reset`` wait for
the landing, as every request but a read does, and build a new net."""

import asyncio
from datetime import datetime, timezone

import pytest

from petritype import petri_net
from petritype.core.events import Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    HeldByBodyError,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    SubnetPlaceNode,
)
from petritype.core.serialisation import NetJson, Templates
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry
from petritype.runtime import ExecutionMode, RunContext, Runner

construct = ExecutableGraphOperations.construct_graph


class Gate:
    """An async body that holds its firing in flight until ``release``."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def work(self, a: int) -> int:
        await self.release.wait()
        return a * 10


def worker(gate: Gate, template: str = "Test Worker", **settings) -> ExecutableGraph:
    return construct([
        ListPlaceNode("In", int, [1], port=True), ListPlaceNode("Out", int, port=True),
        FunctionTransitionNode("Work", gate.work),
        ArgumentEdgeToTransition("In", "Work", "a"), ReturnedEdgeFromTransition("Work", "Out"),
    ], template=template, **settings)


LOADS = {
    "import_marking": lambda graph, marking: graph.import_marking(marking),
    "load_into": lambda graph, marking: NetJson.load_into(graph, {"marking": marking}),
}


async def in_flight(graph: ExecutableGraph) -> None:
    while not graph.in_flight:
        await asyncio.sleep(0.001)


@pytest.mark.parametrize("load", LOADS)
@pytest.mark.parametrize("mode", [ExecutionMode.SEQUENTIAL, ExecutionMode.CONCURRENT], ids=["sequential", "concurrent"])
def test_a_load_is_refused_while_a_firing_of_the_net_is_in_flight_and_lands_after(load, mode):
    async def go():
        gate = Gate()
        graph = worker(gate)
        run = asyncio.ensure_future(Runner.run(RunContext(graph=graph, mode=mode)))
        await in_flight(graph)
        with pytest.raises(HeldByBodyError) as refused:
            LOADS[load](graph, {"In": [5], "Out": []})
        assert refused.value.holder == "in_flight" and refused.value.whole_state
        assert "a firing of its transition 'Work' is in flight until it lands" in str(refused.value)
        assert graph.place("In").tokens == [], "nothing was loaded"
        gate.release.set()
        await run
        assert graph.place("Out").tokens == [10]
        LOADS[load](graph, {"In": [5], "Out": []})
        assert graph.place("In").tokens == [5] and graph.place("Out").tokens == []
    asyncio.run(go())


def test_a_body_is_refused_a_load_of_its_own_net():
    graph: list[ExecutableGraph] = []

    def reload(a: int) -> int:
        with pytest.raises(HeldByBodyError, match="its transition 'Reload' is in flight"):
            graph[0].import_marking({"In": [], "Out": [99]})
        return a

    graph.append(construct([
        ListPlaceNode("In", int, [1]), ListPlaceNode("Out", int),
        FunctionTransitionNode("Reload", reload),
        ArgumentEdgeToTransition("In", "Reload", "a"), ReturnedEdgeFromTransition("Reload", "Out"),
    ], open=True))
    asyncio.run(ExecutableGraphOperations.execute_graph(graph[0]))
    assert graph[0].place("Out").tokens == [1]


def test_a_load_of_the_net_around_a_subnet_is_refused_while_a_firing_of_the_subnet_is_in_flight():
    async def go():
        gate = Gate()
        Templates.register("Test Worker", lambda: worker(gate))
        try:
            world = construct([SubnetPlaceNode("Shop", Templates.build("Test Worker"))], open=True)
            shop = world.subnet("Shop")
            run = asyncio.ensure_future(ExecutableGraphOperations.execute_graph(shop))
            await in_flight(shop)
            saved = {"template": "Test Worker", "marking": {"In": [5], "Out": []}}
            with pytest.raises(HeldByBodyError, match="'Work' inside subnet place 'Shop' is in flight"):
                world.import_marking({"Shop": [saved]})
            gate.release.set()
            await run
            assert shop.place("Out").tokens == [10]
        finally:
            Templates.forget("Test Worker")
    asyncio.run(go())


def test_a_load_of_a_subnet_is_refused_while_a_firing_of_the_net_around_on_its_port_is_in_flight():
    async def go():
        gate = Gate()
        shop = construct([ListPlaceNode("In", int, port=True), ListPlaceNode("Out", int, port=True)])
        world = construct([
            ListPlaceNode("Orders", int, [1]),
            SubnetPlaceNode("Shop", shop),
            FunctionTransitionNode("Work", gate.work),
            ArgumentEdgeToTransition("Orders", "Work", "a"), ReturnedEdgeFromTransition("Work", Port("Shop", "In")),
        ], open=True)
        run = asyncio.ensure_future(ExecutableGraphOperations.execute_graph(world))
        await in_flight(world)
        with pytest.raises(HeldByBodyError, match="'Work' of the net around it, on one of its ports"):
            shop.import_marking({"In": [], "Out": [7]})
        gate.release.set()
        await run
        assert shop.place("In").tokens == [10] and shop.place("Out").tokens == []
    asyncio.run(go())


GATES: list[Gate] = []


@petri_net(name="gated-worker", description="A worker whose body waits for a gate")
def gated_worker() -> ExecutableGraph:
    GATES.append(Gate())
    return worker(GATES[-1], open=True)


@pytest.mark.parametrize("method", ["load", "reset"])
def test_the_protocol_runners_load_and_reset_wait_for_the_landing_and_build_a_new_net(method):
    async def go():
        owner = Owner(Registry.from_factories([gated_worker]))
        now = datetime(2026, 11, 28, tzinfo=timezone.utc)
        await owner.handle({"id": "1", "method": "load", "params": {"net": "gated-worker"}}, now)
        first = owner._loaded.outer.graph
        stepped = await owner.handle({"id": "2", "method": "step", "params": {}}, now)
        landing = asyncio.ensure_future(stepped.launch.coroutine)
        await in_flight(first)
        params = {"net": "gated-worker"} if method == "load" else {}
        waited = await owner.handle({"id": "3", "method": method, "params": params}, now)
        assert waited.lines == [] and owner._loaded.outer.graph is first, "it waits for the landing"
        GATES[-1].release.set()
        landed = await owner.completed(await landing, now)
        methods = [line.get("method") or line.get("id") for line in landed.lines]
        assert methods.index("distributed") < methods.index("reloaded") < methods.index("3")
        assert landed.lines[-1]["result"]["status"] == ("loaded" if method == "load" else "reset")
        assert first.place("Out").tokens == [10], "the firing landed in the net it was launched in"
        assert owner._loaded.outer.graph is not first and owner._loaded.outer.graph.place("In").tokens == [1]
    asyncio.run(go())
