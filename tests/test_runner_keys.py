"""The protocol runner over token keys, queries and filters (PROTOCOL.md §13),
driven through ``Owner`` directly: keys on the wire, ``query`` and ``distinct``,
and the connection's projection (``set_filter``): what it lists, what it counts,
and how it reports the tokens it hides."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.token_view_filtering.wildlife import wildlife  # noqa: E402
from petritype import petri_net  # noqa: E402
from petritype.core.executable_graph_components import (  # noqa: E402
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReturnedEdgeFromTransition,
)
from petritype.runner.owner import Owner  # noqa: E402
from petritype.runner.registry import Registry  # noqa: E402

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 9, 9, 0, 0, tzinfo=timezone.utc)
BIRDS = {"clauses": [{"key": "class", "values": ["Bird"]}]}


class Harness:
    def __init__(self, *factories):
        self.owner = Owner(Registry.from_factories(factories))
        self.next_id = 0

    async def send(self, method, params=None):
        self.next_id += 1
        out = await self.owner.handle({"id": str(self.next_id), "method": method, "params": params or {}}, NOW)
        lines = list(out.lines)
        while out.launch is not None:
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


async def loaded_wildlife():
    harness = Harness(wildlife)
    await harness.send("load", {"net": "wildlife"})
    return harness


# --- keys on the wire ---------------------------------------------------------------------------


class TestKeysOnTheWire:

    async def test_topology_and_tokens_carry_keys(self):
        harness = await loaded_wildlife()
        state = result(await harness.send("get_state"))
        assert state["topology"]["places"][0] == {
            "name": "Forest", "type_name": "Species", "serializable": True, "key_names": ["species", "class", "diet"],
        }
        robin, fox, deer, woodpecker = state["marking"]["places"]["Forest"]["tokens"]
        assert robin["keys"] == {"species": "Robin", "class": "Bird", "diet": "Insectivore"}
        assert fox["keys"] == {"species": "Fox", "class": "Mammal", "diet": "Omnivore"}
        assert deer["keys"]["diet"] == "Herbivore" and woodpecker["keys"]["class"] == "Bird"
        assert "matching" not in state["marking"]["places"]["Forest"], "no filter set"
        page = result(await harness.send("get_place_tokens", {"place": "Tundra"}))
        assert [t["keys"]["species"] for t in page["tokens"]] == ["Fox", "Arctic Hare", "Lemming", "Snowy Owl"]

    async def test_a_place_without_keys_has_none(self):
        def grow(x: int) -> int:
            return x + 1

        @petri_net(name="plain")
        def plain():
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("In", int, [1]),
                ArgumentEdgeToTransition("In", "Grow", "x"),
                FunctionTransitionNode("Grow", grow),
                ReturnedEdgeFromTransition("Grow", "Out"),
                ListPlaceNode("Out", int),
            ])

        harness = Harness(plain)
        await harness.send("load", {"net": "plain"})
        state = result(await harness.send("get_state"))
        assert "key_names" not in state["topology"]["places"][0]
        assert "keys" not in state["marking"]["places"]["In"]["tokens"][0]
        lines = await harness.send("step")
        assert len(lines[1]["params"]["produced"][0]) == 3, "no keys, no fifth element"
        assert error(await harness.send("set_filter", {"filter": {"places": ["Nowhere"]}})) == "unknown_place"
        await harness.send("set_filter", {"filter": BIRDS})
        state = result(await harness.send("get_state"))
        assert state["marking"]["places"]["Out"] == {"count": 1, "matching": 0, "tokens": []}, "a place without keys matches no clause"

    async def test_mutated_and_produced_carry_keys(self):
        harness = await loaded_wildlife()
        await harness.send("set_param", {"param": "season", "value": "Summer"})  # no migrations
        lines = await harness.send("step")  # Spread Savanna Wetland: a keyed mutate edge and a produced copy
        assert result(lines)["transition"] == "Spread Savanna Wetland"
        mutated = next(line for line in lines if line.get("method") == "mutated")
        assert mutated["params"]["place"] == "Savanna"
        assert mutated["params"]["keys"] == {"species": "Egret", "class": "Bird", "diet": "Insectivore"}
        distributed = next(line for line in lines if line.get("method") == "distributed")
        place, token_id, label, color, keys = distributed["params"]["produced"][0]
        assert (place, color, keys["species"]) == ("Wetland", None, "Egret")
        assert token_id != mutated["params"]["id"], "the copy is a token of its own"


# --- query and distinct --------------------------------------------------------------------------


class TestQueryAndDistinct:

    async def test_query_with_a_filter(self):
        harness = await loaded_wildlife()
        answer = result(await harness.send("query", {"filter": BIRDS}))
        assert answer["count"] == 6
        assert [(m["place"], m["token"]["keys"]["species"]) for m in answer["matches"]] == [
            ("Forest", "Robin"), ("Forest", "Woodpecker"), ("Wetland", "Robin"), ("Wetland", "Heron"),
            ("Savanna", "Egret"), ("Tundra", "Snowy Owl"),
        ]
        assert answer["matches"][0]["json"]["class"] == "Bird" and answer["matches"][0]["json"]["population"] == 120
        assert answer["marking_version"] == 0

    async def test_query_restricted_and_capped(self):
        harness = await loaded_wildlife()
        mammals_there = {"clauses": [{"key": "class", "values": ["Mammal"]}], "places": ["Forest", "Tundra"]}
        answer = result(await harness.send("query", {"filter": mammals_there, "token_limit": 3}))
        assert answer["count"] == 5 and [m["token"]["keys"]["species"] for m in answer["matches"]] == ["Fox", "Deer", "Fox"]
        answer = result(await harness.send("query", {"filter": {"prefix": "Re"}}))
        assert [m["place"] for m in answer["matches"]] == ["Reef"] * 4
        everything = result(await harness.send("query"))
        assert everything["count"] == 20

    async def test_query_without_a_filter_uses_the_connections(self):
        harness = await loaded_wildlife()
        await harness.send("set_filter", {"filter": BIRDS})
        assert result(await harness.send("query"))["count"] == 6
        assert result(await harness.send("query", {"filter": {}}))["count"] == 20, "a given filter is used alone"
        assert result(await harness.send("query"))["count"] == 6, "and leaves the connection's as it was"

    async def test_distinct(self):
        harness = await loaded_wildlife()
        assert result(await harness.send("distinct", {"key": "class"})) == {
            "marking_version": 0, "key": "class", "values": ["Bird", "Mammal", "Fish", "Reptile"],
        }
        assert result(await harness.send("distinct", {"key": "diet", "places": ["Tundra"]}))["values"] == [
            "Omnivore", "Herbivore", "Carnivore",
        ]
        assert result(await harness.send("distinct", {"key": "colour"}))["values"] == []
        assert error(await harness.send("distinct", {"key": "class", "places": ["Nowhere"]})) == "unknown_place"

    async def test_bad_filters(self):
        harness = await loaded_wildlife()
        assert error(await harness.send("query", {"filter": []})) == "invalid_params"
        assert error(await harness.send("query", {"filter": {"clauses": [{"key": "class"}]}})) == "invalid_params"
        assert error(await harness.send("query", {"filter": {"clauses": [{"key": "class", "values": [1.5]}]}})) == "invalid_params"
        assert error(await harness.send("query", {"filter": {"colour": "red"}})) == "invalid_params"
        assert error(await harness.send("query", {"filter": {"places": ["Nowhere"]}})) == "unknown_place"
        assert error(await harness.send("set_filter", {"filter": {"clauses": [{"key": "class", "values": ["Bird"], "param": "x"}]}})) == "invalid_params"

    async def test_a_param_clause_reads_the_net_parameter(self):
        harness = await loaded_wildlife()
        await harness.send("set_param", {"param": "season", "value": "Bird"})
        answer = result(await harness.send("query", {"filter": {"clauses": [{"key": "class", "param": "season"}]}}))
        assert answer["count"] == 6


# --- the connection's projection -----------------------------------------------------------------


class TestProjection:

    async def test_reads_list_only_matching_tokens_and_count_both(self):
        harness = await loaded_wildlife()
        assert result(await harness.send("set_filter", {"filter": BIRDS})) == {"marking_version": 0}
        state = result(await harness.send("get_state"))
        forest = state["marking"]["places"]["Forest"]
        assert (forest["count"], forest["matching"]) == (4, 2)
        assert [t["keys"]["species"] for t in forest["tokens"]] == ["Robin", "Woodpecker"]
        assert state["marking"]["places"]["Reef"] == {"count": 4, "matching": 0, "tokens": []}
        page = result(await harness.send("get_place_tokens", {"place": "Wetland", "limit": 1}))
        assert (page["count"], page["matching"], page["has_more"]) == (4, 2, True)
        page = result(await harness.send("get_place_tokens", {"place": "Wetland", "offset": 1, "limit": 1}))
        assert [t["keys"]["species"] for t in page["tokens"]] == ["Heron"] and page["has_more"] is False
        fox = state["marking"]["places"]["Forest"]
        all_forest = result(await harness.send("set_filter")) and result(await harness.send("get_state"))
        fox_id = all_forest["marking"]["places"]["Forest"]["tokens"][1]["id"]
        await harness.send("set_filter", {"filter": BIRDS})
        assert error(await harness.send("get_token", {"id": fox_id})) == "unknown_token", "a hidden token is not shown"
        robin_id = forest["tokens"][0]["id"]
        assert result(await harness.send("get_token", {"id": robin_id}))["value"]["name"] == "Robin"
        await harness.send("set_filter")
        assert "matching" not in result(await harness.send("get_state"))["marking"]["places"]["Forest"]

    async def test_hidden_stands_in_for_added_and_removed_of_hidden_tokens(self):
        harness = await loaded_wildlife()
        await harness.send("set_filter", {"filter": BIRDS})
        gecko = {"name": "Gecko", "class": "Reptile", "diet": "Insectivore", "population": 5}
        lines = await harness.send("inject", {"place": "Forest", "token": gecko})
        assert methods(lines) == ["hidden", "result"]
        assert lines[0]["params"] == {"kind": "added", "place": "Forest", "marking_version": 1, "running": False}
        owl = {"name": "Owl", "class": "Bird", "diet": "Carnivore", "population": 5}
        lines = await harness.send("inject", {"place": "Forest", "token": owl})
        assert methods(lines) == ["added", "result"]
        assert lines[0]["params"]["keys"]["species"] == "Owl"
        assert lines[0]["params"]["marking_version"] == 2, "hidden moved the version too"
        state = result(await harness.send("get_state"))
        assert (state["marking"]["places"]["Forest"]["count"], state["marking"]["places"]["Forest"]["matching"]) == (6, 3)

    async def test_a_firing_lists_only_matching_tokens(self):
        harness = await loaded_wildlife()  # autumn by default
        await harness.send("set_filter", {"filter": BIRDS})
        lines = await harness.send("step")  # Migrate: a keyed take of the robin alone
        assert result(lines)["transition"] == "Migrate Forest Wetland"
        launched = lines[0]["params"]
        assert [place for place, _ in launched["consumed"]] == ["Forest"], "only the robin"
        distributed = next(line for line in lines if line.get("method") == "distributed")["params"]
        assert [(entry[0], entry[4]["species"]) for entry in distributed["produced"]] == [("Migrating", "Robin")]
        for migration in ("Migrate Forest Wetland", "Migrate Tundra Wetland"):
            await harness.send("disable", {"transition": migration})
        lines = await harness.send("step")  # Settle
        distributed = next(line for line in lines if line.get("method") == "distributed")["params"]
        assert [(entry[0], entry[4]["species"]) for entry in distributed["produced"]] == [("Wetland", "Robin")]
        state = result(await harness.send("get_state"))
        assert (state["marking"]["places"]["Forest"]["count"], state["marking"]["places"]["Forest"]["matching"]) == (3, 1)
        assert state["marking"]["places"]["Wetland"]["matching"] == 3

    async def test_a_mutation_in_or_out_of_the_filter_is_added_or_removed(self):
        harness = await loaded_wildlife()
        await harness.send("set_param", {"param": "season", "value": "Summer"})  # no migrations
        mammals = {"clauses": [{"key": "class", "values": ["Mammal"]}]}
        await harness.send("set_filter", {"filter": mammals})
        lines = await harness.send("step")  # Spread Savanna Wetland: the egret is a bird, hidden
        assert result(lines)["transition"] == "Spread Savanna Wetland"
        changes = [line for line in lines if line.get("method") in ("mutated", "hidden", "added", "removed")]
        assert [(c["method"], c["params"].get("kind"), c["params"]["place"]) for c in changes] == [
            ("hidden", "mutated", "Savanna"),
        ]
        distributed = next(line for line in lines if line.get("method") == "distributed")["params"]
        assert distributed["produced"] == [], "the egret's copy is hidden too"
        # A token that comes to match is reported as added, one that stops matching as removed,
        # whatever moved it: here the filter names a net parameter, and the parameter changes.
        await harness.send("set_filter", {"filter": {"clauses": [{"key": "species", "param": "season"}]}})
        lines = await harness.send("set_param", {"param": "season", "value": "Robin"})
        assert methods(lines) == ["param_changed", "added", "added", "result"], "both robins come into view"
        assert {line["params"]["place"] for line in lines[1:3]} == {"Forest", "Wetland"}
        lines = await harness.send("set_param", {"param": "season", "value": "Deer"})
        assert [(line.get("method"), line.get("params", {}).get("place")) for line in lines[1:-1]] == [
            ("removed", "Forest"), ("removed", "Wetland"), ("added", "Forest"),
        ], "the robins leave, the deer comes"
        await harness.send("disable", {"transition": "Spread Savanna Wetland"})
        lines = await harness.send("step")  # Decline Forest mutates the deer, which stays in view
        assert result(lines)["transition"] == "Decline Forest"
        changes = [line for line in lines if line.get("method") in ("mutated", "hidden", "added", "removed")]
        assert [c["method"] for c in changes] == ["mutated"] and changes[0]["params"]["keys"]["species"] == "Deer"
        lines = await harness.send("set_param", {"param": "season", "value": "Owl"})
        assert methods(lines) == ["param_changed", "removed", "result"], "and leaves it"
        lines = await harness.send("step")  # Decline again: the deer is hidden now
        changes = [line for line in lines if line.get("method") in ("mutated", "hidden", "added", "removed")]
        assert [(c["method"], c["params"].get("kind")) for c in changes] == [("hidden", "mutated")]

    async def test_a_filter_on_an_inner_net_is_per_net(self):
        from examples.tokens_as_nets.transit import transit
        harness = Harness(transit)
        await harness.send("load", {"net": "transit"})
        state = result(await harness.send("get_state"))
        cabin = state["marking"]["places"]["Depot"]["tokens"][0]["nets"][0]["nested_id"]
        await harness.send("set_filter", {"filter": {"places": ["Seats"]}, "nested_id": cabin})
        inner = result(await harness.send("get_state", {"nested_id": cabin}))
        assert inner["marking"]["places"]["Door"]["matching"] == 0
        outer = result(await harness.send("get_state"))
        assert "matching" not in outer["marking"]["places"]["Depot"], "the outer net is unfiltered"
        lines = await harness.send("step")  # Board Depot puts Ada at the cabin's door, hidden by its filter
        inner_lines = [line["params"]["event"] for line in lines if line.get("method") == "nested"]
        assert inner_lines[0]["method"] == "hidden" and inner_lines[0]["params"]["kind"] == "added"
