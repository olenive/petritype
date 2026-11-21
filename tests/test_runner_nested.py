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


def step(place, token, net):
    """One step of a path (PROTOCOL.md §12.1): the net ``net`` in token ``token`` of ``place``."""
    return {"place": place, "token": token, "net": net}


def message(lines):
    assert "error" in lines[-1], lines
    return lines[-1]["error"]["message"]


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
        assert await harness.send("get_state", {"path": [step("Shelf", shelf[0]["id"], "counter")]}) == []
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
        path = [step("Shelf", shelf[1]["id"], "counter")]
        assert shelf[1]["nets"][0]["path"] == path, "a token's nets name their path from the loaded net"
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
        assert lines[0]["params"]["path"] == [step("Shelf", shelf[0]["id"], "counter")]
        assert result(lines) == {"fired": True, "transition": "Up", "path": [step("Shelf", shelf[0]["id"], "counter")],
                                 "nested_id": first, "marking_version": 0, "running": False}
        assert lines[0]["params"]["event"]["params"]["marking_version"] == 1, "the inner net's own version"
        assert lines[0]["params"]["marking_version"] == 0, "the outer net's, unmoved"
        lines = await harness.send("step")
        assert lines[0]["params"]["nested_id"] == second
        assert result(await harness.send("step"))["fired"] is False
        # Progress is per net (§5.4): the outer net fired nothing of its own.
        outer = result(await harness.send("get_state"))
        assert outer["progress"] == {"step_count": 0, "last_fired": None, "fired_counts": {}, "last_progress_at": None}
        for nested_id in (first, second):
            progress = result(await harness.send("get_state", {"nested_id": nested_id}))["progress"]
            assert (progress["step_count"], progress["last_fired"], progress["fired_counts"]) == (1, "Up", {"Up": 1})

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
        assert entries[0]["path"] == [step("Shelf", shelf[0]["id"], "counter")]
        assert [e["seq"] for e in result(await harness.send("get_log"))["entries"]] == [1, 2]
        by_path = await harness.send("get_log", {"path": [step("Shelf", shelf[0]["id"], "counter")]})
        assert result(by_path)["entries"] == entries

    async def test_errors(self):
        harness, shelf, spare = await loaded_boxes()
        nested_id = shelf[0]["nets"][0]["nested_id"]
        assert error(await harness.send("get_state", {"nested_id": "999"})) == "unknown_net"
        assert error(await harness.send("get_state", {"path": [step("Attic", "1", "counter")]})) == "unknown_net"
        assert error(await harness.send("get_state", {"path": [], "nested_id": nested_id})) == "invalid_params"
        assert error(await harness.send("enable", {"transition": "Fill", "nested_id": nested_id})) == "invalid_params"
        assert error(await harness.send("enable", {"transition": "Fill", "path": []})) == "invalid_params"
        assert error(await harness.send("set_param", {"param": "fill", "value": 1})) == "invalid_params"
        assert error(await harness.send("set_param", {"param": "speed", "value": 1})) == "invalid_params"

    async def test_the_empty_path_names_the_loaded_net(self):
        harness, shelf, spare = await loaded_boxes()
        by_path = result(await harness.send("get_state", {"path": []}))
        assert by_path == result(await harness.send("get_state")), "the same read, with no path or nested_id in it"
        assert by_path["net"] == "boxes" and "path" not in by_path and "nested_id" not in by_path
        page = result(await harness.send("get_place_tokens", {"place": "Shelf", "path": []}))
        assert page["count"] == 2 and "path" not in page
        injected = result(await harness.send("inject", {"place": "Spare", "path": [], "token": {
            "label": "d", "counter": {"template": "Counter", "marking": {}}}}))
        assert set(injected) == {"status", "id", "marking_version"}

    async def test_a_path_of_the_wrong_shape_is_invalid_params(self):
        """§12.1: a path that is not a list, a step without ``place``, a step with
        one of ``token`` and ``net`` without the other, and a step value that is
        not a string. The old text form is refused, with a message that says what
        a path is."""
        harness, shelf, spare = await loaded_boxes()
        token = shelf[0]["id"]
        text = await harness.send("get_state", {"path": f"boxes/Shelf/#{token}/counter"})
        assert error(text) == "invalid_params" and "a list of steps" in message(text)
        for path in (
            {"place": "Shelf", "token": token, "net": "counter"},   # one step, not a list of them
            ["Shelf", token, "counter"],                            # steps that are not objects
            [{"token": token, "net": "counter"}],                   # no place
            [{"place": "Shelf", "token": token}],                   # a token without its net
            [{"place": "Shelf", "net": "counter"}],                 # a net without its token
            [{"place": "Shelf", "token": int(token), "net": "counter"}],  # a token id is a string
            [{"place": 3, "token": token, "net": "counter"}],
            [{"place": "Shelf", "token": token, "net": None}],
            [step("Shelf", token, "counter"), "Low"],
        ):
            lines = await harness.send("get_state", {"path": path})
            assert error(lines) == "invalid_params", path
        lines = await harness.send("get_place_tokens", {"place": "Low", "path": [{"place": "Shelf", "token": token}]})
        assert error(lines) == "invalid_params" and "step 0" in message(lines)
        assert error(await harness.send("inject", {"place": "Low", "token": 1, "path": "boxes"})) == "invalid_params"
        assert error(await harness.send("query", {"path": 7})) == "invalid_params"

    async def test_unknown_net_names_the_step_that_failed(self):
        harness, shelf, spare = await loaded_boxes()
        token = shelf[0]["id"]
        for path, failing in (
            ([step("Attic", token, "counter")], 0),                        # no such place
            ([step("Shelf", "999", "counter")], 0),                        # no such token there
            ([step("Shelf", "01", "counter")], 0),                         # only the canonical spelling names a token
            ([step("Shelf", token, "boot")], 0),                           # the token holds no net of that name
            ([{"place": "Shelf"}], 0),                                     # a subnet step: the net has no subnet places
            ([step("Shelf", token, "counter"), step("Low", "1", "x")], 1),  # Low holds no nets
            ([step("Shelf", token, "counter"), {"place": "High"}], 1),
        ):
            lines = await harness.send("get_state", {"path": path})
            assert error(lines) == "unknown_net", path
            assert message(lines).startswith(f"step {failing} of the path:"), message(lines)
        assert result(await harness.send("get_state", {"path": [step("Shelf", token, "counter")]}))["net"] == "counter"

    async def test_reset_drops_every_inner_net(self):
        harness, shelf, spare = await loaded_boxes()
        nested_id = shelf[0]["nets"][0]["nested_id"]
        await harness.send("reset")
        assert error(await harness.send("get_state", {"nested_id": nested_id})) == "unknown_net"
        fresh = result(await harness.send("get_state"))["marking"]["places"]["Shelf"]["tokens"]
        assert fresh[0]["nets"][0]["nested_id"] != nested_id


# --- names that the text form of version 1 could not spell, and two depths ------------------------


class Odd(BaseModel):
    """A token whose one net has a name with a slash in it."""

    label: str
    inside: ExecutableGraph

    def __petritype_nets__(self):
        return {"in/side": self.inside}


def odd_inside() -> ExecutableGraph:
    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("#Low", int),
        ArgumentEdgeToTransition("#Low", "Up/1", "x"),
        FunctionTransitionNode("Up/1", tick),
        ReturnedEdgeFromTransition("Up/1", "#High/"),
        ListPlaceNode("#High/", int),
    ])
    NestedNets.put(graph, "#Low", [1])
    graph.drain_events()
    return graph


@petri_net(name="odd/names", description="Names with a slash or a leading hash")
def odd_names() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("#Shelf/A", Odd, [Odd(label="o", inside=odd_inside())], nested=True),
    ])


class Nest(BaseModel):
    label: str
    inner: ExecutableGraph

    def __petritype_nets__(self):
        return {"inner": self.inner}


def nest_level(child: Nest | None = None) -> ExecutableGraph:
    """A counter with one token to count, holding ``child`` in its place ``Inside``."""
    nodes = [
        ListPlaceNode("Low", int),
        ArgumentEdgeToTransition("Low", "Up", "x"),
        FunctionTransitionNode("Up", tick),
        ReturnedEdgeFromTransition("Up", "High"),
        ListPlaceNode("High", int),
    ]
    if child is not None:
        nodes.append(ListPlaceNode("Inside", Nest, [child], nested=True))
    graph = ExecutableGraphOperations.construct_graph(nodes)
    NestedNets.put(graph, "Low", [1])
    graph.drain_events()
    return graph


def ring(bell: int) -> int:
    return bell


@petri_net(name="nests", description="A bell, and a counter in a counter")
def nests() -> ExecutableGraph:
    middle = Nest(label="middle", inner=nest_level(Nest(label="deep", inner=nest_level())))
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Bell", int, [1]),
        ArgumentEdgeToTransition("Bell", "Ring", "bell"),
        FunctionTransitionNode("Ring", ring),
        ReturnedEdgeFromTransition("Ring", "Rung"),
        ListPlaceNode("Rung", int),
        ListPlaceNode("Top", Nest, [middle], nested=True),
    ])


class TestNamesAndDepth:

    async def test_names_with_a_slash_or_a_leading_hash_are_addresses_like_any_other(self):
        harness = Harness(odd_names)
        await harness.send("load", {"net": "odd/names"})
        state = result(await harness.send("get_state"))
        token = state["marking"]["places"]["#Shelf/A"]["tokens"][0]
        path = [step("#Shelf/A", token["id"], "in/side")]
        assert token["nets"][0]["path"] == path and token["nets"][0]["name"] == "in/side"
        nested_id = token["nets"][0]["nested_id"]
        inner = result(await harness.send("get_state", {"path": path}))
        assert (inner["net"], inner["path"], inner["nested_id"]) == ("in/side", path, nested_id)
        assert list(inner["marking"]["places"]) == ["#Low", "#High/"]
        page = result(await harness.send("get_place_tokens", {"place": "#Low", "path": path}))
        assert page["count"] == 1 and page["path"] == path
        lines = await harness.send("step")
        wrapped = [line["params"]["path"] for line in lines if line.get("method") == "nested"]
        assert wrapped and all(each == path for each in wrapped), "the nested lines carry the path as given"
        assert result(lines)["path"] == path and result(lines)["transition"] == "Up/1"
        entries = result(await harness.send("get_log", {"path": path}))["entries"]
        assert [(entry["transition"], entry["path"]) for entry in entries] == [("Up/1", path)]
        progress = result(await harness.send("get_state", {"path": path}))["progress"]
        assert (progress["fired_counts"], progress["last_fired"]) == ({"Up/1": 1}, "Up/1")
        assert result(await harness.send("get_state"))["progress"]["fired_counts"] == {}

    async def test_progress_is_per_net_at_two_depths(self):
        harness = Harness(nests)
        await harness.send("load", {"net": "nests"})
        top = result(await harness.send("get_state"))["marking"]["places"]["Top"]["tokens"][0]
        middle_path = top["nets"][0]["path"]
        middle = result(await harness.send("get_state", {"path": middle_path}))
        deep_token = middle["marking"]["places"]["Inside"]["tokens"][0]
        deep_path = deep_token["nets"][0]["path"]
        assert deep_path == [step("Top", top["id"], "inner"), step("Inside", deep_token["id"], "inner")]
        fired = []
        while (answer := result(await harness.send("step")))["fired"]:
            fired.append((answer["transition"], answer.get("path")))
        assert fired == [("Ring", None), ("Up", middle_path), ("Up", deep_path)], "outer first, then down"
        counts = {}
        for name, params in (("outer", {}), ("middle", {"path": middle_path}), ("deep", {"path": deep_path})):
            progress = result(await harness.send("get_state", params))["progress"]
            counts[name] = (progress["step_count"], progress["last_fired"], progress["fired_counts"])
        assert counts == {
            "outer": (1, "Ring", {"Ring": 1}),
            "middle": (1, "Up", {"Up": 1}),
            "deep": (1, "Up", {"Up": 1}),
        }, "each net counts its own firings under their own names, and none of the nets inside it"
        log = result(await harness.send("get_log", {"path": middle_path}))["entries"]
        assert [entry.get("path") for entry in log] == [middle_path, deep_path], "a net's log holds the nets inside it"
        wrong = await harness.send("get_state", {"path": [*middle_path, step("Inside", "999", "inner")]})
        assert error(wrong) == "unknown_net" and message(wrong).startswith("step 1 of the path:")


class TestTransitOverTheProtocol:

    async def test_the_cabin_is_followed_by_its_nested_id_across_a_move(self):
        harness = Harness(transit)
        await harness.send("load", {"net": "transit"})
        state = result(await harness.send("get_state"))
        bus = state["marking"]["places"]["Depot"]["tokens"][0]
        cabin = bus["nets"][0]["nested_id"]
        fired = []
        while not fired or fired[-1] != "Depart Depot":  # boarding, settling, the doors closing
            lines = await harness.send("step")
            fired.append(result(lines)["transition"])
        assert len(fired) == 10
        assert [line["params"]["event"]["method"] for line in lines if line.get("method") == "nested"] == ["param_changed"]
        moved = result(await harness.send("get_state", {"nested_id": cabin}))
        in_transit = result(await harness.send("get_place_tokens", {"place": "In Transit"}))["tokens"][0]["id"]
        assert moved["path"] == [step("In Transit", in_transit, "cabin")] and moved["nested_id"] == cabin
        lines = await harness.send("step")
        assert result(lines)["transition"] == "Arrive"
        assert [(line["params"]["event"]["method"], line["params"]["event"]["params"].get("place"))
                for line in lines if line.get("method") == "nested"][:4] == [
            ("param_changed", None), ("removed", "Doors Closed"), ("added", "Doors Open"), ("added", "Announcements"),
        ], "stopped, the doors opened, and the announcement, all by the outer body"
        arrived = result(await harness.send("get_state", {"nested_id": cabin}))
        at_central = result(await harness.send("get_place_tokens", {"place": "Central"}))["tokens"][0]["id"]
        assert arrived["path"] == [step("Central", at_central, "cabin")]
        assert result(await harness.send("get_state", {"path": arrived["path"]}))["nested_id"] == cabin
        assert arrived["marking"]["places"]["Announcements"]["count"] == 1
        assert arrived["marking"]["places"]["Doors Closed"]["count"] == 0
        assert result(await harness.send("get_token", {"id": arrived["marking"]["places"]["Doors Open"]["tokens"][0]["id"],
                                                       "nested_id": cabin}))["value"] == "Front"
        assert arrived["transitions"]["Go To Door"]["fireable"] is True

    async def test_stepping_over_the_protocol_delivers_every_passenger(self):
        harness = Harness(transit)
        await harness.send("load", {"net": "transit"})
        for _ in range(45):
            assert result(await harness.send("step"))["fired"] is True
        arrived = result(await harness.send("get_place_tokens", {"place": "Arrived"}))
        assert arrived["count"] == 7
        for stop in ("Depot", "Central", "Harbour", "Airport"):
            assert result(await harness.send("get_place_tokens", {"place": f"Platform@{stop}"}))["count"] == 0
        assert result(await harness.send("step"))["fired"] is True, "the vehicles go on round, empty"
