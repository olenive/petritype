"""Targeted regression tests for runner and serialisation correctness fixes:

1. ``Owner._capture`` remapping ``view.eager`` indices across ``keyed_taken``.
2. ``Owner._transition_states`` rejecting non-finite priorities (NaN / Inf).
3. ``Owner._loaded.inner`` pruning destroyed inner nets without dropping in-flight ones.
4. ``Owner._held`` and eager capture protecting inner nets handed through
   ``ReadEdge`` / ``ReadWhereEdge`` / ``ReadAllEdge``.
5. ``Frames.encode`` rejecting NaN / Infinity with ``allow_nan=False``.
6. ``NetJson._apply_load`` deep-copying ``params`` to avoid input aliasing.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateWhereEdge,
    NestedNets,
    ReadAllEdge,
    ReadEdge,
    ReadWhereEdge,
    ReturnedEdgeFromTransition,
    SubnetPlaceNode,
    TakeWhereEdge,
)
from petritype.core.keys import Filter
from petritype.core.serialisation import NetJson, Templates
from petritype.runner.owner import Owner, _Handover
from petritype.runner.process import Frames
from petritype.runner.registry import Registry
from petritype.runtime import ControlSpec, SetTokens


NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)


class Harness:
    """Drives ``Owner`` directly; lands launches inline unless ``land=False``."""

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


def result(lines):
    assert lines and "result" in lines[-1], lines
    return lines[-1]["result"]


def step(place, token, net):
    return {"place": place, "token": token, "net": net}


# --- 1. Owner._capture remapping view.eager across keyed_taken ---------------------------------


class Item(BaseModel):
    name: str
    tag: str
    value: int = 0


class TestKeyedTakenEagerRemapping:

    @pytest.mark.asyncio
    async def test_keyed_take_remaps_surviving_mutated_eager_entries(self):
        """A transition with TakeWhereEdge and MutateWhereEdge on the same place
        removes an earlier index while mutating a later index; the surviving
        mutated token must keep its eagerly captured snapshot at the shifted
        index and block ``get_token`` while airborne."""
        gate = asyncio.Event()
        mutated_ready = asyncio.Event()

        async def process(taken: Item, target: Item) -> str:
            target.value = 999
            mutated_ready.set()
            await gate.wait()
            return taken.name

        @petri_net(name="keyed-remap")
        def keyed_remap() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode(
                    "Items",
                    Item,
                    [
                        Item(name="keep-before", tag="keep", value=1),
                        Item(name="drop-me", tag="take", value=2),
                        Item(name="mutate-me", tag="mut", value=3),
                        Item(name="keep-after", tag="keep", value=4),
                    ],
                    keys=("tag",),
                ),
                ListPlaceNode("Done", str),
                TakeWhereEdge("Items", "Work", "taken", where=Filter.where(tag="take")),
                MutateWhereEdge("Items", "Work", "target", where=Filter.where(tag="mut")),
                FunctionTransitionNode("Work", process),
                ReturnedEdgeFromTransition("Work", "Done"),
            ])

        harness = Harness(keyed_remap)
        await harness.send("load", {"net": "keyed-remap"})
        before = result(await harness.send("get_state"))["marking"]["places"]["Items"]["tokens"]
        before_by_label_name = {t["id"]: t for t in before}
        keep_before_id = before[0]["id"]
        mutated_id = before[2]["id"]
        keep_after_id = before[3]["id"]

        await harness.send("step", land=False)
        task = asyncio.create_task(harness.pending.coroutine)
        harness.pending = None
        await mutated_ready.wait()

        # The body has already set target.value = 999 in memory while airborne.
        # get_state must still return the eagerly captured pre-mutation label at the shifted index (1).
        mid_state = result(await harness.send("get_state"))
        mid_tokens = mid_state["marking"]["places"]["Items"]["tokens"]
        assert [t["id"] for t in mid_tokens] == [keep_before_id, mutated_id, keep_after_id]
        assert mid_tokens[1]["label"] == before_by_label_name[mutated_id]["label"]
        assert '"value":3' in mid_tokens[1]["label"]

        # get_token on the surviving mutated token must WAIT because its remapped index is in view.eager.
        assert await harness.send("get_token", {"id": mutated_id}) == []
        # Untouched surviving tokens answer immediately.
        assert result(await harness.send("get_token", {"id": keep_before_id}))["value"]["value"] == 1
        assert result(await harness.send("get_token", {"id": keep_after_id}))["value"]["value"] == 4

        gate.set()
        landing = await task
        lines = await harness._follow(await harness.owner.completed(landing, NOW), True)
        # The deferred get_token for mutated_id is answered after landing with value 999.
        token_answers = [line["result"] for line in lines if "result" in line and line["result"].get("id") == mutated_id]
        assert len(token_answers) == 1
        assert token_answers[0]["value"]["value"] == 999

    @pytest.mark.asyncio
    async def test_capture_remaps_multiple_keyed_taken_indices_accurately(self):
        """Directly verify index shifting when multiple non-contiguous indices are removed."""
        @petri_net(name="five-items")
        def five_items() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("P", int, [10, 20, 30, 40, 50]),
            ])

        harness = Harness(five_items)
        await harness.send("load", {"net": "five-items"})
        outer = harness.owner._loaded.outer
        handover = _Handover(
            consumed=[("P", 2), ("P", 4)],
            taken={},
            mutated={"P": {0, 1, 2, 3, 4}},
            keyed_taken={"P": [1, 3]},
        )
        snapshot = harness.owner._capture(outer, handover=handover)
        view = snapshot.places["P"]
        assert view.tokens == [10, 30, 50]
        assert view.ids == [1, 3, 5]
        assert sorted(view.eager) == [0, 1, 2]
        assert [view.eager[i].id for i in (0, 1, 2)] == ["1", "3", "5"]
        assert [view.eager[i].label for i in (0, 1, 2)] == ["10", "30", "50"]


# --- 2. Owner._transition_states non-finite priority guard -------------------------------------


class TestNonFinitePriorities:

    @pytest.mark.asyncio
    async def test_nan_and_infinity_priorities_are_omitted_and_produce_valid_json(self):
        def noop(x: int) -> int:
            return x

        @petri_net(name="wild-priorities")
        def wild_priorities() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("In", int, [1]),
                ListPlaceNode("Out", int),
                ArgumentEdgeToTransition("In", "T1", "x"),
                FunctionTransitionNode("T1", noop, priority=lambda _g: float("nan")),
                ReturnedEdgeFromTransition("T1", "Out"),
                ArgumentEdgeToTransition("In", "T2", "x"),
                FunctionTransitionNode("T2", noop, priority=lambda _g: float("inf")),
                ReturnedEdgeFromTransition("T2", "Out"),
                ArgumentEdgeToTransition("In", "T3", "x"),
                FunctionTransitionNode("T3", noop, priority=lambda _g: float("-inf")),
                ReturnedEdgeFromTransition("T3", "Out"),
                ArgumentEdgeToTransition("In", "T4", "x"),
                FunctionTransitionNode("T4", noop, priority=lambda _g: 3.5),
                ReturnedEdgeFromTransition("T4", "Out"),
            ])

        harness = Harness(wild_priorities)
        await harness.send("load", {"net": "wild-priorities"})
        [line] = await harness.send("get_state")
        transitions = line["result"]["transitions"]
        assert "priority" not in transitions["T1"]
        assert "priority" not in transitions["T2"]
        assert "priority" not in transitions["T3"]
        assert transitions["T4"]["priority"] == 3.5
        # Must encode cleanly as strict RFC 8259 JSON without raising.
        encoded = Frames.encode([line])
        assert b"NaN" not in encoded and b"Infinity" not in encoded


# --- 3. Owner._loaded.inner pruning ------------------------------------------------------------


def bump_one(x: int) -> int:
    return x + 1


@Templates.template("FixTestCounter")
def build_counter() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Low", int),
        ArgumentEdgeToTransition("Low", "Up", "x"),
        FunctionTransitionNode("Up", bump_one),
        ReturnedEdgeFromTransition("Up", "High"),
        ListPlaceNode("High", int),
    ])


class Box(BaseModel):
    label: str
    counter: ExecutableGraph

    def __petritype_nets__(self):
        return {"counter": self.counter}


def make_box(label: str, low: list[int] = ()) -> Box:
    counter = build_counter()
    if low:
        NestedNets.put(counter, "Low", list(low))
    return Box(label=label, counter=counter)


class TestInnerNetPruning:

    @pytest.mark.asyncio
    async def test_destroyed_inner_nets_are_pruned_while_live_ones_and_subnets_stay(self):
        def discard(box: Box) -> str:
            return box.label

        def move(box: Box) -> Box:
            return box

        @petri_net(name="prune-test")
        def prune_test() -> ExecutableGraph:
            subnet = ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Port", int, [1], port=True),
            ])
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode(
                    "Trash",
                    Box,
                    [make_box("a"), make_box("b"), make_box("c")],
                    nested=True,
                    stepping="outer_first",
                ),
                ListPlaceNode(
                    "Keep",
                    Box,
                    [make_box("keeper")],
                    nested=True,
                    stepping="outer_first",
                ),
                ListPlaceNode("Moved", Box, [], nested=True, stepping="outer_first"),
                ListPlaceNode("Log", str),
                SubnetPlaceNode("Sub", subnet),
                ArgumentEdgeToTransition("Trash", "Discard", "box"),
                FunctionTransitionNode("Discard", discard, priority=lambda _g: 10.0),
                ReturnedEdgeFromTransition("Discard", "Log"),
                ArgumentEdgeToTransition("Keep", "Move", "box"),
                FunctionTransitionNode("Move", move, priority=lambda _g: 1.0),
                ReturnedEdgeFromTransition("Move", "Moved"),
            ])

        harness = Harness(prune_test)
        await harness.send("load", {"net": "prune-test"})
        state = result(await harness.send("get_state"))
        trash_ids = [int(t["nets"][0]["nested_id"]) for t in state["marking"]["places"]["Trash"]["tokens"]]
        keeper_id = int(state["marking"]["places"]["Keep"]["tokens"][0]["nets"][0]["nested_id"])
        subnet_id = int(state["marking"]["places"]["Sub"]["subnet"]["nested_id"])

        assert set(harness.owner._loaded.inner) == set(trash_ids) | {keeper_id, subnet_id}

        # Step through all three discards; each destroyed inner net must be evicted after landing.
        for idx, destroyed_id in enumerate(trash_ids):
            await harness.send("step")
            remaining_trash = set(trash_ids[idx + 1:])
            assert destroyed_id not in harness.owner._loaded.inner
            assert set(harness.owner._loaded.inner) == remaining_trash | {keeper_id, subnet_id}

        # Moving a token preserves its inner net record and marking_version.
        await harness.send("inject", {"place": "Low", "nested_id": str(keeper_id), "token": 5})
        version_before = harness.owner._loaded.inner[keeper_id].marking_version
        assert version_before == 1
        await harness.send("step")  # Move fires (outer_first)
        assert keeper_id in harness.owner._loaded.inner
        assert harness.owner._loaded.inner[keeper_id].marking_version == version_before

    @pytest.mark.asyncio
    async def test_in_flight_inner_net_is_not_pruned_while_airborne_and_survives_restore(self):
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def fail_slowly(box: Box) -> str:
            entered.set()
            await gate.wait()
            raise RuntimeError("boom")

        @petri_net(name="inflight-prune")
        def inflight_prune(restore: bool = True) -> ExecutableGraph:
            graph = ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Inbox", Box, [make_box("a")], nested=True, stepping="outer_first"),
                ListPlaceNode("Other", Box, [make_box("b")], nested=True, stepping="outer_first"),
                ListPlaceNode("Out", str),
                ArgumentEdgeToTransition("Inbox", "Fail", "box"),
                FunctionTransitionNode("Fail", fail_slowly),
                ReturnedEdgeFromTransition("Fail", "Out"),
            ])
            graph.restore_tokens_on_failure = restore
            return graph

        harness = Harness(inflight_prune)
        await harness.send("load", {"net": "inflight-prune", "params": {"restore": True}})
        state = result(await harness.send("get_state"))
        box_a_id = int(state["marking"]["places"]["Inbox"]["tokens"][0]["nets"][0]["nested_id"])
        box_b_id = int(state["marking"]["places"]["Other"]["tokens"][0]["nets"][0]["nested_id"])

        await harness.send("step", land=False)
        task = asyncio.create_task(harness.pending.coroutine)
        harness.pending = None
        await entered.wait()

        # Token a has been popped from Inbox by stage 1, so it is absent from outer.graph,
        # yet pruning while airborne must keep it in loaded.inner because it is held in flight.
        harness.owner._prune_inner()
        assert box_a_id in harness.owner._loaded.inner
        assert box_b_id in harness.owner._loaded.inner

        # Release and fail with restore=True: token a is restored, so box_a_id must still be kept.
        gate.set()
        landing = await task
        await harness._follow(await harness.owner.completed(landing, NOW), True)
        assert box_a_id in harness.owner._loaded.inner
        assert box_b_id in harness.owner._loaded.inner

        # Now reload with restore=False: after failure, token a is gone and box_a_id must be evicted.
        gate.clear()
        entered.clear()
        await harness.send("load", {"net": "inflight-prune", "params": {"restore": False}})
        state = result(await harness.send("get_state"))
        box_a_id = int(state["marking"]["places"]["Inbox"]["tokens"][0]["nets"][0]["nested_id"])
        box_b_id = int(state["marking"]["places"]["Other"]["tokens"][0]["nets"][0]["nested_id"])

        await harness.send("step", land=False)
        task = asyncio.create_task(harness.pending.coroutine)
        harness.pending = None
        await entered.wait()
        harness.owner._prune_inner()
        assert box_a_id in harness.owner._loaded.inner

        gate.set()
        landing = await task
        await harness._follow(await harness.owner.completed(landing, NOW), True)
        assert box_a_id not in harness.owner._loaded.inner
        assert box_b_id in harness.owner._loaded.inner

    @pytest.mark.asyncio
    async def test_control_removing_nested_token_prunes_inner_net(self):
        @petri_net(
            name="control-prune",
            controls={
                "drop_first": ControlSpec(
                    node="Shelf",
                    kind="button",
                    label="Drop first",
                    to_command=lambda _v, g: (
                        SetTokens(place="Shelf", tokens=list(g.place_named("Shelf").tokens[1:]))
                        if g.place_named("Shelf").tokens
                        else None
                    ),
                )
            },
        )
        def control_prune() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Shelf", Box, [make_box("a"), make_box("b")], nested=True),
            ])

        harness = Harness(control_prune)
        await harness.send("load", {"net": "control-prune"})
        shelf = result(await harness.send("get_state"))["marking"]["places"]["Shelf"]["tokens"]
        first_net_id = int(shelf[0]["nets"][0]["nested_id"])
        second_net_id = int(shelf[1]["nets"][0]["nested_id"])
        assert {first_net_id, second_net_id} == set(harness.owner._loaded.inner)

        await harness.send("press", {"control": "drop_first"})
        assert first_net_id not in harness.owner._loaded.inner
        assert second_net_id in harness.owner._loaded.inner


# --- 4. Owner._held and eager capture for ReadEdge / ReadWhereEdge / ReadAllEdge ---------------


class TestReadEdgeInnerNetProtection:

    @pytest.mark.asyncio
    async def test_read_edge_holds_inner_net_and_renders_holding_token_eagerly(self):
        gate = asyncio.Event()
        modified = asyncio.Event()

        async def inspect_and_push(trigger: int, box: Box) -> int:
            NestedNets.put(box.counter, "Low", [10, 20])
            modified.set()
            await gate.wait()
            return trigger

        @petri_net(name="read-nested")
        def read_nested() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Triggers", int, [1]),
                ListPlaceNode(
                    "Shelf",
                    Box,
                    [make_box("first", [1]), make_box("second", [2])],
                    nested=True,
                    stepping="outer_first",
                ),
                ListPlaceNode("Done", int),
                ArgumentEdgeToTransition("Triggers", "Inspect", "trigger"),
                ReadEdge("Shelf", "Inspect", "box"),
                FunctionTransitionNode("Inspect", inspect_and_push),
                ReturnedEdgeFromTransition("Inspect", "Done"),
            ])

        harness = Harness(read_nested)
        await harness.send("load", {"net": "read-nested"})
        shelf = result(await harness.send("get_state"))["marking"]["places"]["Shelf"]["tokens"]
        held_box_id = shelf[0]["id"]
        held_net_id = shelf[0]["nets"][0]["nested_id"]
        free_box_id = shelf[1]["id"]
        free_net_id = shelf[1]["nets"][0]["nested_id"]

        await harness.send("step", land=False)
        task = asyncio.create_task(harness.pending.coroutine)
        harness.pending = None
        await modified.wait()

        # Inner net of the ReadEdge token is held: reads by nested_id and by path both wait.
        assert await harness.send("get_state", {"nested_id": held_net_id}) == []
        assert await harness.send("get_state", {"path": [step("Shelf", held_box_id, "counter")]}) == []
        # get_token on the holding token waits because it serializes the inner net live.
        assert await harness.send("get_token", {"id": held_box_id}) == []

        # Outer get_state answers immediately using the eagerly captured pre-body snapshot (count == 1).
        mid_outer = result(await harness.send("get_state"))
        assert mid_outer["marking"]["places"]["Shelf"]["tokens"][0]["nets"][0]["count"] == 1

        # Unread sibling token and its inner net answer immediately.
        assert result(await harness.send("get_state", {"nested_id": free_net_id}))["marking"]["places"]["Low"]["count"] == 1
        assert result(await harness.send("get_token", {"id": free_box_id}))["nets"][0]["count"] == 1

        gate.set()
        landing = await task
        lines = await harness._follow(await harness.owner.completed(landing, NOW), True)
        results = [line["result"] for line in lines if "result" in line]
        # Deferred reads see the post-body state (1 + 2 = 3 tokens in Low).
        inner_states = [r for r in results if r.get("nested_id") == held_net_id]
        assert len(inner_states) == 2
        assert all(s["marking"]["places"]["Low"]["count"] == 3 for s in inner_states)
        token_result = next(r for r in results if r.get("id") == held_box_id)
        assert token_result["nets"][0]["count"] == 3

    @pytest.mark.asyncio
    async def test_read_where_edge_holds_only_the_matched_inner_net(self):
        gate = asyncio.Event()
        modified = asyncio.Event()

        async def inspect_match(trigger: int, box: Box) -> int:
            NestedNets.put(box.counter, "Low", [99])
            modified.set()
            await gate.wait()
            return trigger

        @petri_net(name="read-where-nested")
        def read_where_nested() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Triggers", int, [1]),
                ListPlaceNode(
                    "Shelf",
                    Box,
                    [make_box("alpha", [1]), make_box("beta", [2])],
                    keys=("label",),
                    nested=True,
                    stepping="outer_first",
                ),
                ListPlaceNode("Done", int),
                ArgumentEdgeToTransition("Triggers", "Inspect", "trigger"),
                ReadWhereEdge("Shelf", "Inspect", "box", where=Filter.where(label="beta")),
                FunctionTransitionNode("Inspect", inspect_match),
                ReturnedEdgeFromTransition("Inspect", "Done"),
            ])

        harness = Harness(read_where_nested)
        await harness.send("load", {"net": "read-where-nested"})
        shelf = result(await harness.send("get_state"))["marking"]["places"]["Shelf"]["tokens"]
        alpha_net_id = shelf[0]["nets"][0]["nested_id"]
        beta_box_id = shelf[1]["id"]
        beta_net_id = shelf[1]["nets"][0]["nested_id"]

        await harness.send("step", land=False)
        task = asyncio.create_task(harness.pending.coroutine)
        harness.pending = None
        await modified.wait()

        # Matched token ("beta") is held; non-matching front token ("alpha") is free.
        assert await harness.send("get_state", {"nested_id": beta_net_id}) == []
        assert await harness.send("get_token", {"id": beta_box_id}) == []
        assert result(await harness.send("get_state", {"nested_id": alpha_net_id}))["marking"]["places"]["Low"]["count"] == 1

        gate.set()
        landing = await task
        await harness._follow(await harness.owner.completed(landing, NOW), True)
        assert result(await harness.send("get_state", {"nested_id": beta_net_id}))["marking"]["places"]["Low"]["count"] == 2

    @pytest.mark.asyncio
    async def test_read_all_edge_holds_every_inner_net_in_the_place(self):
        gate = asyncio.Event()
        modified = asyncio.Event()

        async def inspect_all(trigger: int, boxes: list[Box]) -> int:
            for box in boxes:
                NestedNets.put(box.counter, "Low", [7])
            modified.set()
            await gate.wait()
            return trigger

        @petri_net(name="read-all-nested")
        def read_all_nested() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Triggers", int, [1]),
                ListPlaceNode(
                    "Shelf",
                    Box,
                    [make_box("a", [1]), make_box("b", [2])],
                    nested=True,
                    stepping="outer_first",
                ),
                ListPlaceNode(
                    "Spare",
                    Box,
                    [make_box("c", [3])],
                    nested=True,
                    stepping="outer_first",
                ),
                ListPlaceNode("Done", int),
                ArgumentEdgeToTransition("Triggers", "InspectAll", "trigger"),
                ReadAllEdge("Shelf", "InspectAll", "boxes"),
                FunctionTransitionNode("InspectAll", inspect_all),
                ReturnedEdgeFromTransition("InspectAll", "Done"),
            ])

        harness = Harness(read_all_nested)
        await harness.send("load", {"net": "read-all-nested"})
        places = result(await harness.send("get_state"))["marking"]["places"]
        shelf_nets = [t["nets"][0]["nested_id"] for t in places["Shelf"]["tokens"]]
        spare_net = places["Spare"]["tokens"][0]["nets"][0]["nested_id"]

        await harness.send("step", land=False)
        task = asyncio.create_task(harness.pending.coroutine)
        harness.pending = None
        await modified.wait()

        # Both inner nets in Shelf are held; Spare is untouched and answers immediately.
        for nid in shelf_nets:
            assert await harness.send("get_state", {"nested_id": nid}) == []
        assert result(await harness.send("get_state", {"nested_id": spare_net}))["marking"]["places"]["Low"]["count"] == 1

        gate.set()
        landing = await task
        await harness._follow(await harness.owner.completed(landing, NOW), True)
        for nid in shelf_nets:
            assert result(await harness.send("get_state", {"nested_id": nid}))["marking"]["places"]["Low"]["count"] == 2

    @pytest.mark.asyncio
    async def test_plain_non_nested_read_edge_token_still_answers_get_token_immediately(self):
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def look(trigger: int, item: Item) -> str:
            entered.set()
            await gate.wait()
            return item.name

        @petri_net(name="read-plain")
        def read_plain() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Triggers", int, [1]),
                ListPlaceNode("Items", Item, [Item(name="widget", tag="x", value=5)]),
                ListPlaceNode("Done", str),
                ArgumentEdgeToTransition("Triggers", "Look", "trigger"),
                ReadEdge("Items", "Look", "item"),
                FunctionTransitionNode("Look", look),
                ReturnedEdgeFromTransition("Look", "Done"),
            ])

        harness = Harness(read_plain)
        await harness.send("load", {"net": "read-plain"})
        item_id = result(await harness.send("get_state"))["marking"]["places"]["Items"]["tokens"][0]["id"]

        await harness.send("step", land=False)
        task = asyncio.create_task(harness.pending.coroutine)
        harness.pending = None
        await entered.wait()

        # Non-nested read token does not block get_token.
        token = result(await harness.send("get_token", {"id": item_id}))
        assert token["value"]["name"] == "widget"

        gate.set()
        landing = await task
        await harness._follow(await harness.owner.completed(landing, NOW), True)


# --- 5. Frames.encode rejects NaN / Infinity ---------------------------------------------------


class TestFramesEncodeStrictJson:

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_frames_encode_rejects_non_finite_floats(self, bad):
        with pytest.raises(ValueError):
            Frames.encode([{"id": "1", "result": {"priority": bad}}])

    def test_frames_encode_accepts_finite_floats(self):
        encoded = Frames.encode([{"id": "1", "result": {"priority": 2.5}}])
        assert encoded == b'{"id": "1", "result": {"priority": 2.5}}\n'


# --- 6. NetJson._apply_load deep-copies params -------------------------------------------------


class TestNetJsonParamsCopy:

    def test_load_into_deep_copies_mutable_params(self):
        graph = ExecutableGraphOperations.construct_graph(
            [ListPlaceNode("In", int, [1])],
            params={"tags": ["initial"], "config": {"nested": [1, 2]}},
        )
        saved_params = {"tags": ["loaded"], "config": {"nested": [3, 4]}}
        saved = {"marking": {"In": [1]}, "params": saved_params, "enabled": {}}
        NetJson.load_into(graph, saved)

        # Mutate the input dictionary and its nested containers after load_into.
        saved_params["tags"].append("mutated")
        saved_params["config"]["nested"].append(99)
        saved_params["config"]["extra"] = True

        assert graph.params == {"tags": ["loaded"], "config": {"nested": [3, 4]}}

    def test_restore_deep_copies_mutable_params(self):
        @Templates.template("FixTestMutableParams")
        def make_graph() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph(
                [ListPlaceNode("In", int)],
                params={"items": [], "meta": {"k": "v"}},
            )

        try:
            saved = {
                "template": "FixTestMutableParams",
                "marking": {"In": [7]},
                "params": {"items": ["a"], "meta": {"k": "loaded"}},
                "enabled": {},
            }
            restored = NetJson.restore(saved)
            saved["params"]["items"].append("b")
            saved["params"]["meta"]["k"] = "mutated"
            assert restored.params == {"items": ["a"], "meta": {"k": "loaded"}}
        finally:
            Templates.forget("FixTestMutableParams")
