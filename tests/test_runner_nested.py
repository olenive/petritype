"""The protocol runner over nested nets (PROTOCOL.md §12), driven through
``Owner`` directly: what the shared transcript ``05_nested_nets.json`` cannot
show, chiefly the reads that wait while a body holds the token they would read
through, and the snapshots a firing inside an inner net leaves for its
ancestors.
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.tokens_as_nets.transit import transit  # noqa: E402
from petritype import petri_net  # noqa: E402
from petritype.core.executable_graph_components import (  # noqa: E402
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    NestedNets,
    ReturnedEdgeFromTransition,
)
from petritype.core.serialisation import Templates  # noqa: E402
from petritype.runner.owner import Owner  # noqa: E402
from petritype.runner.registry import Registry  # noqa: E402

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 9, 9, 0, 0, tzinfo=timezone.utc)


class Harness:
    """Assigns ids "1", "2", ... and lands every launch inline unless told not to."""

    def __init__(self, *factories):
        self.owner = Owner(Registry.from_factories(factories))
        self.next_id = 0
        self.pending = None

    async def send(self, method, params=None, *, land=True):
        self.next_id += 1
        request = {"id": str(self.next_id), "method": method, "params": params or {}}
        out = await self.owner.handle(request, NOW)
        return await self._follow(out, land)

    async def land(self):
        launch, self.pending = self.pending, None
        assert launch is not None, "nothing airborne"
        landing = await launch.coroutine
        return await self._follow(await self.owner.completed(landing, NOW), True)

    async def _follow(self, out, land):
        lines = list(out.lines)
        while out.launch is not None:
            if not land:
                self.pending = out.launch
                break
            landing = await out.launch.coroutine
            out = await self.owner.completed(landing, NOW)
            lines.extend(out.lines)
        return lines


def methods(lines):
    return [line.get("method") or ("result" if "result" in line else "error") for line in lines]


def result(lines):
    assert "result" in lines[-1], lines
    return lines[-1]["result"]


def error(lines):
    assert "error" in lines[-1], lines
    return lines[-1]["error"]["code"]


# --- a small nested net with a slow outer body ------------------------------------------------


def tick(x: int) -> int:
    return x + 1


@Templates.template("Counter")
def build_counter() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Low", int),
        ArgumentEdgeToTransition("Low", "Up", "x"),
        FunctionTransitionNode("Up", tick),
        ReturnedEdgeFromTransition("Up", "High"),
        ListPlaceNode("High", int),
    ])


class Box(BaseModel):
    label: str
    counter: ExecutableGraph

    def __petritype_nets__(self):
        return {"counter": self.counter}


GATE = {"open": None}  # an Event the slow body waits on, set per test


async def fill(box: Box) -> None:
    """Reach inside the box, then wait until the test lets the body finish."""
    NestedNets.put(box.counter, "Low", [10])
    await GATE["open"].wait()


def seed_box(label: str, low: list[int]) -> Box:
    counter = build_counter()
    NestedNets.put(counter, "Low", low)
    return Box(label=label, counter=counter)


@petri_net(name="boxes", description="Boxes holding counters")
def boxes() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Shelf", Box, [seed_box("a", [1]), seed_box("b", [2])], nested=True),
        ListPlaceNode("Spare", Box, [seed_box("c", [])], nested=True),
        MutateEdge("Shelf", "Fill", "box"),
        FunctionTransitionNode("Fill", fill, guard=lambda g: g.params["fill"]),
    ], params={"fill": False})


async def loaded_boxes():
    harness = Harness(boxes)
    await harness.send("load", {"net": "boxes"})
    state = result(await harness.send("get_state"))
    shelf = state["marking"]["places"]["Shelf"]["tokens"]
    spare = state["marking"]["places"]["Spare"]["tokens"]
    return harness, shelf, spare


# --- reads during an outer body that holds the token --------------------------------------------


class TestReadsWhileHeld:

    async def test_a_held_inner_net_is_read_after_the_landing(self):
        harness, shelf, spare = await loaded_boxes()
        held = shelf[0]["nets"][0]["nested_id"]
        free = spare[0]["nets"][0]["nested_id"]
        GATE["open"] = asyncio.Event()
        await harness.send("set_param", {"param": "fill", "value": True})
        lines = await harness.send("step", land=False)
        assert methods(lines) == ["launched"]
        # The box the body holds through its mutate edge: its counter waits.
        assert await harness.send("get_state", {"nested_id": held}) == []
        assert await harness.send("get_state", {"path": f"boxes/Shelf/#{shelf[0]['id']}/counter"}) == []
        # A box the body does not hold: read at once, live.
        free_state = result(await harness.send("get_state", {"nested_id": free}))
        assert free_state["net"] == "counter" and free_state["nested_id"] == free
        # The held box itself renders its counter, so its full value waits too.
        assert await harness.send("get_token", {"id": shelf[0]["id"]}) == []
        # The other box on the shelf is served from the outer snapshot at once.
        other = result(await harness.send("get_token", {"id": shelf[1]["id"]}))
        assert other["nets"][0]["count"] == 1
        GATE["open"].set()
        lines = await harness.land()
        assert methods(lines)[:3] == ["nested", "mutated", "distributed"], lines
        assert lines[0]["params"]["event"]["method"] == "added", "what the body put inside"
        answered = {line["id"]: line["result"] for line in lines if "result" in line}
        assert list(answered) == ["4", "5", "6", "8"], "the step, then the waiting reads in order, after the landing"
        assert answered["5"]["marking"]["places"]["Low"]["count"] == 2, "read after the body"
        assert answered["6"] == answered["5"]
        assert answered["8"]["nets"][0]["count"] == 2

    async def test_a_read_of_an_inner_net_during_an_inner_body_comes_from_its_snapshot(self):
        harness, shelf, spare = await loaded_boxes()
        nested_id = shelf[0]["nets"][0]["nested_id"]
        lines = await harness.send("step", land=False)
        assert methods(lines) == ["nested"] and lines[0]["params"]["event"]["method"] == "launched"
        assert lines[0]["params"]["nested_id"] == nested_id
        inner = result(await harness.send("get_state", {"nested_id": nested_id}))
        assert inner["marking"]["places"]["Low"]["count"] == 0, "the token the firing takes is gone from the snapshot"
        assert inner["transitions"]["Up"]["in_flight"] is True
        outer = result(await harness.send("get_state"))
        assert outer["marking"]["places"]["Shelf"]["count"] == 2, "the outer snapshot, with the holding box rendered"
        assert await harness.send("get_token", {"id": shelf[0]["id"]}) == [], "the holding box's label is at risk"
        lines = await harness.land()
        assert lines[0]["method"] == "nested" and lines[0]["params"]["event"]["method"] == "distributed"
        assert any("result" in line for line in lines), "the waiting get_token is answered"


# --- addressing, results and lifetime ----------------------------------------------------------


class TestAddressing:

    async def test_inner_state_names_and_versions(self):
        harness, shelf, spare = await loaded_boxes()
        nested_id = shelf[1]["nets"][0]["nested_id"]
        path = f"boxes/Shelf/#{shelf[1]['id']}/counter"
        by_id = result(await harness.send("get_state", {"nested_id": nested_id}))
        by_path = result(await harness.send("get_state", {"path": path}))
        assert by_id == by_path
        assert (by_id["net"], by_id["generation"], by_id["path"], by_id["nested_id"], by_id["template"]) == (
            "counter", 1, path, nested_id, "Counter",
        )
        assert by_id["marking_version"] == 0
        assert by_id["topology"]["places"][0] == {"name": "Low", "type_name": "int", "serializable": True}
        assert result(await harness.send("get_state"))["topology"]["places"][0] == {
            "name": "Shelf", "type_name": "Box", "serializable": True, "nested": True,
        }

    async def test_step_fires_inner_nets_in_order_and_versions_are_per_net(self):
        harness, shelf, spare = await loaded_boxes()
        first, second = (token["nets"][0]["nested_id"] for token in shelf)
        lines = await harness.send("step")
        assert lines[0]["params"]["nested_id"] == first
        assert result(lines) == {"fired": True, "transition": "Up", "path": f"boxes/Shelf/#{shelf[0]['id']}/counter",
                                 "nested_id": first, "marking_version": 0, "running": False}
        assert lines[0]["params"]["event"]["params"]["marking_version"] == 1, "the inner net's own version"
        assert lines[0]["params"]["marking_version"] == 0, "the outer net's, unmoved"
        lines = await harness.send("step")
        assert lines[0]["params"]["nested_id"] == second
        assert result(await harness.send("step"))["fired"] is False
        outer = result(await harness.send("get_state"))
        assert outer["progress"]["step_count"] == 2
        assert outer["progress"]["fired_counts"] == {
            f"Shelf/#{shelf[0]['id']}/counter/Up": 1, f"Shelf/#{shelf[1]['id']}/counter/Up": 1,
        }
        assert result(await harness.send("get_state", {"nested_id": first}))["progress"]["step_count"] == 1

    async def test_a_running_net_stops_when_nothing_fires_anywhere(self):
        harness, shelf, spare = await loaded_boxes()
        lines = await harness.send("start")
        assert methods(lines)[-1] == "execution_stopped"
        assert [line["params"]["event"]["params"]["transition"] for line in lines if line.get("method") == "nested"
                and line["params"]["event"]["method"] == "launched"] == ["Up", "Up"]

    async def test_inject_and_get_token_in_an_inner_net(self):
        harness, shelf, spare = await loaded_boxes()
        nested_id = spare[0]["nets"][0]["nested_id"]
        lines = await harness.send("inject", {"place": "Low", "nested_id": nested_id, "token": 5})
        assert methods(lines) == ["nested", "nested", "result"]
        assert lines[0]["params"]["event"]["method"] == "added" and lines[1]["params"]["event"]["method"] == "fireable"
        injected = result(lines)
        assert injected["marking_version"] == 1
        token = result(await harness.send("get_token", {"id": injected["id"], "nested_id": nested_id}))
        assert (token["value"], token["nested_id"], token["place"]) == (5, nested_id, "Low")
        # Ids are per net: the same id names a box on the outer shelf.
        assert result(await harness.send("get_token", {"id": injected["id"]}))["place"] == "Shelf"
        assert error(await harness.send("inject", {"place": "Low", "nested_id": nested_id, "token": "5"})) == "invalid_token"

    async def test_inject_a_nested_token_rebuilds_its_net_from_the_template(self):
        harness, shelf, spare = await loaded_boxes()
        lines = await harness.send("inject", {"place": "Spare", "token": {
            "label": "d", "counter": {"template": "Counter", "marking": {"Low": [7]}},
        }})
        assert methods(lines) == ["added", "result"]
        token = result(await harness.send("get_token", {"id": result(lines)["id"]}))
        assert token["nets"][0]["template"] == "Counter" and token["nets"][0]["count"] == 1
        assert token["value"]["counter"]["marking"] == {"Low": [7], "High": []}
        assert error(await harness.send("inject", {"place": "Spare", "token": {
            "label": "e", "counter": {"template": "Nowhere", "marking": {}},
        }})) == "invalid_token"

    async def test_get_log_by_net(self):
        harness, shelf, spare = await loaded_boxes()
        first = shelf[0]["nets"][0]["nested_id"]
        await harness.send("step")
        await harness.send("step")
        entries = result(await harness.send("get_log", {"nested_id": first}))["entries"]
        assert [(e["seq"], e["transition"], e["nested_id"]) for e in entries] == [(1, "Up", first)]
        assert entries[0]["path"] == f"boxes/Shelf/#{shelf[0]['id']}/counter"
        assert [e["seq"] for e in result(await harness.send("get_log"))["entries"]] == [1, 2]

    async def test_errors(self):
        harness, shelf, spare = await loaded_boxes()
        nested_id = shelf[0]["nets"][0]["nested_id"]
        assert error(await harness.send("get_state", {"nested_id": "999"})) == "unknown_net"
        assert error(await harness.send("get_state", {"path": "boxes/Attic/#1/counter"})) == "unknown_net"
        assert error(await harness.send("get_state", {"path": "elsewhere/Shelf/#1/counter"})) == "unknown_net"
        assert error(await harness.send("get_state", {"path": "boxes/Shelf/#1"})) == "unknown_net"
        assert error(await harness.send("get_state", {"path": "boxes", "nested_id": nested_id})) == "invalid_params"
        assert error(await harness.send("enable", {"transition": "Fill", "nested_id": nested_id})) == "invalid_params"
        assert error(await harness.send("set_param", {"param": "fill", "value": 1})) == "invalid_params"
        assert error(await harness.send("set_param", {"param": "speed", "value": 1})) == "invalid_params"
        assert result(await harness.send("get_state", {"path": "boxes"}))["net"] == "boxes", "the outer net by path"

    async def test_reset_drops_every_inner_net(self):
        harness, shelf, spare = await loaded_boxes()
        nested_id = shelf[0]["nets"][0]["nested_id"]
        await harness.send("reset")
        assert error(await harness.send("get_state", {"nested_id": nested_id})) == "unknown_net"
        fresh = result(await harness.send("get_state"))["marking"]["places"]["Shelf"]["tokens"]
        assert fresh[0]["nets"][0]["nested_id"] != nested_id


class TestTransitOverTheProtocol:

    async def test_the_cabin_is_followed_by_its_nested_id_across_a_move(self):
        harness = Harness(transit)
        await harness.send("load", {"net": "transit"})
        state = result(await harness.send("get_state"))
        bus = state["marking"]["places"]["Depot"]["tokens"][0]
        cabin = bus["nets"][0]["nested_id"]
        for transition in ("Board Depot", "Alight Depot", "Alight Central", "Arrive"):
            await harness.send("disable", {"transition": transition})
        for transition in ("Depart Central", "Depart Harbour", "Depart Airport"):
            await harness.send("disable", {"transition": transition})
        lines = await harness.send("step")  # Depart Depot
        assert result(lines)["transition"] == "Depart Depot"
        moved = result(await harness.send("get_state", {"nested_id": cabin}))
        assert moved["path"].startswith("transit/In Transit/#") and moved["nested_id"] == cabin
        assert [line["params"]["event"]["method"] for line in lines if line.get("method") == "nested"] == ["param_changed"]
        await harness.send("enable", {"transition": "Arrive"})
        lines = await harness.send("step")
        assert result(lines)["transition"] == "Arrive"
        arrived = result(await harness.send("get_state", {"nested_id": cabin}))
        assert arrived["path"].startswith("transit/Central/#")
        assert arrived["marking"]["places"]["Announcements"]["count"] == 1
