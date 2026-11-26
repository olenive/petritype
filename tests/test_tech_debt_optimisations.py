"""Tests for technical-debt deduplication and algorithmic optimisations across
keys, serialisation, registry, and runner owner."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Annotated

import pytest
from pydantic import BaseModel

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    MutateWhereEdge,
    ReadAllEdge,
    ReadEdge,
    ReadWhereEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    TakeWhereEdge,
)
from petritype.core.keys import Clause, Filter, Keys, Query
from petritype.core.serialisation import JsonForm
from petritype.runner.owner import Owner, _Handover
from petritype.runner.registry import NetEntries, ParamSpec, Registry

NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)


# --- 1. Keys.one, Query.distinct, Clause.accepts -----------------------------------------------


class TestKeysAndQueries:

    def test_keys_one_extracts_only_requested_key(self):
        calls = {"a": 0, "b": 0}

        def extract_a(tok):
            calls["a"] += 1
            return tok["a"]

        def extract_b(tok):
            calls["b"] += 1
            return tok["b"]

        place = ListPlaceNode("P", dict, [{"a": "hello", "b": 42}], keys={"a": extract_a, "b": extract_b})
        assert Keys.one(place, place.tokens[0], "a") == "hello"
        assert calls == {"a": 1, "b": 0}

    def test_keys_one_validates_type_and_missing_key(self):
        place = ListPlaceNode("P", object, [1.5], keys={"bad": lambda x: x})
        with pytest.raises(TypeError, match="key 'bad' of a token in 'P' is float"):
            Keys.one(place, 1.5, "bad")
        with pytest.raises(KeyError):
            Keys.one(place, 1.5, "missing")
        plain = ListPlaceNode("Q", int, [1])
        with pytest.raises(KeyError):
            Keys.one(plain, 1, "any")

    def test_query_distinct_strict_types_order_and_single_extractor(self):
        other_calls = 0

        def unused_extractor(_tok):
            nonlocal other_calls
            other_calls += 1
            return "never"

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode(
                "P",
                object,
                [1, True, 0, False, "1", 1, True, 2, False],
                keys={"v": lambda x: x, "other": unused_extractor},
            ),
        ])
        distinct = Query.distinct(graph, "v")
        assert [(type(v), v) for v in distinct] == [
            (int, 1),
            (bool, True),
            (int, 0),
            (bool, False),
            (str, "1"),
            (int, 2),
        ]
        assert other_calls == 0

    def test_clause_accepts_strict_types_and_unhashable(self):
        clause_int = Clause("k", (1, 0, "1"))
        assert clause_int.accepts({"k": 1}, {})
        assert clause_int.accepts({"k": 0}, {})
        assert clause_int.accepts({"k": "1"}, {})
        assert not clause_int.accepts({"k": True}, {})
        assert not clause_int.accepts({"k": False}, {})
        assert not clause_int.accepts({"k": [1]}, {})

        clause_bool = Clause("k", (True,))
        assert clause_bool.accepts({"k": True}, {})
        assert not clause_bool.accepts({"k": 1}, {})


# --- 2. JsonForm.encode fast path & JsonForm.place_type memoization ----------------------------


class Colour(str):
    pass


class Score(int):
    pass


class TestJsonForm:

    def test_encode_scalar_fast_path_and_subclasses(self):
        assert JsonForm.encode(True) is True
        assert JsonForm.encode(False) is False
        assert type(JsonForm.encode(42)) is int and JsonForm.encode(42) == 42
        assert type(JsonForm.encode("abc")) is str and JsonForm.encode("abc") == "abc"
        assert type(JsonForm.encode(3.25)) is float and JsonForm.encode(3.25) == 3.25

        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                JsonForm.encode(bad)

        encoded_colour = JsonForm.encode(Colour("red"))
        assert type(encoded_colour) is str and encoded_colour == "red"

        encoded_score = JsonForm.encode(Score(7))
        assert type(encoded_score) is int and encoded_score == 7

    def test_place_type_memoizes_model_json_schema(self):
        schema_calls = 0

        class CountedModel(BaseModel):
            x: int

            @classmethod
            def model_json_schema(cls, *args, **kwargs):
                nonlocal schema_calls
                schema_calls += 1
                return super().model_json_schema(*args, **kwargs)

        assert JsonForm.place_type(CountedModel) is True
        assert JsonForm.place_type(CountedModel) is True
        assert JsonForm.place_type(CountedModel) is True
        assert schema_calls == 1


# --- 3. NetEntries.adapter caching -------------------------------------------------------------


class TestNetEntriesAdapterCache:

    def test_adapter_returns_cached_instance(self):
        spec_a = ParamSpec(name="n", type_name="int", required=True, annotation=list[int])
        spec_b = ParamSpec(name="m", type_name="int", required=True, annotation=list[int])
        first = NetEntries.adapter(spec_a)
        second = NetEntries.adapter(spec_b)
        assert first is not None
        assert first is second

    def test_adapter_handles_unhashable_annotation(self):
        unhashable = Annotated[int, {"meta": "unhashable"}]
        spec = ParamSpec(name="x", type_name="int", required=True, annotation=unhashable)
        adapter = NetEntries.adapter(spec)
        assert adapter is not None
        assert adapter.validate_python(5) == 5


# --- 4. Owner._handed_over via plan_stage_1 & Owner._distinct ----------------------------------


class Order(BaseModel):
    oid: str
    courier: str


class Courier(BaseModel):
    name: str
    load: int = 0


async def _load_owner(factory) -> tuple[Owner, object]:
    owner = Owner(Registry.from_factories([factory]))
    name = NetEntries.from_factory(factory).name
    await owner.handle({"id": "1", "method": "load", "params": {"net": name}}, NOW)
    return owner, owner._loaded.outer


class TestOwnerHandedOver:

    @pytest.mark.asyncio
    async def test_plain_drain_mutate_read_and_snapshot_edges(self):
        def work(a: int, drained: list[int], mut: list[int], r_one: str, r_all: list[str], snap: int) -> int:
            return a

        @petri_net(name="mixed-edges")
        def mixed_edges() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Plain", int, [10, 20]),
                ListPlaceNode("Drained", int, [30, 40]),
                ListPlaceNode("EmptyDrain", int, []),
                ListPlaceNode("Mut", int, [50, 60]),
                ListPlaceNode("ROne", str, ["r0", "r1"]),
                ListPlaceNode("RAll", str, ["a0", "a1"]),
                ListPlaceNode("Snap", int, [99]),
                ListPlaceNode("Out", int),
                ArgumentEdgeToTransition("Plain", "Work", "a"),
                DrainEdge("Drained", "Work", "drained"),
                MutateEdge("Mut", "Work", "mut"),
                ReadEdge("ROne", "Work", "r_one"),
                ReadAllEdge("RAll", "Work", "r_all"),
                SnapshotEdge("Snap", "Work", "snap"),
                FunctionTransitionNode("Work", work),
                ReturnedEdgeFromTransition("Work", "Out"),
            ])

        owner, outer = await _load_owner(mixed_edges)
        t = outer.graph.transitions[0]
        handover = owner._handed_over(outer, t)
        plain_ids = outer.place_nodes["Plain"].token_ids
        drained_ids = outer.place_nodes["Drained"].token_ids

        assert handover.consumed == [
            ("Plain", plain_ids[0]),
            ("Drained", drained_ids[0]),
            ("Drained", drained_ids[1]),
        ]
        assert handover.taken == {"Plain": 1, "Drained": 2}
        assert handover.keyed_taken == {}
        assert handover.mutated == {"Mut": {0, 1}}
        assert handover.read == {"ROne": {0}, "RAll": {0, 1}}
        assert "Snap" not in handover.mutated and "Snap" not in handover.read

    @pytest.mark.asyncio
    async def test_empty_drain_and_unfireable_transition(self):
        def drain_only(items: list[int]) -> int:
            return len(items)

        def needs_token(x: int) -> int:
            return x

        @petri_net(name="drain-and-empty")
        def drain_and_empty() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Empty", int, []),
                ListPlaceNode("Out", int),
                DrainEdge("Empty", "Drain", "items"),
                FunctionTransitionNode("Drain", drain_only),
                ReturnedEdgeFromTransition("Drain", "Out"),
                ArgumentEdgeToTransition("Empty", "Blocked", "x"),
                FunctionTransitionNode("Blocked", needs_token),
                ReturnedEdgeFromTransition("Blocked", "Out"),
            ])

        owner, outer = await _load_owner(drain_and_empty)
        drain_t = next(t for t in outer.graph.transitions if t.name == "Drain")
        blocked_t = next(t for t in outer.graph.transitions if t.name == "Blocked")

        assert owner._handed_over(outer, drain_t) == _Handover()
        assert owner._handed_over(outer, blocked_t) == _Handover()

    @pytest.mark.asyncio
    async def test_keyed_take_and_multiple_consuming_edges_same_place(self):
        def pair(first: int, second: int) -> int:
            return first + second

        @petri_net(name="multi-same-place")
        def multi_same_place() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Nums", int, [10, 20, 30, 40], keys={"v": lambda x: x}),
                ListPlaceNode("Out", int),
                # Plain take -> index 0; TakeWhere -> index 2 (non-contiguous {0, 2} -> keyed_taken)
                ArgumentEdgeToTransition("Nums", "NonContig", "first"),
                TakeWhereEdge("Nums", "NonContig", "second", where=Filter.where(v=30)),
                FunctionTransitionNode("NonContig", pair),
                ReturnedEdgeFromTransition("NonContig", "Out"),
                # TakeWhere -> index 1; Plain take -> index 0 (contiguous {0, 1} -> taken=2)
                TakeWhereEdge("Nums", "Contig", "first", where=Filter.where(v=20)),
                ArgumentEdgeToTransition("Nums", "Contig", "second"),
                FunctionTransitionNode("Contig", pair),
                ReturnedEdgeFromTransition("Contig", "Out"),
            ])

        owner, outer = await _load_owner(multi_same_place)
        ids = outer.place_nodes["Nums"].token_ids
        non_contig = next(t for t in outer.graph.transitions if t.name == "NonContig")
        contig = next(t for t in outer.graph.transitions if t.name == "Contig")

        h_nc = owner._handed_over(outer, non_contig)
        assert h_nc.consumed == [("Nums", ids[0]), ("Nums", ids[2])]
        assert h_nc.taken == {}
        assert h_nc.keyed_taken == {"Nums": [0, 2]}
        # Verify _capture removes indices 0 and 2 without double-shifting.
        snap = owner._capture(outer, airborne=non_contig, handover=h_nc)
        assert snap.places["Nums"].tokens == [20, 40]
        assert snap.places["Nums"].ids == [ids[1], ids[3]]

        h_c = owner._handed_over(outer, contig)
        assert h_c.consumed == [("Nums", ids[1]), ("Nums", ids[0])]
        assert h_c.taken == {"Nums": 2}
        assert h_c.keyed_taken == {}
        snap_c = owner._capture(outer, airborne=contig, handover=h_c)
        assert snap_c.places["Nums"].tokens == [30, 40]
        assert snap_c.places["Nums"].ids == [ids[2], ids[3]]

    @pytest.mark.asyncio
    async def test_bound_edges_across_take_mutate_and_read(self):
        def dispatch(order: Order, courier: Courier, info: Courier) -> str:
            courier.load += 1
            return f"{order.oid}:{courier.name}:{info.name}"

        @petri_net(name="bound-handover")
        def bound_handover() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode(
                    "Orders", Order,
                    [Order(oid="o1", courier="missing"), Order(oid="o2", courier="bob")],
                    keys=("courier",),
                ),
                ListPlaceNode(
                    "Couriers", Courier,
                    [Courier(name="alice"), Courier(name="bob")],
                    keys=("name",),
                ),
                ListPlaceNode(
                    "Directory", Courier,
                    [Courier(name="carol"), Courier(name="bob")],
                    keys=("name",),
                ),
                ListPlaceNode("Log", str),
                ArgumentEdgeToTransition("Orders", "Dispatch", "order"),
                MutateWhereEdge(
                    "Couriers", "Dispatch", "courier",
                    where=Filter((Clause("name", input="Orders", input_key="courier"),)),
                ),
                ReadWhereEdge(
                    "Directory", "Dispatch", "info",
                    where=Filter((Clause("name", input="Orders", input_key="courier"),)),
                ),
                FunctionTransitionNode("Dispatch", dispatch),
                ReturnedEdgeFromTransition("Dispatch", "Log"),
            ])

        owner, outer = await _load_owner(bound_handover)
        t = outer.graph.transitions[0]
        order_ids = outer.place_nodes["Orders"].token_ids

        handover = owner._handed_over(outer, t)
        assert handover.consumed == [("Orders", order_ids[1])]
        assert handover.taken == {}
        assert handover.keyed_taken == {"Orders": [1]}
        assert handover.mutated == {"Couriers": {1}}
        assert handover.read == {"Directory": {1}}

    @pytest.mark.asyncio
    async def test_owner_distinct_on_snapshot_strict_types_and_single_key(self):
        gate = asyncio.Event()
        started = asyncio.Event()
        other_calls = 0

        def other_key(_x):
            nonlocal other_calls
            other_calls += 1
            return "x"

        async def slow(x: int) -> int:
            started.set()
            await gate.wait()
            return x

        @petri_net(name="distinct-airborne")
        def distinct_airborne() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Work", int, [99]),
                ListPlaceNode("Done", int),
                ListPlaceNode(
                    "Vals", object, [1, True, 0, False, 1, True],
                    keys={"v": lambda x: x, "other": other_key},
                ),
                ArgumentEdgeToTransition("Work", "Run", "x"),
                FunctionTransitionNode("Run", slow),
                ReturnedEdgeFromTransition("Run", "Done"),
            ])

        owner = Owner(Registry.from_factories([distinct_airborne]))
        await owner.handle({"id": "1", "method": "load", "params": {"net": "distinct-airborne"}}, NOW)
        step_out = await owner.handle({"id": "2", "method": "step", "params": {}}, NOW)
        task = asyncio.create_task(step_out.launch.coroutine)
        await started.wait()

        other_calls = 0
        distinct_out = await owner.handle({"id": "3", "method": "distinct", "params": {"key": "v"}}, NOW)
        values = distinct_out.lines[-1]["result"]["values"]
        assert [(type(v), v) for v in values] == [(int, 1), (bool, True), (int, 0), (bool, False)]
        assert other_calls == 0

        gate.set()
        landing = await task
        await owner.completed(landing, NOW)
