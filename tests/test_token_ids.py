"""Stable token ids on ``ExecutableGraph`` (DESIGN_RUNNER.md, "Token ids").

Every token gets a monotonic integer id from ``graph.next_token_id`` when it
enters a place; ``place.token_ids`` runs parallel to ``place.tokens``. Ids move
with their tokens, are retired when a token is consumed or removed, and are
never reused. Firings report which ids they consumed and produced through the
``Fired`` / ``Failed`` events, which these tests read back to check that the
reports match what actually left and arrived.
"""

import asyncio
from dataclasses import dataclass

import pytest

from petritype.core.events import Added, Fired, Mutated, Removed, Resynced
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    TokenIds,
    TransitionFailedError,
)
from petritype.runtime import Extend, RunContext, Runner, SetTokens


def increment(x: int) -> int:
    return x + 1


def _chain(tokens=(1, 2, 3), **graph_options):
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, list(tokens)),
        ArgumentEdgeToTransition("In", "Inc", "x"),
        FunctionTransitionNode("Inc", increment),
        ReturnedEdgeFromTransition("Inc", "Out"),
        ListPlaceNode("Out", int),
    ], **graph_options)


def _fire(graph, n=1):
    return asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=n))


def _apply(graph, *commands):
    ctx = RunContext(graph=graph, inbox=asyncio.Queue())
    for command in commands:
        ctx.inbox.put_nowait(command)
    Runner.drain_inbox(ctx)
    return ctx


def _all_ids(graph):
    return [token_id for place in graph.places for token_id in place.token_ids]


class TestSeeds:

    def test_seed_tokens_are_numbered_from_one_in_place_order(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("A", int, [10, 20]),
            ListPlaceNode("B", str, ["x"]),
            ListPlaceNode("C", int, [30, 40, 50]),
        ])
        assert graph.place_named("A").token_ids == (1, 2)
        assert graph.place_named("B").token_ids == (3,)
        assert graph.place_named("C").token_ids == (4, 5, 6)
        assert graph.next_token_id == 7

    def test_empty_graph_starts_the_counter_at_one(self):
        assert _chain(tokens=()).next_token_id == 1

    def test_token_ids_is_a_read_only_tuple(self):
        ids = _chain().place_named("In").token_ids
        assert isinstance(ids, tuple)


class TestFiring:

    def test_consumed_and_produced_match_what_left_and_arrived(self):
        graph = _chain(record_events=False)
        graph.record_events = True
        before_in = graph.place_named("In").token_ids
        _fire(graph)
        (fired,) = graph.drain_events()
        assert isinstance(fired, Fired)
        assert fired.consumed == (("In", before_in[0]),)
        assert graph.place_named("In").token_ids == before_in[1:]
        assert fired.produced == (("Out", graph.place_named("Out").token_ids[-1]),)
        assert fired.produced[0][1] == 4  # the next id after the three seeds

    def test_ids_are_never_reused(self):
        graph = _chain(tokens=(1, 2, 3))
        seen = set(_all_ids(graph))
        for _ in range(3):
            _fire(graph)
            current = set(_all_ids(graph))
            fresh = current - seen
            assert fresh, "a firing produced no new id"
            seen |= current
        _apply(graph, SetTokens("Out", [7, 8]), Extend("In", [9]))
        current = set(_all_ids(graph))
        assert not (current - set(range(1, graph.next_token_id)))
        assert current.isdisjoint(seen)
        assert graph.next_token_id == 10  # 3 seeds + 3 deposits + 2 + 1

    def test_whole_list_argument_consumes_every_id_in_order(self):
        def total(xs: list[int]) -> int:
            return sum(xs)

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [1, 2, 3]),
            ArgumentEdgeToTransition("In", "Sum", "xs"),
            FunctionTransitionNode("Sum", total),
            ReturnedEdgeFromTransition("Sum", "Out"),
            ListPlaceNode("Out", int),
        ], record_events=True)
        _fire(graph)
        (fired,) = graph.drain_events()
        assert fired.consumed == (("In", 1), ("In", 2), ("In", 3))
        assert fired.produced == (("Out", 4),)
        assert graph.place_named("In").token_ids == ()

    def test_batch_deposit_gives_each_token_its_own_id(self):
        def split(x: int) -> list[int]:
            return [x, x, x]

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [5]),
            ArgumentEdgeToTransition("In", "Split", "x"),
            FunctionTransitionNode("Split", split),
            ReturnedEdgeFromTransition("Split", "Out"),
            ListPlaceNode("Out", int),
        ], record_events=True)
        _fire(graph)
        (fired,) = graph.drain_events()
        assert fired.produced == (("Out", 2), ("Out", 3), ("Out", 4))
        assert graph.place_named("Out").token_ids == (2, 3, 4)

    def test_copy_under_allow_token_copying_gets_a_new_id(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [1]),
            ArgumentEdgeToTransition("In", "Inc", "x"),
            FunctionTransitionNode("Inc", increment),
            ReturnedEdgeFromTransition("Inc", "Left"),
            ReturnedEdgeFromTransition("Inc", "Right"),
            ListPlaceNode("Left", int),
            ListPlaceNode("Right", int),
        ], allow_token_copying=True, record_events=True)
        _fire(graph)
        (fired,) = graph.drain_events()
        assert fired.produced == (("Left", 2), ("Right", 3))
        assert graph.place_named("Left").token_ids == (2,)
        assert graph.place_named("Right").token_ids == (3,)


def _exploding_graph(**graph_options):
    def explode(a: int, b: int) -> int:
        raise ValueError("boom")

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("A", int, [1, 2]),
        ListPlaceNode("B", int, [3, 4]),
        ArgumentEdgeToTransition("A", "Explode", "a"),
        ArgumentEdgeToTransition("B", "Explode", "b"),
        FunctionTransitionNode("Explode", explode),
        ReturnedEdgeFromTransition("Explode", "Out"),
        ListPlaceNode("Out", int),
    ], **graph_options)


class TestRestore:

    def test_restore_puts_the_same_ids_back_at_the_front(self):
        graph = _exploding_graph(restore_tokens_on_failure=True)
        with pytest.raises(TransitionFailedError):
            _fire(graph)
        assert graph.place_named("A").tokens == [1, 2]
        assert graph.place_named("A").token_ids == (1, 2)
        assert graph.place_named("B").token_ids == (3, 4)
        assert graph.next_token_id == 5

    def test_restore_of_a_whole_list_keeps_the_order_taken(self):
        def explode(xs: list[int]) -> int:
            raise ValueError("boom")

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [1, 2, 3]),
            ArgumentEdgeToTransition("In", "Explode", "xs"),
            FunctionTransitionNode("Explode", explode),
            ReturnedEdgeFromTransition("Explode", "Out"),
            ListPlaceNode("Out", int),
        ], restore_tokens_on_failure=True)
        with pytest.raises(TransitionFailedError):
            _fire(graph)
        assert graph.place_named("In").token_ids == (1, 2, 3)

    def test_without_restore_the_consumed_ids_are_retired(self):
        graph = _exploding_graph()
        with pytest.raises(TransitionFailedError):
            _fire(graph)
        assert graph.place_named("A").token_ids == (2,)
        assert graph.place_named("B").token_ids == (4,)
        assert TokenIds.locate(graph, 1) is None


@dataclass
class Box:
    items: list


def _read_edge_graph(edge_class):
    def bump(boxes: list[Box], x: int) -> int:
        boxes[0].items.append(x)
        return x

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Shared", Box, [Box([0]), Box([1])]),
        ListPlaceNode("In", int, [7]),
        edge_class("Shared", "Bump", "boxes"),
        ArgumentEdgeToTransition("In", "Bump", "x"),
        FunctionTransitionNode("Bump", bump),
        ReturnedEdgeFromTransition("Bump", "Out"),
        ListPlaceNode("Out", int),
    ], record_events=True)


class TestReadEdges:

    def test_snapshot_read_changes_nothing(self):
        graph = _read_edge_graph(SnapshotEdge)
        _fire(graph)
        assert graph.place_named("Shared").tokens == [Box([0]), Box([1])]
        assert graph.place_named("Shared").token_ids == (1, 2)
        events = graph.drain_events()
        assert not any(isinstance(event, Mutated) for event in events)
        assert events[-1].consumed == (("In", 3),)

    def test_mutate_keeps_the_id_and_records_a_mutation(self):
        graph = _read_edge_graph(MutateEdge)
        _fire(graph)
        assert graph.place_named("Shared").tokens == [Box([0, 7]), Box([1])]
        assert graph.place_named("Shared").token_ids == (1, 2)
        events = graph.drain_events()
        assert events[:2] == [Mutated("Shared", 1), Mutated("Shared", 2)]
        assert isinstance(events[2], Fired)

    def test_single_token_mutate_edge_marks_only_the_front_token(self):
        def touch(item: list[int], x: int) -> int:
            item.append(x)
            return x

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Shared", list[int], [[0], [1]]),
            ListPlaceNode("In", int, [7]),
            MutateEdge("Shared", "Touch", "item"),
            ArgumentEdgeToTransition("In", "Touch", "x"),
            FunctionTransitionNode("Touch", touch),
            ReturnedEdgeFromTransition("Touch", "Out"),
            ListPlaceNode("Out", int),
        ], record_events=True)
        _fire(graph)
        events = graph.drain_events()
        assert events[0] == Mutated("Shared", 1)
        assert isinstance(events[1], Fired)
        assert len(events) == 2


class TestCommands:

    def test_extend_gives_fresh_ids(self):
        graph = _chain(tokens=(1,), record_events=True)
        _apply(graph, Extend("In", [2, 3]))
        assert graph.place_named("In").token_ids == (1, 2, 3)
        assert graph.drain_events() == [Added("In", 2), Added("In", 3)]

    def test_set_tokens_removes_every_old_id_and_adds_new_ones(self):
        graph = _chain(tokens=(1, 2), record_events=True)
        _apply(graph, SetTokens("In", [1, 2, 3]))
        assert graph.place_named("In").token_ids == (3, 4, 5)
        assert graph.drain_events() == [
            Removed("In", 1), Removed("In", 2),
            Added("In", 3), Added("In", 4), Added("In", 5),
        ]

    def test_set_tokens_empty_list_clears_the_ids(self):
        graph = _chain(tokens=(1, 2))
        _apply(graph, SetTokens("In", []))
        assert graph.place_named("In").token_ids == ()

    def test_rejected_command_assigns_nothing(self):
        graph = _chain(tokens=(1,), record_events=True)
        _apply(graph, Extend("In", ["not an int"]))
        assert graph.place_named("In").token_ids == (1,)
        assert graph.next_token_id == 2
        assert graph.drain_events() == []


class TestResync:

    def test_direct_assignment_is_detected_and_re_ided(self):
        graph = _chain(tokens=(1, 2), record_events=True)
        graph.place_named("In").tokens = [5, 6, 7]
        _fire(graph)
        events = graph.drain_events()
        assert events[0] == Resynced("In")
        # Re-ided 3, 4, 5; the firing consumed the front one and produced 6.
        assert events[1].consumed == (("In", 3),)
        assert events[1].produced == (("Out", 6),)
        assert graph.place_named("In").token_ids == (4, 5)

    def test_in_sync_places_are_left_alone(self):
        graph = _chain(tokens=(1, 2), record_events=True)
        assert TokenIds.resync(graph) == ()
        assert graph.drain_events() == []

    def test_command_application_resyncs_first(self):
        graph = _chain(tokens=(1,), record_events=True)
        graph.place_named("Out").tokens.append(9)
        _apply(graph, Extend("In", [2]))
        assert graph.drain_events() == [Resynced("Out"), Added("In", 3)]
        assert graph.place_named("Out").token_ids == (2,)

    def test_resync_is_recorded_only_when_events_are_on(self):
        graph = _chain(tokens=(1,))
        graph.place_named("In").tokens = []
        assert TokenIds.resync(graph) == ("In",)
        assert graph.drain_events() == []


class TestLocate:

    def test_finds_the_place_and_token(self):
        graph = _chain(tokens=(10, 20))
        assert TokenIds.locate(graph, 2) == ("In", 20)

    def test_none_for_a_retired_id(self):
        graph = _chain(tokens=(10, 20))
        _fire(graph)
        assert TokenIds.locate(graph, 1) is None
        assert TokenIds.locate(graph, 3) == ("Out", 11)

    def test_none_for_an_id_never_issued(self):
        assert TokenIds.locate(_chain(), 999) is None
