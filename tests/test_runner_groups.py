"""The protocol runner's topology fields for groups (PROTOCOL.md §5.1), driven
through ``Owner`` directly: ``topology.groups`` and ``group`` on places, subnet
places and transitions, for the loaded net and for every inner net, written only
when set. The shared transcript ``11_groups.json`` shows them over the
conformance net ``groups``; these tests add a net a token carries, the nested id,
a net with no groups, and that a firing names no group.
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from petritype import petri_net  # noqa: E402
from petritype.core.executable_graph_components import (  # noqa: E402
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.runner.owner import Owner  # noqa: E402
from petritype.runner.registry import Registry  # noqa: E402
from tests.protocol_nets.groups import box, groups  # noqa: E402
from tests.protocol_nets.levels import levels  # noqa: E402

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 21, 9, 0, 0, tzinfo=timezone.utc)
BOX = [{"place": "Box"}]


class Harness:
    """Assigns ids "1", "2", ... and lands every launch inline."""

    def __init__(self, *factories):
        self.owner = Owner(Registry.from_factories(factories))
        self.next_id = 0

    async def send(self, method, params=None):
        self.next_id += 1
        out = await self.owner.handle({"id": str(self.next_id), "method": method, "params": params or {}}, NOW)
        lines = list(out.lines)
        while out.launch is not None:
            out = await self.owner.completed(await out.launch.coroutine, NOW)
            lines.extend(out.lines)
        return lines


def result(lines):
    assert "result" in lines[-1], lines
    return lines[-1]["result"]


async def loaded(factory, net: str) -> Harness:
    harness = Harness(factory)
    result(await harness.send("load", {"net": net}))
    return harness


def by_name(entries: list) -> dict:
    return {entry["name"]: entry for entry in entries}


def mentions_group(value) -> bool:
    """Whether ``group`` or ``groups`` is a key anywhere in ``value``."""
    if isinstance(value, dict):
        return bool({"group", "groups"} & set(value)) or any(mentions_group(item) for item in value.values())
    if isinstance(value, list):
        return any(mentions_group(item) for item in value)
    return False


async def test_the_loaded_nets_topology_lists_its_groups_and_each_members_group():
    harness = await loaded(groups, "groups")
    topology = result(await harness.send("get_state"))["topology"]
    assert topology["groups"] == ["Sorting", "Packing"]
    places, transitions = by_name(topology["places"]), by_name(topology["transitions"])
    assert "group" not in places["Inbox"], "a place in no group carries no group"
    assert places["Sorted"]["group"] == "Sorting"
    assert places["Box"]["group"] == "Packing" and places["Box"]["subnet"] is True
    assert transitions["Sort"]["group"] == "Sorting" and transitions["Pack"]["group"] == "Packing"
    assert places["Box"]["ports"] == [{"name": "Tray", "type_name": "str", "serializable": True}], \
        "a port listed in its subnet place carries no group: its group is the subnet's"


async def test_an_inner_nets_topology_lists_its_own_groups_by_path_and_by_nested_id():
    harness = await loaded(groups, "groups")
    outer = result(await harness.send("get_state"))
    nested_id = outer["marking"]["places"]["Box"]["subnet"]["nested_id"]
    for address in ({"path": BOX}, {"nested_id": nested_id}):
        topology = result(await harness.send("get_state", address))["topology"]
        assert topology["groups"] == ["Sealing"], "the subnet's own groups, none of the net's"
        places, transitions = by_name(topology["places"]), by_name(topology["transitions"])
        assert places["Tray"] == {"name": "Tray", "type_name": "str", "serializable": True, "port": True,
                                  "group": "Sealing"}
        assert "group" not in places["Sealed"]
        assert transitions["Seal"]["group"] == "Sealing"


@petri_net(name="crates", description="A crate holds a box, a net carried by a token")
def crates() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Crates", ExecutableGraph, [box()], nested=True),
    ])


async def test_a_net_a_token_carries_shows_its_own_groups_and_the_net_around_it_none():
    harness = await loaded(crates, "crates")
    outer = result(await harness.send("get_state"))
    assert not mentions_group(outer["topology"]), "the net around the token declares no group"
    [held] = outer["marking"]["places"]["Crates"]["tokens"][0]["nets"]
    inner = result(await harness.send("get_state", {"path": held["path"]}))["topology"]
    assert inner["groups"] == ["Sealing"]
    assert by_name(inner["places"])["Tray"]["group"] == "Sealing"
    assert by_name(inner["places"])["Tray"]["port"] is True, "a token's net shows its port, since 2026-11-23"


async def test_a_net_with_no_groups_shows_no_groups_and_no_group_anywhere():
    harness = await loaded(levels, "levels")
    state = result(await harness.send("get_state"))
    assert not mentions_group(state["topology"])
    inner = result(await harness.send("get_state", {"path": [{"place": "Level 1"}]}))
    assert not mentions_group(inner["topology"])


async def test_a_firing_and_the_log_name_no_group():
    harness = await loaded(groups, "groups")
    lines = []
    for _ in range(3):  # Sort, Pack into the box's tray, the box's Seal
        lines += await harness.send("step")
    assert [line["result"]["transition"] for line in lines if "result" in line] == ["Sort", "Pack", "Seal"]
    lines += await harness.send("get_log")
    lines += await harness.send("get_state", {"path": BOX})
    assert not mentions_group([line for line in lines if "topology" not in line.get("result", {})])
    assert not mentions_group(result(await harness.send("get_log"))), "no log entry names a group"
