"""The protocol wording gaps settled on 2026-10-12 (PROTOCOL.md; the decisions
A2 to A8) and the engine's two scheduling policies as the runner honours them
(the round-robin tie-break and the interleaved deep step), driven through
``Owner`` directly. The fold of A1 is pinned in ``tests/test_protocol_transcripts.py``
and the policies themselves in ``tests/test_engine_policies.py``."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.token_view_filtering.wildlife import wildlife  # noqa: E402
from examples.tokens_as_nets.transit import transit  # noqa: E402
from petritype import petri_net  # noqa: E402
from petritype.core.executable_graph_components import (  # noqa: E402
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    NestedNets,
    ReturnedEdgeFromTransition,
)
from petritype.core.serialisation import Templates  # noqa: E402
from petritype.runner.owner import Owner  # noqa: E402
from petritype.runner.registry import Registry  # noqa: E402

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)


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


def result(lines):
    assert "result" in lines[-1], lines
    return lines[-1]["result"]


def error(lines):
    assert "error" in lines[-1], lines
    return lines[-1]["error"]["code"]


def nested_events(lines, method=None):
    """The events inside ``nested`` lines, as ``(nested_id, event)``."""
    return [(line["params"]["nested_id"], line["params"]["event"]) for line in lines
            if line.get("method") == "nested" and (method is None or line["params"]["event"]["method"] == method)]


# --- a net whose outer bodies reach into crates holding tallies ---------------------------------


def tick(x: int) -> int:
    return x + 1


@Templates.template("Tally")
def build_tally() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Low", int),
        ArgumentEdgeToTransition("Low", "Up", "x"),
        FunctionTransitionNode("Up", tick),
        ReturnedEdgeFromTransition("Up", "High"),
        ListPlaceNode("High", int),
    ])


class Crate(BaseModel):
    label: str
    tally: ExecutableGraph

    def __petritype_nets__(self):
        return {"tally": self.tally}


def fill(crate: Crate) -> Crate:
    """Reach into the crate: its tally gets something to count."""
    NestedNets.put(crate.tally, "Low", [10])
    return crate


def make(label: str) -> Crate:
    return Crate(label=label, tally=build_tally())


@petri_net(name="crates", description="Crates are made, then filled")
def crates(seeded: bool = False, labels: Optional[list[str]] = None) -> ExecutableGraph:
    """Passive places, so the driver never fires a tally: only the outer bodies
    change one, which is what a ``fireable`` baseline is about."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Labels", str, list(labels or [])),
        ListPlaceNode("Spare", Crate, [make("seed")] if seeded else [], nested=True, passive=True),
        ListPlaceNode("Shelf", Crate, nested=True, passive=True),
        FunctionTransitionNode("Make", make),
        ArgumentEdgeToTransition("Labels", "Make", "label"),
        ReturnedEdgeFromTransition("Make", "Spare"),
        FunctionTransitionNode("Fill", fill),
        ArgumentEdgeToTransition("Spare", "Fill", "crate"),
        ReturnedEdgeFromTransition("Fill", "Shelf"),
    ])


async def tally_of(harness, place):
    state = result(await harness.send("get_state"))
    return state["marking"]["places"][place]["tokens"][0]["nets"][0]


class TestFireableBaselines:
    """A7 (§7.2, §12.3): an inner net's baseline is taken when the runtime first
    knows of it, so the first ``nested`` ``fireable`` reports the first change."""

    async def test_a_net_the_factory_seeded_is_known_from_the_load(self):
        harness = Harness(crates)
        lines = await harness.send("load", {"net": "crates", "params": {"seeded": True}})
        assert nested_events(lines) == [], "no fireable for a baseline itself"
        tally = (await tally_of(harness, "Spare"))["nested_id"]
        lines = await harness.send("step")  # Fill: the first change the tally sees
        assert result(lines)["transition"] == "Fill"
        assert [(nid, event["params"]["transitions"]) for nid, event in nested_events(lines, "fireable")] == [
            (tally, ["Up"]),
        ]

    async def test_a_net_an_inject_carried_is_known_from_the_inject(self):
        harness = Harness(crates)
        await harness.send("load", {"net": "crates"})
        lines = await harness.send("inject", {"place": "Spare", "token": {
            "label": "i", "tally": {"template": "Tally", "marking": {}}}})
        assert [line.get("method") for line in lines[:-1]] == ["added", "fireable"]
        tally = (await tally_of(harness, "Spare"))["nested_id"]
        lines = await harness.send("step")
        assert [(nid, event["params"]["transitions"]) for nid, event in nested_events(lines, "fireable")] == [
            (tally, ["Up"]),
        ]

    async def test_a_net_a_firing_produced_is_known_from_its_landing(self):
        harness = Harness(crates)
        await harness.send("load", {"net": "crates", "params": {"labels": ["m"]}})
        lines = await harness.send("step")  # Make: a crate with a new tally lands in Spare
        assert result(lines)["transition"] == "Make" and nested_events(lines) == []
        tally = (await tally_of(harness, "Spare"))["nested_id"]
        lines = await harness.send("step")
        assert [(nid, event["params"]["transitions"]) for nid, event in nested_events(lines, "fireable")] == [
            (tally, ["Up"]),
        ]

    async def test_reset_retakes_every_baseline(self):
        harness = Harness(crates)
        await harness.send("load", {"net": "crates", "params": {"seeded": True}})
        await harness.send("step")
        await harness.send("reset")
        lines = await harness.send("step")
        assert [event["params"]["transitions"] for _nid, event in nested_events(lines, "fireable")] == [["Up"]]


class TestInnerInject:
    """A2 (§12.3, §12.5): ``inject`` into an inner net answers with its address."""

    async def test_the_result_names_the_inner_net_and_carries_its_version(self):
        harness = Harness(crates)
        await harness.send("load", {"net": "crates", "params": {"seeded": True}})
        tally = await tally_of(harness, "Spare")
        by_id = result(await harness.send("inject", {"place": "Low", "token": 1, "nested_id": tally["nested_id"]}))
        assert by_id == {"status": "injected", "id": by_id["id"], "marking_version": 1,
                         "path": tally["path"], "nested_id": tally["nested_id"]}
        by_path = result(await harness.send("inject", {"place": "Low", "token": 2, "path": tally["path"]}))
        assert (by_path["marking_version"], by_path["path"], by_path["nested_id"]) == (2, tally["path"], tally["nested_id"])
        outer = result(await harness.send("inject", {"place": "Labels", "token": "x"}))
        assert set(outer) == {"status", "id", "marking_version"}, "the outer net's result names no net"


class TestOuterOnly:
    """A3 (§12.3): ``step`` acts on the outer net only, as ``enable`` and the rest do."""

    async def test_step_with_an_address_is_invalid_params(self):
        harness = Harness(crates)
        await harness.send("load", {"net": "crates", "params": {"seeded": True}})
        tally = await tally_of(harness, "Spare")
        assert error(await harness.send("step", {"nested_id": tally["nested_id"]})) == "invalid_params"
        assert error(await harness.send("step", {"path": tally["path"]})) == "invalid_params"
        assert error(await harness.send("step", {"path": "crates"})) == "invalid_params", "even the outer net's own path"
        assert result(await harness.send("step", {"path": None}))["transition"] == "Fill", "a null path is no path (A5)"


class TestNetParameters:
    """A4 and A8 (§6.4): an undeclared name is ``invalid_params``; a value decodes by
    the declared type, the factory parameter's annotation, else the JSON type of
    the current value."""

    async def test_an_undeclared_name_is_invalid_params(self):
        harness = Harness(transit)
        await harness.send("load", {"net": "transit"})
        assert error(await harness.send("set_param", {"param": "speed", "value": 3})) == "invalid_params"
        assert error(await harness.send("set_param", {"param": "speed", "value": 3, "transition": None})) == "invalid_params"

    async def test_an_annotated_parameter_takes_its_type_in_either_order(self):
        harness = Harness(transit)
        await harness.send("load", {"net": "transit"})
        params = harness.owner._loaded.outer.graph.params
        lines = await harness.send("set_param", {"param": "destination", "value": "Airport"})
        assert [line.get("method") for line in lines] == ["param_changed", None] and params["destination"] == "Airport"
        lines = await harness.send("set_param", {"param": "destination", "value": None})
        assert lines[0]["method"] == "param_changed" and params["destination"] is None, "the override cleared"
        assert error(await harness.send("set_param", {"param": "destination", "value": 3})) == "invalid_params"
        assert params["destination"] is None, "refused while null, too"
        await harness.send("set_param", {"param": "destination", "value": "Harbour"})
        assert params["destination"] == "Harbour"
        assert error(await harness.send("set_param", {"param": "destination", "value": True})) == "invalid_params"

    async def test_the_annotation_types_the_load_parameter_too(self):
        harness = Harness(transit)
        await harness.send("load", {"net": "transit", "params": {"destination": "Central"}})
        assert harness.owner._loaded.outer.graph.params["destination"] == "Central"
        assert error(await harness.send("load", {"net": "transit", "params": {"destination": 3}})) == "invalid_params"
        described = harness.owner.describe()["nets"][0]["params"]
        assert described == [{"name": "destination", "type_name": "str | None", "required": False, "default": None}]

    async def test_an_unannotated_parameter_keeps_the_json_type_of_its_value(self):
        harness = Harness(wildlife)
        await harness.send("load", {"net": "wildlife"})
        assert result(await harness.send("set_param", {"param": "season", "value": "Autumn"}))
        assert error(await harness.send("set_param", {"param": "season", "value": 3})) == "invalid_params"
        assert result(await harness.send("set_param", {"param": "spread_threshold", "value": 5}))
        assert error(await harness.send("set_param", {"param": "spread_threshold", "value": "5"})) == "invalid_params"


class TestNullIsAbsent:
    """A5 (§2, §13.3): ``null`` for an optional request field counts as absent."""

    async def test_query_and_get_state(self):
        harness = Harness(wildlife)
        await harness.send("load", {"net": "wildlife"})
        await harness.send("set_filter", {"filter": {"clauses": [{"key": "class", "values": ["Bird"]}]}})
        assert result(await harness.send("query", {"filter": None})) == result(await harness.send("query"))
        assert result(await harness.send("query", {"filter": None}))["count"] == 6, "the connection's filter: the birds"
        assert result(await harness.send("get_state", {"token_limit": None})) == result(await harness.send("get_state"))
        assert result(await harness.send("get_place_tokens", {"place": "Forest", "offset": None, "limit": None})) == \
            result(await harness.send("get_place_tokens", {"place": "Forest"}))
        assert result(await harness.send("get_log", {"after": None, "limit": None, "path": None})) == {"entries": []}

    async def test_a_null_that_is_a_value_stays_one(self):
        harness = Harness(transit)
        await harness.send("load", {"net": "transit"})
        assert error(await harness.send("inject", {"place": "Arrived", "token": None})) == "invalid_token"
        assert error(await harness.send("inject", {"place": "Arrived"})) == "invalid_params"
        lines = await harness.send("set_param", {"param": "destination", "value": None, "transition": None})
        assert lines[0]["method"] == "param_changed", "a null transition is none: the net's own parameter"
        assert error(await harness.send("get_token", {"id": None})) == "invalid_params", "a required field stays required"


# --- a failing firing under a filter ------------------------------------------------------------


class Job(BaseModel):
    name: str
    kind: str


def explode(job: Job) -> Job:
    raise RuntimeError(f"{job.name} broke")


@petri_net(name="breakage", description="Every job breaks")
def breakage() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Red", Job, [Job(name="a", kind="red")], keys=["kind"]),
        ListPlaceNode("Blue", Job, [Job(name="b", kind="blue")], keys=["kind"]),
        ListPlaceNode("Done", Job, keys=["kind"]),
        FunctionTransitionNode("Break Red", explode),
        ArgumentEdgeToTransition("Red", "Break Red", "job"),
        ReturnedEdgeFromTransition("Break Red", "Done"),
        FunctionTransitionNode("Break Blue", explode),
        ArgumentEdgeToTransition("Blue", "Break Blue", "job"),
        ReturnedEdgeFromTransition("Break Blue", "Done"),
    ], restore_tokens_on_failure=True)


class TestFailedUnderAFilter:
    """A6 (§13.4): ``failed.consumed`` is projected like ``launched.consumed``."""

    async def test_failed_lists_only_what_launched_showed(self):
        harness = Harness(breakage)
        await harness.send("load", {"net": "breakage"})
        blue_id = result(await harness.send("get_state"))["marking"]["places"]["Blue"]["tokens"][0]["id"]
        await harness.send("set_filter", {"filter": {"clauses": [{"key": "kind", "values": ["blue"]}]}})
        lines = await harness.send("step")  # Break Red: its job is hidden
        assert [line.get("method") for line in lines[:2]] == ["launched", "failed"]
        assert lines[0]["params"]["consumed"] == [] and lines[1]["params"]["consumed"] == [], "sent, though empty"
        assert result(lines) == {"fired": True, "transition": "Break Red", "failed": True,
                                 "marking_version": 2, "running": False}
        await harness.send("disable", {"transition": "Break Red"})
        lines = await harness.send("step")  # Break Blue: its job is shown, and comes back
        assert lines[0]["params"]["consumed"] == [["Blue", blue_id]]
        failed = lines[1]["params"]
        assert (failed["consumed"], failed["restored"]) == ([["Blue", blue_id]], True)
        assert "added" not in [line.get("method") for line in lines], "a restored token is not announced again"
        page = result(await harness.send("get_place_tokens", {"place": "Blue"}))
        assert [token["id"] for token in page["tokens"]] == [blue_id]

    async def test_without_a_filter_failed_lists_everything(self):
        harness = Harness(breakage)
        await harness.send("load", {"net": "breakage"})
        lines = await harness.send("step")
        assert len(lines[1]["params"]["consumed"]) == 1


# --- the scheduling policies, as the runner honours them ----------------------------------------


def passing(x: int) -> int:
    return x


@petri_net(name="turns", description="Three transitions share one input")
def turns(tie_break: str = "round_robin") -> ExecutableGraph:
    nodes = [ListPlaceNode("In", int, list(range(10))), ListPlaceNode("Out", int)]
    for name in ("A", "B", "C"):
        nodes += [
            FunctionTransitionNode(name, passing),
            ArgumentEdgeToTransition("In", name, "x"),
            ReturnedEdgeFromTransition(name, "Out"),
        ]
    return ExecutableGraphOperations.construct_graph(nodes, tie_break=tie_break)


def keep_time(clock: int) -> int:
    return clock + 1


def seeded_tally(low) -> ExecutableGraph:
    tally = build_tally()
    NestedNets.put(tally, "Low", list(low))
    tally.drain_events()
    return tally


@petri_net(name="clockwork", description="A clock that always ticks, and crates of tallies")
def clockwork(deep_policy: str = "outer_first", clock: bool = True) -> ExecutableGraph:
    nodes = [ListPlaceNode("Shelf", Crate, [Crate(label="a", tally=seeded_tally([1, 2])),
                                            Crate(label="b", tally=seeded_tally([3, 4]))], nested=True)]
    if clock:
        nodes += [
            ListPlaceNode("Clock", int, [0]),
            ArgumentEdgeToTransition("Clock", "Tick", "clock"),
            FunctionTransitionNode("Tick", keep_time),
            ReturnedEdgeFromTransition("Tick", "Clock"),
        ]
    return ExecutableGraphOperations.construct_graph(nodes, deep_policy=deep_policy)


async def stepped(harness, count):
    """What ``count`` steps fired: the transition, under its crate's place and token for an inner one."""
    fired = []
    for _ in range(count):
        answer = result(await harness.send("step"))
        fired.append(answer["path"].split("/")[2] + " " + answer["transition"] if "path" in answer else answer["transition"])
    return fired


class TestPoliciesThroughTheRunner:

    async def test_round_robin_and_the_cursor_on_reset_and_load(self):
        harness = Harness(turns)
        await harness.send("load", {"net": "turns"})
        assert await stepped(harness, 4) == ["A", "B", "C", "A"]
        await harness.send("reset")
        assert await stepped(harness, 2) == ["A", "B"], "a reset net starts its round afresh"
        await harness.send("load", {"net": "turns"})
        assert await stepped(harness, 1) == ["A"]
        await harness.send("disable", {"transition": "B"})
        assert await stepped(harness, 3) == ["C", "A", "C"], "a disabled transition is never a candidate"
        await harness.send("load", {"net": "turns", "params": {"tie_break": "first_declared"}})
        assert await stepped(harness, 2) == ["A", "A"]

    async def test_outer_first_starves_the_crates_and_interleaved_does_not(self):
        harness = Harness(clockwork)
        await harness.send("load", {"net": "clockwork"})
        shelf = result(await harness.send("get_state"))["marking"]["places"]["Shelf"]["tokens"]
        a, b = (f"#{token['id']}" for token in shelf)
        assert await stepped(harness, 4) == ["Tick"] * 4
        await harness.send("load", {"net": "clockwork", "params": {"deep_policy": "interleaved"}})
        assert await stepped(harness, 6) == ["Tick", f"{a} Up", f"{b} Up", "Tick", f"{a} Up", f"{b} Up"]
        assert await stepped(harness, 2) == ["Tick", "Tick"], "both tallies done: the clock alone"

    async def test_a_policy_switched_mid_run_resumes_after_the_net_that_fired_last(self):
        harness = Harness(clockwork)
        await harness.send("load", {"net": "clockwork", "params": {"clock": False}})
        shelf = result(await harness.send("get_state"))["marking"]["places"]["Shelf"]["tokens"]
        a, b = (f"#{token['id']}" for token in shelf)
        assert await stepped(harness, 1) == [f"{a} Up"]
        harness.owner._loaded.outer.graph.deep_policy = "interleaved"
        assert await stepped(harness, 2) == [f"{b} Up", f"{a} Up"]

    async def test_a_running_net_alternates_its_inner_nets(self):
        harness = Harness(clockwork)
        await harness.send("load", {"net": "clockwork", "params": {"deep_policy": "interleaved", "clock": False}})
        lines = await harness.send("start")
        launched = [nid for nid, _event in nested_events(lines, "launched")]
        assert len(launched) == 4 and launched[0] != launched[1] and launched[::2] == [launched[0]] * 2
        assert lines[-1]["method"] == "execution_stopped"
        await harness.send("load", {"net": "clockwork", "params": {"clock": False}})
        lines = await harness.send("start")
        launched = [nid for nid, _event in nested_events(lines, "launched")]
        assert launched[0] == launched[1] and launched[2] == launched[3] != launched[0], "outer first: one, then the other"
