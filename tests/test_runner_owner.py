"""``petritype.runner.owner.Owner``: the protocol's state machine, driven directly.

Every test feeds requests with a fixed ``now`` and reads back the lines, awaiting
each ``Launch`` inline and handing its ``Landing`` to ``completed`` — exactly how
the transcript harness and the process layer drive it. ``duration_ms`` of a
``fired`` is the engine's own measurement of the body, so it is masked.
"""

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import runpy

import pytest
from pydantic import BaseModel

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
)
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 3, 9, 15, 2, 413000, tzinfo=timezone.utc)
ISO_NOW = "2026-10-03T09:15:02.413Z"
MS = object()  # matches any number


def masked(expected, actual) -> bool:
    if expected is MS:
        return isinstance(actual, (int, float))
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and expected.keys() == actual.keys()
            and all(masked(expected[k], actual[k]) for k in expected)
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(masked(e, a) for e, a in zip(expected, actual))
        )
    return expected == actual and type(expected) is type(actual)


def assert_lines(expected: list, actual: list) -> None:
    assert masked(expected, actual), f"\nexpected: {expected}\nactual:   {actual}"


class Harness:
    """Assigns ids "1", "2", ... and lands every launch inline unless told not to."""

    def __init__(self, *factories, now=NOW):
        self.owner = Owner(Registry.from_factories(factories))
        self.next_id = 0
        self.now = now
        self.pending = None  # a launch held back by ``land=False``
        self.exit = False

    async def send(self, method, params=None, *, land=True, raw=None):
        self.next_id += 1
        request = raw if raw is not None else {"id": str(self.next_id), "method": method}
        if raw is None and params is not None:
            request["params"] = params
        out = await self.owner.handle(request, self.now)
        return await self._follow(out, land)

    async def land(self, *, again=True):
        """Await the held launch and hand its landing back; keep landing what
        follows if ``again``."""
        launch, self.pending = self.pending, None
        assert launch is not None, "nothing airborne"
        landing = await launch.coroutine
        out = await self.owner.completed(landing, self.now)
        return await self._follow(out, again)

    async def tick(self, *, land=True):
        return await self._follow(await self.owner.tick(self.now), land)

    async def _follow(self, out, land):
        lines = list(out.lines)
        self.exit = self.exit or out.exit
        while out.launch is not None:
            if not land:
                self.pending = out.launch
                break
            landing = await out.launch.coroutine
            out = await self.owner.completed(landing, self.now)
            lines.extend(out.lines)
            self.exit = self.exit or out.exit
        return lines

    async def loaded(self, net, params=None):
        lines = await self.send("load", {"net": net, "params": params or {}})
        assert "result" in lines[-1], lines
        return lines


def methods(lines):
    return [line.get("method") or ("result" if "result" in line else "error") for line in lines]


def note(method, marking_version, running=False, **params):
    return {"method": method, "params": {**params, "marking_version": marking_version, "running": running}}


# --- nets ----------------------------------------------------------------------------------


def double(x: int) -> int:
    """Double a number."""
    return x * 2


@petri_net(name="chain", description="Doubles numbers")
def chain(n: int = 2, start: int = 1):
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Input", int, list(range(start, start + n))),
        ArgumentEdgeToTransition("Input", "Double", "x"),
        FunctionTransitionNode("Double", double),
        ReturnedEdgeFromTransition("Double", "Output"),
        ListPlaceNode("Output", int),
    ])


@petri_net(name="needs", mode="batch")
def needs(count: int, label: str = "x"):
    """Needs a count.

    More text that is not the description."""
    return chain(n=count)


@petri_net(name="broken")
def broken():
    raise RuntimeError("factory exploded")


@petri_net(name="not-a-graph")
def not_a_graph():
    return {"places": []}


def explode(x: int) -> int:
    raise ValueError(f"cannot take {x}")


@petri_net(name="fails")
def fails():
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, [1, 2]),
        ArgumentEdgeToTransition("In", "Explode", "x"),
        FunctionTransitionNode("Explode", explode),
        ReturnedEdgeFromTransition("Explode", "Out"),
        ListPlaceNode("Out", int),
    ])


@petri_net(name="watcher", mode="24/7")
def watcher():
    return chain(n=1)


class Parcel(BaseModel):
    id: int
    weight: int
    color: str | None = None


class HeavyParcel(Parcel):
    pass


def sort(parcel: Parcel) -> Parcel:
    return parcel


@petri_net(name="parcels")
def parcels():
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Receiving", Parcel, [
            Parcel(id=1, weight=5, color="red"),
            HeavyParcel(id=2, weight=50, color="#123456"),
            Parcel(id=3, weight=7, color="chartreuse"),
        ]),
        ArgumentEdgeToTransition("Receiving", "Sort", "parcel"),
        FunctionTransitionNode("Sort", sort),
        ReturnedEdgeFromTransition("Sort", "Sorted"),
        ListPlaceNode("Sorted", Parcel),
    ])


class Opaque:
    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return f"Opaque({self.text})"


def keep(item: Opaque) -> Opaque:
    return item


@petri_net(name="opaque")
def opaque(length: int = 10):
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Things", Opaque, [Opaque("a" * length)]),
        ArgumentEdgeToTransition("Things", "Keep", "item"),
        FunctionTransitionNode("Keep", keep),
        ReturnedEdgeFromTransition("Keep", "Kept"),
        ListPlaceNode("Kept", Opaque),
    ])


@petri_net(name="words")
def words(word: str = "w"):
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Words", str, [word]),
        ArgumentEdgeToTransition("Words", "Echo", "text"),
        FunctionTransitionNode("Echo", echo),
        ReturnedEdgeFromTransition("Echo", "Echoed"),
        ListPlaceNode("Echoed", str),
    ])


def echo(text: str) -> str:
    return text


# Shared by the blocking-body test: the body waits on ``RELEASE`` on its thread.
RELEASE = threading.Event()
ENTERED = threading.Event()


def slow_double(x: int) -> int:
    ENTERED.set()
    assert RELEASE.wait(5), "the test never released the body"
    return x * 2


@petri_net(name="slow")
def slow():
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Input", int, [1, 2]),
        ArgumentEdgeToTransition("Input", "Slow", "x"),
        FunctionTransitionNode("Slow", slow_double),
        ReturnedEdgeFromTransition("Slow", "Output"),
        ListPlaceNode("Output", int),
    ])


def scale(x: int, factor: int = 1) -> int:
    return x * factor


@petri_net(name="tunable")
def tunable():
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, [3]),
        ArgumentEdgeToTransition("In", "Scale", "x"),
        FunctionTransitionNode("Scale", scale),
        ReturnedEdgeFromTransition("Scale", "Out"),
        ListPlaceNode("Out", int),
    ])


def bump(counters: list[int], total: list[int]) -> tuple[int, int]:
    counters[0] += 100
    return sum(total), len(total)


@petri_net(name="shapes")
def shapes():
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Counters", int, [1]),
        ListPlaceNode("Totals", int, [2, 3]),
        ListPlaceNode("Sum", int),
        ListPlaceNode("Len", int),
        MutateEdge("Counters", "Bump", "counters"),
        ArgumentEdgeToTransition("Totals", "Bump", "total"),
        FunctionTransitionNode("Bump", bump),
        ReturnedEdgeFromTransition("Bump", "Sum", 0),
        ReturnedEdgeFromTransition("Bump", "Len", 1),
    ])


def peek(seen: list[int], x: int) -> int:
    return x + len(seen)


def high(graph) -> float:
    return 2.5


def boom(graph) -> float:
    raise ZeroDivisionError("no priority")


@petri_net(name="prioritised")
def prioritised():
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("A", int, [1]),
        ListPlaceNode("B", int, [1]),
        ListPlaceNode("Seen", int, [9]),
        ListPlaceNode("Out", int),
        SnapshotEdge("Seen", "Low", "seen"),
        ArgumentEdgeToTransition("A", "Low", "x"),
        FunctionTransitionNode("Low", peek, priority=boom),
        ReturnedEdgeFromTransition("Low", "Out"),
        ArgumentEdgeToTransition("B", "High", "x"),
        FunctionTransitionNode("High", double, priority=high),
        ReturnedEdgeFromTransition("High", "Out"),
    ])


# --- describe, load and the errors before a load -------------------------------------------


async def test_describe_lists_decorated_nets_with_their_params():
    h = Harness(chain, needs)
    [line] = await h.send("describe")
    result = line["result"]
    assert result["protocol_version"] == 1 and result["runtime"] == "python"
    assert isinstance(result["runtime_version"], str)
    assert result["nets"] == [
        {"name": "chain", "description": "Doubles numbers", "mode": "manual", "params": [
            {"name": "n", "type_name": "int", "required": False, "default": 2},
            {"name": "start", "type_name": "int", "required": False, "default": 1},
        ]},
        {"name": "needs", "description": "Needs a count.", "mode": "batch", "params": [
            {"name": "count", "type_name": "int", "required": True},
            {"name": "label", "type_name": "str", "required": False, "default": "x"},
        ]},
    ]
    assert h.owner.describe() == result


async def test_methods_need_a_load_except_describe_load_and_shutdown():
    h = Harness(chain)
    for method in ("get_state", "step", "start", "reset", "inject", "get_log", "set_param"):
        [line] = await h.send(method)
        assert line["error"]["code"] == "not_loaded", (method, line)
    [line] = await h.send("frobnicate")
    assert line == {"id": str(h.next_id), "error": {"code": "unknown_method", "message": "no such method: 'frobnicate'"}}
    [line] = await h.send("shutdown")
    assert line["result"] == {"status": "ok"} and h.exit


async def test_malformed_requests_and_params_that_are_not_objects():
    h = Harness(chain)
    [line] = await h.send(None, raw={"method": "describe"})
    assert line == {"id": "", "error": {"code": "invalid_params", "message": 'a request needs a string "id"'}}
    [line] = await h.send(None, raw={"id": "x"})
    assert line["id"] == "x" and line["error"]["code"] == "invalid_params"
    [line] = await h.send("describe", [1, 2])
    assert line["error"]["code"] == "invalid_params"
    # Missing params read as {}.
    [line] = await h.send(None, raw={"id": "y", "method": "describe"})
    assert "result" in line


async def test_load_writes_progress_then_reloaded_then_the_response():
    h = Harness(chain)
    lines = await h.send("load", {"net": "chain", "params": {"n": 3}})
    assert_lines([
        note("load_progress", 0, step="build", message="building chain"),
        note("reloaded", 0, generation=1, reason="load"),
        {"id": "1", "result": {"status": "loaded", "generation": 1, "marking_version": 0}},
    ], lines)
    assert h.owner.loaded and not h.owner.running and h.owner.marking_version == 0


async def test_load_errors():
    h = Harness(chain, needs, broken, not_a_graph)
    cases = [
        ({"net": "nope"}, "unknown_net"),
        ({}, "invalid_params"),
        ({"net": "chain", "params": [1]}, "invalid_params"),
        ({"net": "chain", "params": {"bogus": 1}}, "invalid_params"),
        ({"net": "chain", "params": {"n": "lots"}}, "invalid_params"),
        ({"net": "needs"}, "invalid_params"),
        ({"net": "broken"}, "load_failed"),
        ({"net": "not-a-graph"}, "load_failed"),
    ]
    for params, code in cases:
        lines = await h.send("load", params)
        assert lines[-1]["error"]["code"] == code, (params, lines)
    lines = await h.send("load", {"net": "broken"})
    assert lines[-1]["error"]["message"] == "RuntimeError: factory exploded"
    assert methods(lines) == ["load_progress", "error"]
    assert not h.owner.loaded


async def test_a_failed_load_keeps_the_previous_net():
    h = Harness(chain, broken)
    await h.loaded("chain")
    await h.send("load", {"net": "broken"})
    [line] = await h.send("get_state")
    assert line["result"]["net"] == "chain" and line["result"]["generation"] == 1


async def test_version_0_load_form_imports_the_module_and_finds_the_function():
    h = Harness()  # nothing registered: the v0 form needs no registry entry
    lines = await h.send("load", {"module": __name__, "function": "chain", "factory_params": {"n": 1}})
    assert lines[-1]["result"] == {"status": "loaded", "generation": 1, "marking_version": 0}
    [line] = await h.send("get_state")
    assert line["result"]["net"] == "chain"
    assert line["result"]["marking"]["places"]["Input"]["count"] == 1
    lines = await h.send("load", {"module": __name__, "function": "no_such_factory"})
    assert lines[-1]["error"]["code"] == "unknown_net"
    lines = await h.send("load", {"module": "no_such_module_anywhere", "function": "f"})
    assert lines[-1]["error"]["code"] == "unknown_net"
    lines = await h.send("load", {"module": __name__})
    assert lines[-1]["error"]["code"] == "invalid_params"


# --- state ---------------------------------------------------------------------------------


async def test_get_state_shape():
    h = Harness(chain)
    await h.loaded("chain")
    [line] = await h.send("get_state")
    assert line["result"] == {
        "protocol_version": 1,
        "runtime": "python",
        "net": "chain",
        "generation": 1,
        "marking_version": 0,
        "running": False,
        "topology": {
            "places": [
                {"name": "Input", "type_name": "int", "serializable": True},
                {"name": "Output", "type_name": "int", "serializable": True},
            ],
            "transitions": [{"name": "Double", "detail": {
                "function_name": "double", "signature": "(x: int) -> int", "docstring": "Double a number.",
            }}],
            "edges": [
                {"place": "Input", "transition": "Double", "direction": "in", "kind": "take"},
                {"place": "Output", "transition": "Double", "direction": "out"},
            ],
        },
        "transitions": {"Double": {"enabled": True, "fireable": True, "in_flight": False, "priority": 0.0}},
        "marking": {"places": {
            "Input": {"count": 2, "tokens": [{"id": "1", "label": "1"}, {"id": "2", "label": "2"}]},
            "Output": {"count": 0, "tokens": []},
        }},
        "progress": {"step_count": 0, "last_fired": None, "fired_counts": {}, "last_progress_at": None},
    }


async def test_edge_kinds_slots_and_priorities():
    h = Harness(shapes, prioritised)
    await h.loaded("shapes")
    [line] = await h.send("get_state")
    assert line["result"]["topology"]["edges"] == [
        {"place": "Totals", "transition": "Bump", "direction": "in", "kind": "take_all"},
        {"place": "Counters", "transition": "Bump", "direction": "in", "kind": "mutate"},
        {"place": "Sum", "transition": "Bump", "direction": "out", "slot": 0},
        {"place": "Len", "transition": "Bump", "direction": "out", "slot": 1},
    ]
    await h.loaded("prioritised")
    [line] = await h.send("get_state")
    state = line["result"]
    assert {"place": "Seen", "transition": "Low", "direction": "in", "kind": "snapshot"} in state["topology"]["edges"]
    assert state["transitions"]["High"]["priority"] == 2.5
    assert "priority" not in state["transitions"]["Low"]  # it raised


async def test_token_limit_caps_tokens_but_not_count():
    h = Harness(chain)
    await h.loaded("chain", {"n": 5})
    [line] = await h.send("get_state", {"token_limit": 2})
    marking = line["result"]["marking"]["places"]["Input"]
    assert marking["count"] == 5 and [t["id"] for t in marking["tokens"]] == ["1", "2"]
    [line] = await h.send("get_state", {"token_limit": 0})  # clamped to 1
    assert len(line["result"]["marking"]["places"]["Input"]["tokens"]) == 1
    [line] = await h.send("get_state", {"token_limit": -1})
    assert line["error"]["code"] == "invalid_params"


async def test_get_place_tokens_pages():
    h = Harness(chain)
    await h.loaded("chain", {"n": 5})
    [line] = await h.send("get_place_tokens", {"place": "Input", "offset": 1, "limit": 2})
    assert line["result"] == {
        "place": "Input", "marking_version": 0, "count": 5, "offset": 1, "limit": 2,
        "tokens": [{"id": "2", "label": "2"}, {"id": "3", "label": "3"}], "has_more": True,
    }
    [line] = await h.send("get_place_tokens", {"place": "Input", "offset": 3})
    assert line["result"]["limit"] == 200 and line["result"]["has_more"] is False
    assert [t["id"] for t in line["result"]["tokens"]] == ["4", "5"]
    [line] = await h.send("get_place_tokens", {"place": "Input", "limit": 5000})
    assert line["result"]["limit"] == 1000
    [line] = await h.send("get_place_tokens", {"place": "Nowhere"})
    assert line["error"]["code"] == "unknown_place"
    [line] = await h.send("get_place_tokens", {})
    assert line["error"]["code"] == "invalid_params"


async def test_labels_are_capped_at_1000_characters_including_the_marker():
    h = Harness(words)
    await h.loaded("words", {"word": "y" * 2000})
    [line] = await h.send("get_state")
    [token] = line["result"]["marking"]["places"]["Words"]["tokens"]
    assert len(token["label"]) == 1000
    assert token["label"] == ("'" + "y" * 996) + "..."
    # A repr of exactly 1000 characters is not cut.
    await h.loaded("words", {"word": "z" * 998})
    [line] = await h.send("get_state")
    [token] = line["result"]["marking"]["places"]["Words"]["tokens"]
    assert token["label"] == "'" + "z" * 998 + "'"
    # Counted in Unicode scalar values, not bytes.
    await h.loaded("words", {"word": "é" * 1500})
    [line] = await h.send("get_state")
    [token] = line["result"]["marking"]["places"]["Words"]["tokens"]
    assert len(token["label"]) == 1000 and token["label"].endswith("é...")


async def test_pydantic_labels_colors_and_per_token_type_names():
    h = Harness(parcels)
    await h.loaded("parcels")
    [line] = await h.send("get_state")
    state = line["result"]
    assert state["topology"]["places"][0] == {"name": "Receiving", "type_name": "Parcel", "serializable": True}
    assert state["marking"]["places"]["Receiving"]["tokens"] == [
        {"id": "1", "label": '{"id":1,"weight":5,"color":"red"}', "color": "#ef4444"},
        {"id": "2", "label": '{"id":2,"weight":50,"color":"#123456"}', "color": "#123456",
         "type_name": "HeavyParcel"},
        # An unknown colour name has no hex form, so no colour is sent.
        {"id": "3", "label": '{"id":3,"weight":7,"color":"chartreuse"}'},
    ]
    lines = await h.send("step")
    assert lines[0] == note(
        "fired", 1, transition="Sort", consumed=[["Receiving", "1"]],
        produced=[["Sorted", "4", '{"id":1,"weight":5,"color":"red"}', "#ef4444"]],
        duration_ms=lines[0]["params"]["duration_ms"],
    )


# --- get_token -----------------------------------------------------------------------------


async def test_get_token_is_json_for_a_serializable_place_and_text_otherwise():
    h = Harness(parcels, opaque)
    await h.loaded("parcels")
    [line] = await h.send("get_token", {"id": "2"})
    assert line["result"] == {
        "id": "2", "place": "Receiving", "type_name": "HeavyParcel", "kind": "json",
        "value": {"id": 2, "weight": 50, "color": "#123456"},
    }
    await h.loaded("opaque", {"length": 3000})
    [line] = await h.send("get_state")
    assert line["result"]["topology"]["places"][0]["serializable"] is False
    [line] = await h.send("get_token", {"id": "1"})
    result = line["result"]
    assert result["kind"] == "text" and "value" not in result
    assert result["text"] == "Opaque(" + "a" * 3000 + ")"  # never truncated


async def test_get_token_errors():
    h = Harness(chain)
    await h.loaded("chain")
    await h.send("step")  # consumes token 1
    for wanted in ("1", "99", "abc", "01", "-2"):
        [line] = await h.send("get_token", {"id": wanted})
        assert line["error"]["code"] == "unknown_token", wanted
    [line] = await h.send("get_token", {"id": 2})
    assert line["error"]["code"] == "invalid_params"


# --- step, notifications and the log --------------------------------------------------------


async def test_step_writes_fired_and_fireable_before_its_response():
    h = Harness(chain)
    await h.loaded("chain", {"n": 1})
    lines = await h.send("step")
    assert_lines([
        note("fired", 1, transition="Double", consumed=[["Input", "1"]],
             produced=[["Output", "2", "2"]], duration_ms=MS),
        note("fireable", 1, transitions=[]),
        {"id": "2", "result": {"fired": True, "transition": "Double", "marking_version": 1, "running": False}},
    ], lines)
    lines = await h.send("step")
    assert lines == [{"id": "3", "result": {"fired": False, "marking_version": 1, "running": False}}]
    [line] = await h.send("get_state")
    assert line["result"]["progress"] == {
        "step_count": 1, "last_fired": "Double", "fired_counts": {"Double": 1}, "last_progress_at": ISO_NOW,
    }


async def test_get_log_entries_and_paging():
    h = Harness(chain)
    await h.loaded("chain", {"n": 3})
    for _ in range(3):
        await h.send("step")
    [line] = await h.send("get_log")
    entries = line["result"]["entries"]
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert_lines([{
        "seq": 1, "transition": "Double", "started_at": ISO_NOW, "duration_ms": MS, "outcome": "fired",
        "consumed": [["Input", "1"]], "produced": [["Output", "4"]],
    }], entries[:1])
    [line] = await h.send("get_log", {"after": 1, "limit": 1})
    assert [e["seq"] for e in line["result"]["entries"]] == [2]
    [line] = await h.send("get_log", {"after": "x"})
    assert line["error"]["code"] == "invalid_params"


async def test_mutated_comes_before_fired():
    h = Harness(shapes)
    await h.loaded("shapes")
    lines = await h.send("step")
    assert_lines([
        note("mutated", 1, place="Counters", id="1", label="101"),
        note("fired", 2, transition="Bump", consumed=[["Totals", "2"], ["Totals", "3"]],
             produced=[["Sum", "4", "5"], ["Len", "5", "2"]], duration_ms=MS),
        note("fireable", 2, transitions=[]),
        {"id": "2", "result": {"fired": True, "transition": "Bump", "marking_version": 2, "running": False}},
    ], lines)


async def test_a_failing_body_answers_step_with_failed_true():
    h = Harness(fails)
    await h.loaded("fails")
    lines = await h.send("step")
    assert lines == [
        note("failed", 1, transition="Explode", error="ValueError: cannot take 1", restored=False,
             consumed=[["In", "1"]]),
        {"id": "2", "result": {"fired": True, "transition": "Explode", "failed": True,
                               "marking_version": 1, "running": False}},
    ]
    [line] = await h.send("get_log")
    assert line["result"]["entries"] == [{
        "seq": 1, "transition": "Explode", "started_at": ISO_NOW, "duration_ms": 0.0, "outcome": "failed",
        "consumed": [["In", "1"]], "produced": [], "error": "ValueError: cannot take 1", "restored": False,
    }]
    [line] = await h.send("get_state")
    assert line["result"]["progress"]["step_count"] == 0  # only successful firings count
    assert line["result"]["progress"]["last_fired"] is None


# --- the deferred step ---------------------------------------------------------------------


async def test_reads_are_answered_while_a_step_is_airborne_and_the_rest_waits():
    h = Harness(chain)
    await h.loaded("chain", {"n": 2})
    assert await h.send("step", land=False) == []  # id 2: airborne, no answer yet
    assert h.owner.status == ("Double", NOW)
    [line] = await h.send("get_state")  # id 3: at once, from the snapshot
    state = line["result"]
    assert state["marking_version"] == 0
    assert state["marking"]["places"]["Input"]["count"] == 2  # as of version 0
    assert state["transitions"]["Double"] == {"enabled": True, "fireable": False, "in_flight": True, "priority": 0.0}
    [line] = await h.send("get_place_tokens", {"place": "Input"})  # id 4
    assert line["result"]["count"] == 2
    [line] = await h.send("get_token", {"id": "2"})  # id 5: not handed to the body
    assert line["result"]["value"] == 2
    [line] = await h.send("get_log")  # id 6
    assert line["result"] == {"entries": []}
    [line] = await h.send("describe")  # id 7
    assert "result" in line
    assert await h.send("inject", {"place": "Input", "token": 10}) == []  # id 8: waits
    assert await h.send("step") == []  # id 9: a second step waits too
    assert await h.send("get_token", {"id": "1"}) == []  # id 10: the body holds token 1
    lines = await h.land(again=False)
    assert_lines([
        note("fired", 1, transition="Double", consumed=[["Input", "1"]], produced=[["Output", "3", "2"]],
             duration_ms=MS),
        {"id": "2", "result": {"fired": True, "transition": "Double", "marking_version": 1, "running": False}},
        note("added", 2, place="Input", id="4", label="10"),
        {"id": "8", "result": {"status": "injected", "id": "4", "marking_version": 2}},
    ], lines)
    assert h.pending is not None  # the deferred step launched; id 10 waits behind it
    lines = await h.land()
    assert_lines([
        note("fired", 3, transition="Double", consumed=[["Input", "2"]], produced=[["Output", "5", "4"]],
             duration_ms=MS),
        {"id": "9", "result": {"fired": True, "transition": "Double", "marking_version": 3, "running": False}},
        {"id": "10", "error": {"code": "unknown_token", "message": "no live token '1'"}},
    ], lines)


async def test_get_state_does_not_wait_for_a_body_running_on_its_thread():
    RELEASE.clear()
    ENTERED.clear()
    h = Harness(slow)
    await h.loaded("slow")
    await h.send("step", land=False)
    task = asyncio.ensure_future(h.pending.coroutine)
    h.pending = None
    while not ENTERED.is_set():  # the body is now blocked on its thread
        await asyncio.sleep(0.001)
    [line] = await h.send("get_state")
    assert line["result"]["marking"]["places"]["Input"]["count"] == 2
    assert line["result"]["transitions"]["Slow"]["in_flight"] is True
    [line] = await h.send("stop")
    assert line["result"] == {"status": "stopped", "marking_version": 0, "running": False}
    RELEASE.set()
    landing = await task
    out = await h.owner.completed(landing, NOW)
    assert methods(out.lines) == ["fired", "result"]
    assert out.lines[-1]["id"] == "2"


async def test_shutdown_is_answered_while_airborne():
    h = Harness(chain)
    await h.loaded("chain")
    await h.send("step", land=False)
    [line] = await h.send("shutdown")
    assert line["result"] == {"status": "ok"} and h.exit
    h.pending.coroutine.close()  # abandoned with the process


async def test_a_stale_landing_is_ignored():
    h = Harness(chain)
    await h.loaded("chain")
    await h.send("step", land=False)
    landing = await h.pending.coroutine
    first = await h.owner.completed(landing, NOW)
    assert methods(first.lines) == ["fired", "result"]
    again = await h.owner.completed(landing, NOW)
    assert again.lines == [] and again.launch is None


# --- start, quiescence, idle -----------------------------------------------------------------


async def test_start_runs_to_quiescence_and_stops():
    h = Harness(chain)
    await h.loaded("chain", {"n": 2})
    lines = await h.send("start")
    assert_lines([
        {"id": "2", "result": {"status": "started", "marking_version": 0, "running": True}},
        note("fired", 1, True, transition="Double", consumed=[["Input", "1"]], produced=[["Output", "3", "2"]],
             duration_ms=MS),
        note("fired", 2, True, transition="Double", consumed=[["Input", "2"]], produced=[["Output", "4", "4"]],
             duration_ms=MS),
        note("fireable", 2, True, transitions=[]),
        note("execution_stopped", 2, reason="no_fireable_transitions"),
    ], lines)
    assert not h.owner.running


async def test_stop_lets_the_airborne_body_land_and_ends_the_run():
    h = Harness(chain)
    await h.loaded("chain", {"n": 3})
    await h.send("start", land=False)
    [line] = await h.send("stop")
    assert line["result"]["running"] is False
    lines = await h.land()
    assert methods(lines) == ["fired"]  # landed and reported; nothing more launched
    assert h.pending is None and not h.owner.running


async def test_a_continuous_net_goes_idle_once_per_spell():
    later = NOW + timedelta(seconds=5)
    h = Harness(watcher)
    await h.loaded("watcher")
    lines = await h.send("start")
    assert_lines([
        {"id": "2", "result": {"status": "started", "marking_version": 0, "running": True}},
        note("fired", 1, True, transition="Double", consumed=[["Input", "1"]], produced=[["Output", "2", "2"]],
             duration_ms=MS),
        note("fireable", 1, True, transitions=[]),
        note("idle", 1, True, idle_since=ISO_NOW),
    ], lines)
    assert h.owner.running
    assert await h.tick() == []
    assert await h.tick() == []
    h.now = later
    lines = await h.send("inject", {"place": "Input", "token": 5})
    assert_lines([
        note("added", 2, True, place="Input", id="3", label="5"),
        note("fireable", 2, True, transitions=["Double"]),
        {"id": "3", "result": {"status": "injected", "id": "3", "marking_version": 2}},
        note("fired", 3, True, transition="Double", consumed=[["Input", "3"]], produced=[["Output", "4", "10"]],
             duration_ms=MS),
        note("fireable", 3, True, transitions=[]),
        note("idle", 3, True, idle_since="2026-10-03T09:15:07.413Z"),  # a new spell
    ], lines)


async def test_a_failure_while_running_stops_the_run():
    h = Harness(fails)
    await h.loaded("fails")
    lines = await h.send("start")
    assert lines == [
        {"id": "2", "result": {"status": "started", "marking_version": 0, "running": True}},
        note("failed", 1, True, transition="Explode", error="ValueError: cannot take 1", restored=False,
             consumed=[["In", "1"]]),
        note("execution_stopped", 1, reason="transition_failed", error_type="ValueError",
             message="cannot take 1"),
    ]
    assert not h.owner.running


# --- generations and versions --------------------------------------------------------------


async def test_generation_rises_on_load_and_reset_and_the_version_never_resets():
    h = Harness(chain)
    await h.loaded("chain", {"n": 1})
    await h.send("step")  # version 1
    lines = await h.send("reset")
    assert lines == [
        note("reloaded", 1, generation=2, reason="reset"),
        {"id": "3", "result": {"status": "reset", "generation": 2, "marking_version": 1}},
    ]
    [line] = await h.send("get_state")
    state = line["result"]
    assert state["generation"] == 2 and state["marking_version"] == 1
    assert state["marking"]["places"]["Input"]["tokens"] == [{"id": "1", "label": "1"}]  # same params, fresh ids
    assert state["progress"]["step_count"] == 0 and state["progress"]["last_fired"] is None
    lines = await h.send("load", {"net": "chain", "params": {"n": 2}})
    assert lines[-1]["result"] == {"status": "loaded", "generation": 3, "marking_version": 1}
    lines = await h.send("inject", {"place": "Input", "token": 4})
    assert lines[0]["params"]["marking_version"] == 2
    [line] = await h.send("get_log")
    assert line["result"]["entries"] == []  # the log belongs to a generation


async def test_load_while_running_replaces_the_run():
    h = Harness(watcher, chain)
    await h.loaded("watcher")
    await h.send("start")
    assert h.owner.running
    lines = await h.send("load", {"net": "chain"})
    assert lines[0]["params"]["running"] is True  # load_progress: the old net was still running
    assert lines[1] == note("reloaded", 1, generation=2, reason="load")
    assert not h.owner.running


# --- input -----------------------------------------------------------------------------------


async def test_inject_decodes_per_place_type_and_reports_added():
    h = Harness(parcels)
    await h.loaded("parcels")
    lines = await h.send("inject", {"place": "Sorted", "token": {"id": 9, "weight": 1, "color": "blue"}})
    assert lines == [
        note("added", 1, place="Sorted", id="4", label='{"id":9,"weight":1,"color":"blue"}', color="#3b82f6"),
        {"id": "2", "result": {"status": "injected", "id": "4", "marking_version": 1}},
    ]


async def test_inject_errors():
    h = Harness(parcels, opaque, chain)
    await h.loaded("parcels")
    for params, code in [
        ({"place": "Nowhere", "token": 1}, "unknown_place"),
        ({"place": "Sorted", "token": {"id": "nine"}}, "invalid_token"),
        ({"place": "Sorted"}, "invalid_params"),
        ({"token": 1}, "invalid_params"),
    ]:
        [line] = await h.send("inject", params)
        assert line["error"]["code"] == code, (params, line)
    await h.loaded("opaque")
    [line] = await h.send("inject", {"place": "Things", "token": "a"})
    assert line["error"]["code"] == "not_serializable"
    await h.loaded("chain")
    for token in ("seven", 1.5, True, None, [1]):
        [line] = await h.send("inject", {"place": "Input", "token": token})
        assert line["error"]["code"] == "invalid_token", token
    lines = await h.send("inject", {"place": "Input", "token": 2.0})
    assert lines[-1]["result"]["status"] == "injected"


async def test_fireable_is_sent_only_when_it_changes():
    h = Harness(chain)
    await h.loaded("chain", {"n": 1})
    lines = await h.send("disable", {"transition": "Double"})
    assert lines == [
        note("toggled", 1, transition="Double", enabled=False),
        note("fireable", 1, transitions=[]),
        {"id": "2", "result": {"marking_version": 1}},
    ]
    assert await h.send("disable", {"transition": "Double"}) == [{"id": "3", "result": {"marking_version": 1}}]
    [line] = await h.send("get_state")
    assert line["result"]["transitions"]["Double"]["enabled"] is False
    assert (await h.send("step"))[-1]["result"]["fired"] is False  # disabled is not selected
    lines = await h.send("enable", {"transition": "Double"})
    assert lines == [
        note("toggled", 2, transition="Double", enabled=True),
        note("fireable", 2, transitions=["Double"]),
        {"id": "6", "result": {"marking_version": 2}},
    ]
    lines = await h.send("inject", {"place": "Input", "token": 3})  # still fireable: no fireable line
    assert methods(lines) == ["added", "result"]
    [line] = await h.send("enable", {"transition": "Nope"})
    assert line["error"]["code"] == "unknown_transition"
    [line] = await h.send("enable", {})
    assert line["error"]["code"] == "invalid_params"


async def test_set_param_sets_one_keyword_of_one_transition():
    h = Harness(tunable)
    await h.loaded("tunable")
    lines = await h.send("set_param", {"param": "factor", "value": 4, "transition": "Scale"})
    assert lines == [
        note("param_changed", 1, param="factor", transition="Scale"),
        {"id": "2", "result": {"marking_version": 1}},
    ]
    lines = await h.send("step")
    assert lines[0]["params"]["produced"] == [["Out", "2", "12"]]
    for params, code in [
        ({"param": "factor", "value": 4}, "invalid_params"),  # transition is required in Python
        ({"param": "factor", "transition": "Scale"}, "invalid_params"),
        ({"param": "factor", "value": 4, "transition": "Nope"}, "unknown_transition"),
        ({"param": "speed", "value": 4, "transition": "Scale"}, "invalid_params"),
        ({"param": "x", "value": 4, "transition": "Scale"}, "invalid_params"),  # an edge supplies x
        ({"param": "factor", "value": "big", "transition": "Scale"}, "invalid_params"),
    ]:
        [line] = await h.send("set_param", params)
        assert line["error"]["code"] == code, (params, line)


# --- resync and async bodies ---------------------------------------------------------------


def grow(log: list[int], x: int) -> int:
    log.append(x)  # resizes the place through a whole-list mutate edge
    return x


@petri_net(name="grower")
def grower():
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Log", int, [0]),
        ListPlaceNode("In", int, [7]),
        ListPlaceNode("Out", int),
        MutateEdge("Log", "Grow", "log"),
        ArgumentEdgeToTransition("In", "Grow", "x"),
        FunctionTransitionNode("Grow", grow),
        ReturnedEdgeFromTransition("Grow", "Out"),
    ])


async def test_a_body_that_resizes_a_place_is_reported_as_a_resync():
    h = Harness(grower)
    await h.loaded("grower")
    lines = await h.send("step")
    assert methods(lines) == ["mutated", "fired", "reloaded", "fireable", "result"]
    assert lines[2] == note("reloaded", 2, generation=1, reason="resync")  # does not move the version
    assert lines[-1]["result"]["marking_version"] == 2
    [line] = await h.send("get_place_tokens", {"place": "Log"})
    assert [t["label"] for t in line["result"]["tokens"]] == ["0", "7"]
    assert len({t["id"] for t in line["result"]["tokens"]}) == 2


async def halve(x: int) -> int:
    await asyncio.sleep(0)
    return x // 2


@petri_net(name="async-body")
def async_body():
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, [8]),
        ArgumentEdgeToTransition("In", "Halve", "x"),
        FunctionTransitionNode("Halve", halve),
        ReturnedEdgeFromTransition("Halve", "Out"),
        ListPlaceNode("Out", int),
    ])


async def test_an_async_body_fires_on_the_loop():
    h = Harness(async_body)
    await h.loaded("async-body")
    lines = await h.send("step")
    assert lines[0]["params"]["produced"] == [["Out", "2", "4"]]


# --- an example from examples/ ---------------------------------------------------------------

# The README quickstart runs as a script (it fires its own graph at import); the
# runner hosts a factory that builds the same net from its definitions.
QUICKSTART = runpy.run_path(str(Path(__file__).resolve().parent.parent / "examples" / "quickstart.py"))


@petri_net(name="quickstart", description="The README quickstart")
def quickstart_net():
    return QUICKSTART["ExecutableGraphOperations"].construct_graph([
        QUICKSTART["ListPlaceNode"]("Input", int, [1, 2, 3]),
        QUICKSTART["ArgumentEdgeToTransition"]("Input", "Double", "x"),
        QUICKSTART["FunctionTransitionNode"]("Double", QUICKSTART["double"]),
        QUICKSTART["ReturnedEdgeFromTransition"]("Double", "Output"),
        QUICKSTART["ListPlaceNode"]("Output", int),
    ])


async def test_the_quickstart_net_runs_to_the_readme_marking():
    h = Harness(quickstart_net)
    await h.loaded("quickstart")
    lines = await h.send("start")
    assert methods(lines) == ["result", "fired", "fired", "fired", "fireable", "execution_stopped"]
    [line] = await h.send("get_place_tokens", {"place": "Output"})
    assert [t["label"] for t in line["result"]["tokens"]] == ["2", "4", "6"]
