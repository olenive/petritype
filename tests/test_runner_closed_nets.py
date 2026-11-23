"""The runner on closed nets (since 2026-11-23, PROTOCOL.md §3, §5.1, §6.4, §12.7).

The runner's requests are outside every net: ``inject`` into a closed net lands
at one of its ports and answers ``closed_net`` at any other place, at the top
level, by a port pair and by path. A ``press`` runs a control the net declares,
which is inside the net. A net that declares itself open shows ``open: true`` in
its topology, and any other net shows no ``open``.
"""

from datetime import datetime, timezone

import pytest

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ExecutableGraph,
    ExecutableGraphOperations,
    ListPlaceNode,
    SubnetPlaceNode,
)
from petritype.core.serialisation import Templates
from petritype.runner import protocol as wire
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry
from petritype.runtime import ControlSpec

construct = ExecutableGraphOperations.construct_graph
REMEDY = "mark the place as a port, start the net with the token in its definition, or declare the net open"

@Templates.template("Runner Post")
def runner_post() -> ExecutableGraph:
    return construct([ListPlaceNode("Inbox", str, port=True), ListPlaceNode("Ledger", str)])


@Templates.template("Runner Open Post")
def runner_open_post() -> ExecutableGraph:
    return construct([ListPlaceNode("Inbox", str, port=True), ListPlaceNode("Ledger", str)], open=True)


@petri_net(name="lone-post", description="A closed post loaded on its own", controls={
    "entry": ControlSpec.inject("Ledger", lambda _clicked: "Entry", "Add an entry"),
})
def lone_post() -> ExecutableGraph:
    return runner_post()


@petri_net(name="branch", description="A closed net and an open one, each around a subnet")
def branch() -> ExecutableGraph:
    return construct([
        ListPlaceNode("Desk", str, port=True),
        SubnetPlaceNode("Branch", runner_post()),
        ListPlaceNode("Posts", ExecutableGraph, [runner_post(), runner_open_post()], nested=True),
    ])


@petri_net(name="open-branch", description="An open net around a subnet")
def open_branch() -> ExecutableGraph:
    return construct([ListPlaceNode("Desk", str, port=True), SubnetPlaceNode("Branch", runner_post())], open=True)


class Wire:
    def __init__(self, factory):
        self.owner = Owner(Registry.from_factories([factory]))
        self.next_id = 0

    async def send(self, method, params=None) -> dict:
        self.next_id += 1
        now = datetime(2026, 11, 23, tzinfo=timezone.utc)
        out = await self.owner.handle({"id": str(self.next_id), "method": method, "params": params or {}}, now)
        lines = list(out.lines)
        while out.launch is not None:
            out = await self.owner.completed(await out.launch.coroutine, now)
            lines.extend(out.lines)
        return lines[-1]


async def loaded(factory, name: str) -> Wire:
    connection = Wire(factory)
    assert "result" in await connection.send("load", {"net": name})
    return connection


def code(answer: dict) -> str:
    return answer["error"]["code"] if "error" in answer else answer["result"]["status"]


@pytest.mark.asyncio
class TestTheRunnersInject:

    async def test_at_the_top_level(self):
        connection = await loaded(lone_post, "lone-post")
        assert code(await connection.send("inject", {"place": "Inbox", "token": "Letter"})) == "injected"
        refused = await connection.send("inject", {"place": "Ledger", "token": "Forgery"})
        assert refused["error"]["code"] == "closed_net" and REMEDY in refused["error"]["message"]
        state = (await connection.send("get_state"))["result"]
        assert state["marking"]["places"]["Ledger"]["count"] == 0, "nothing was added"

    async def test_by_a_port_pair_on_a_closed_net_and_an_open_one(self):
        closed = await loaded(branch, "branch")
        assert code(await closed.send("inject", {"place": "Branch", "port": "Inbox", "token": "L"})) == "closed_net"
        opened = await loaded(open_branch, "open-branch")
        assert code(await opened.send("inject", {"place": "Branch", "port": "Inbox", "token": "L"})) == "injected"

    async def test_by_path(self):
        connection = await loaded(branch, "branch")
        ids = (await connection.send("get_state"))["result"]["marking"]["places"]["Posts"]["tokens"]
        closed, opened = ([{"place": "Posts", "token": entry["id"], "net": ""}] for entry in ids)
        subnet = [{"place": "Branch"}]
        assert code(await connection.send("inject", {"place": "Inbox", "path": closed, "token": "L"})) == "injected"
        assert code(await connection.send("inject", {"place": "Ledger", "path": closed, "token": "L"})) == "closed_net"
        assert code(await connection.send("inject", {"place": "Ledger", "path": opened, "token": "L"})) == "injected"
        assert code(await connection.send("inject", {"place": "Ledger", "path": subnet, "token": "L"})) == "closed_net"
        assert code(await connection.send("inject", {"place": "Inbox", "path": subnet, "token": "L"})) == "injected"

    async def test_a_press_of_the_nets_own_control_is_inside(self):
        connection = await loaded(lone_post, "lone-post")
        assert code(await connection.send("press", {"control": "entry"})) == "pressed"
        state = (await connection.send("get_state"))["result"]
        assert state["marking"]["places"]["Ledger"]["count"] == 1


class TestOpenOnTheWire:

    @pytest.mark.asyncio
    async def test_a_net_declared_open_shows_it_and_any_other_net_does_not(self):
        connection = await loaded(branch, "branch")
        state = (await connection.send("get_state"))["result"]
        assert "open" not in state["topology"]
        ids = state["marking"]["places"]["Posts"]["tokens"]
        closed, opened = [
            (await connection.send("get_state", {"path": [{"place": "Posts", "token": entry["id"], "net": ""}]}))["result"]
            for entry in ids
        ]
        assert "open" not in closed["topology"] and opened["topology"]["open"] is True
        top = (await (await loaded(open_branch, "open-branch")).send("get_state"))["result"]["topology"]
        assert top["open"] is True

    def test_the_wire_topology_reads_open(self):
        topology = wire.Topology.model_validate({"places": [], "transitions": [], "edges": [], "open": True})
        assert topology.open is True and wire.Topology.model_validate(
            {"places": [], "transitions": [], "edges": []}).open is None
