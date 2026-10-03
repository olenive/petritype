"""Event records on ``ExecutableGraph`` (DESIGN_RUNNER.md, "Events").

With ``record_events`` on, the engine and the runtime append passive event
records that ``drain_events()`` returns and clears. A firing is reported once,
when it completes: its ``Mutated`` records first, then one ``Fired`` or
``Failed``. Both execution modes report every firing exactly once.
"""

import asyncio
from dataclasses import dataclass

import pytest

from petritype.core.events import (
    Added, Failed, Fired, Mutated, ParamChanged, Removed, Toggled,
)
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReturnedEdgeFromTransition,
    TransitionFailedError,
)
from petritype.runtime import (
    Disable, Enable, ExecutionMode, Extend, RunContext, Runner, SetParam, SetTokens,
)


def increment(x: int) -> int:
    return x + 1


def scale(x: int, factor: int = 1) -> int:
    return x * factor


def _chain(tokens=(1, 2, 3), record_events=True, **graph_options):
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, list(tokens)),
        ArgumentEdgeToTransition("In", "Inc", "x"),
        FunctionTransitionNode("Inc", increment),
        ReturnedEdgeFromTransition("Inc", "Out"),
        ListPlaceNode("Out", int),
    ], record_events=record_events, **graph_options)


def _fire(graph, n=1):
    return asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=n))


def _drain_commands(graph, *commands):
    ctx = RunContext(graph=graph, inbox=asyncio.Queue())
    for command in commands:
        ctx.inbox.put_nowait(command)
    Runner.drain_inbox(ctx)
    return ctx


class TestRecording:

    def test_nothing_is_recorded_when_off(self):
        graph = _chain(record_events=False)
        _fire(graph, n=None)
        _drain_commands(graph, Extend("In", [4]), SetTokens("Out", []), Disable("Inc"),
                        SetParam("Inc", {}))
        assert graph.drain_events() == []
        assert graph._events == []

    def test_ids_are_kept_even_when_off(self):
        graph = _chain(record_events=False)
        _fire(graph, n=None)
        assert graph.place_named("Out").token_ids == (4, 5, 6)

    def test_drain_returns_in_order_and_empties(self):
        graph = _chain()
        _fire(graph, n=None)
        events = graph.drain_events()
        assert [event.consumed for event in events] == [
            (("In", 1),), (("In", 2),), (("In", 3),),
        ]
        assert [event.produced for event in events] == [
            (("Out", 4),), (("Out", 5),), (("Out", 6),),
        ]
        assert graph.drain_events() == []

    # Dumping a graph warns about its callables (bodies, selectors); not what this checks.
    @pytest.mark.filterwarnings("ignore::UserWarning")
    def test_events_are_not_dumped_with_the_model(self):
        graph = _chain()
        _fire(graph)
        dumped = graph.model_dump()
        assert "_events" not in dumped
        assert dumped["record_events"] is True
        assert dumped["next_token_id"] == 5

    def test_duration_is_a_non_negative_float(self):
        graph = _chain()
        _fire(graph)
        (fired,) = graph.drain_events()
        assert isinstance(fired.duration_ms, float)
        assert fired.duration_ms >= 0.0


@dataclass
class Box:
    items: list


class TestMutated:

    @staticmethod
    def _graph(body):
        return ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Shared", Box, [Box([])]),
            ListPlaceNode("In", int, [7]),
            MutateEdge("Shared", "Touch", "box"),
            ArgumentEdgeToTransition("In", "Touch", "x"),
            FunctionTransitionNode("Touch", body),
            ReturnedEdgeFromTransition("Touch", "Out"),
            ListPlaceNode("Out", int),
        ], record_events=True, restore_tokens_on_failure=True)

    def test_mutated_comes_before_fired(self):
        def touch(box: Box, x: int) -> int:
            box.items.append(x)
            return x

        graph = self._graph(touch)
        _fire(graph)
        events = graph.drain_events()
        assert events[0] == Mutated("Shared", 1)
        assert isinstance(events[1], Fired)
        assert events[1].consumed == (("In", 2),)

    def test_mutated_comes_before_failed(self):
        def touch_then_fail(box: Box, x: int) -> int:
            box.items.append(x)
            raise RuntimeError("after mutating")

        graph = self._graph(touch_then_fail)
        with pytest.raises(TransitionFailedError):
            _fire(graph)
        events = graph.drain_events()
        assert events[0] == Mutated("Shared", 1)
        assert isinstance(events[1], Failed)


def _exploding_graph(restore):
    def explode(x: int) -> int:
        raise ValueError("boom")

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, [1, 2]),
        ArgumentEdgeToTransition("In", "Explode", "x"),
        FunctionTransitionNode("Explode", explode),
        ReturnedEdgeFromTransition("Explode", "Out"),
        ListPlaceNode("Out", int),
    ], record_events=True, restore_tokens_on_failure=restore)


class TestFailed:

    @pytest.mark.parametrize("restore", [True, False])
    def test_failed_carries_restored(self, restore):
        graph = _exploding_graph(restore)
        with pytest.raises(TransitionFailedError):
            _fire(graph)
        (failed,) = graph.drain_events()
        assert failed == Failed(
            transition="Explode", consumed=(("In", 1),), restored=restore,
            error="ValueError: boom",
        )
        expected_ids = (1, 2) if restore else (2,)
        assert graph.place_named("In").token_ids == expected_ids

    def test_routing_failure_is_a_failed_firing(self):
        def wrong(x: int) -> int:
            return "not an int"  # type: ignore[return-value]

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [1]),
            ArgumentEdgeToTransition("In", "Wrong", "x"),
            FunctionTransitionNode("Wrong", wrong, output_distribution_function=lambda r: {"Out": r}),
            ReturnedEdgeFromTransition("Wrong", "Out"),
            ListPlaceNode("Out", int),
        ], record_events=True)
        with pytest.raises(TransitionFailedError):
            _fire(graph)
        (failed,) = graph.drain_events()
        assert isinstance(failed, Failed)
        assert failed.consumed == (("In", 1),)

    def test_deposit_failure_reports_failed_and_what_was_already_placed(self):
        """Indexed outputs are type-checked only at deposit (stage 3); the error
        still propagates unchanged, but the firing is reported, and the token the
        first edge already placed is reported as Added."""
        def pair(x: int) -> tuple[int, int]:
            return x, "not an int"  # type: ignore[return-value]

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [1]),
            ArgumentEdgeToTransition("In", "Pair", "x"),
            FunctionTransitionNode("Pair", pair),
            ReturnedEdgeFromTransition("Pair", "First", 0),
            ReturnedEdgeFromTransition("Pair", "Second", 1),
            ListPlaceNode("First", int),
            ListPlaceNode("Second", int),
        ], record_events=True)
        with pytest.raises(TypeError):
            _fire(graph)
        added, failed = graph.drain_events()
        assert added == Added("First", 2)
        assert failed.transition == "Pair" and failed.restored is False
        assert failed.consumed == (("In", 1),)


class TestCommandEvents:

    def test_toggled_only_on_change(self):
        graph = _chain()
        ctx = _drain_commands(graph, Enable("Inc"), Disable("Inc"), Disable("Inc"),
                              Enable("Inc"), Enable("Inc"))
        assert graph.drain_events() == [Toggled("Inc", False), Toggled("Inc", True)]
        assert ctx.disabled == set()

    def test_param_changed_once_per_keyword(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [2]),
            ArgumentEdgeToTransition("In", "Scale", "x"),
            FunctionTransitionNode("Scale", scale),
            ReturnedEdgeFromTransition("Scale", "Out"),
            ListPlaceNode("Out", int),
        ], record_events=True)
        _drain_commands(graph, SetParam("Scale", {"factor": 3}))
        assert graph.drain_events() == [ParamChanged("Scale", "factor")]

    def test_rejected_commands_record_nothing(self):
        graph = _chain()
        _drain_commands(graph, Disable("Nope"), SetParam("Nope", {"a": 1}), Extend("Nope", [1]))
        assert graph.drain_events() == []

    def test_extend_and_set_tokens(self):
        graph = _chain(tokens=(1,))
        _drain_commands(graph, Extend("Out", [5]), SetTokens("Out", [6]))
        assert graph.drain_events() == [Added("Out", 2), Removed("Out", 2), Added("Out", 3)]


def _two_lane_graph():
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("A", int, [1, 2]),
        ArgumentEdgeToTransition("A", "Left", "x"),
        FunctionTransitionNode("Left", increment),
        ReturnedEdgeFromTransition("Left", "A Out"),
        ListPlaceNode("A Out", int),
        ListPlaceNode("B", int, [3]),
        ArgumentEdgeToTransition("B", "Right", "x"),
        FunctionTransitionNode("Right", increment),
        ReturnedEdgeFromTransition("Right", "B Out"),
        ListPlaceNode("B Out", int),
    ], record_events=True)


def _check_fired_events_match_the_marking(graph, events):
    fired = [event for event in events if isinstance(event, Fired)]
    assert len(fired) == graph.step_count == sum(graph.fired_counts.values())
    consumed = [pair for event in fired for pair in event.consumed]
    produced = [pair for event in fired for pair in event.produced]
    assert sorted(consumed) == [("A", 1), ("A", 2), ("B", 3)]
    assert sorted(produced) == sorted(
        (place.name, token_id) for place in graph.places for token_id in place.token_ids
    )
    for event in fired:
        assert isinstance(event.duration_ms, float) and event.duration_ms >= 0.0
    return fired


class TestConcurrent:

    def test_one_fired_per_completion(self):
        graph = _two_lane_graph()
        asyncio.run(Runner.run_to_completion(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)))
        fired = _check_fired_events_match_the_marking(graph, graph.drain_events())
        assert sorted(event.transition for event in fired) == ["Left", "Left", "Right"]

    def test_tick_mode_reports_every_firing(self):
        graph = _two_lane_graph()

        async def drive():
            ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, stop=asyncio.Event())
            task = asyncio.create_task(Runner.run_indefinitely(ctx, tick=0.001))
            while graph.step_count < 3:
                await asyncio.sleep(0.001)
            ctx.stop.set()
            await task

        asyncio.run(drive())
        _check_fired_events_match_the_marking(graph, graph.drain_events())

    def test_duration_measures_the_body(self):
        async def slow(x: int) -> int:
            await asyncio.sleep(0.02)
            return x

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [1]),
            ArgumentEdgeToTransition("In", "Slow", "x"),
            FunctionTransitionNode("Slow", slow),
            ReturnedEdgeFromTransition("Slow", "Out"),
            ListPlaceNode("Out", int),
        ], record_events=True)
        asyncio.run(Runner.run_to_completion(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)))
        (fired,) = graph.drain_events()
        assert fired.duration_ms >= 15.0

    @pytest.mark.parametrize("restore", [True, False])
    def test_failed_in_concurrent_mode(self, restore):
        graph = _exploding_graph(restore)
        with pytest.raises(TransitionFailedError):
            asyncio.run(Runner.run_to_completion(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)))
        failed = [event for event in graph.drain_events() if isinstance(event, Failed)]
        assert failed == [Failed("Explode", (("In", 1),), restore, "ValueError: boom")]
        assert graph.place_named("In").token_ids == ((1, 2) if restore else (2,))

    def test_sequential_runner_matches(self):
        graph = _two_lane_graph()
        asyncio.run(Runner.run_to_completion(RunContext(graph=graph)))
        _check_fired_events_match_the_marking(graph, graph.drain_events())
