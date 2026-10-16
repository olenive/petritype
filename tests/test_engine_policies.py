"""The engine's two scheduling policies of 2026-10-12, both defaulting to what the
engine did before: the round-robin tie-break (``tie_break="round_robin"``) and the
interleaved deep step (``deep_policy="interleaved"``), as the library's own drivers
honour them (``fire_one_transition``, ``execute_graph``, the sequential ``Runner``).
The protocol runner's side is in ``tests/test_runner_protocol_gaps.py``.
"""

import asyncio

import pytest
from pydantic import BaseModel

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    NestedNets,
    ReturnedEdgeFromTransition,
    TransitionFailedError,
)
from petritype.core.serialisation import NetJson
from petritype.runtime import RunContext, Runner


def run(coroutine):
    return asyncio.run(coroutine)


async def deep_steps(graph: ExecutableGraph, count: int) -> list:
    """``count`` deep steps, each the engine's own choice; what each fired."""
    maps = ExecutableGraphOperations.maps(graph)
    return [await ExecutableGraphOperations.fire_one_transition(graph, *maps) for _ in range(count)]


def passing(x: int) -> int:
    return x


# --- the round-robin tie-break ------------------------------------------------------------------


def two_stages(**kwargs) -> ExecutableGraph:
    """Source -(Stage1)-> A -(Stage2)-> Sink, with tokens waiting at both stages, so
    both are live at once for a while."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Source", int, [1, 2, 3]),
        ArgumentEdgeToTransition("Source", "Stage1", "x"),
        FunctionTransitionNode("Stage1", passing),
        ReturnedEdgeFromTransition("Stage1", "A"),
        ListPlaceNode("A", int, [10, 20, 30]),
        ArgumentEdgeToTransition("A", "Stage2", "x"),
        FunctionTransitionNode("Stage2", passing),
        ReturnedEdgeFromTransition("Stage2", "Sink"),
        ListPlaceNode("Sink", int),
    ], **kwargs)


def shared_input(names, priorities=None, **kwargs) -> ExecutableGraph:
    """Transitions all drawing from one place of many tokens, so all are live for good."""
    priorities = priorities or {}
    nodes = [ListPlaceNode("In", int, list(range(20))), ListPlaceNode("Out", int)]
    for name in names:
        nodes += [
            FunctionTransitionNode(name, passing, priority=priorities.get(name)),
            ArgumentEdgeToTransition("In", name, "x"),
            ReturnedEdgeFromTransition(name, "Out"),
        ]
    return ExecutableGraphOperations.construct_graph(nodes, **kwargs)


class TestRoundRobin:

    def test_a_pipeline_alternates_its_stages_while_both_are_live(self):
        orders = {}
        for policy in ("first_declared", "last_declared", "round_robin"):
            graph = two_stages(tie_break=policy)
            orders[policy] = run(deep_steps(graph, 9))
        assert orders["first_declared"] == ["Stage1"] * 3 + ["Stage2"] * 6
        assert orders["last_declared"] == ["Stage2"] * 3 + ["Stage1", "Stage2"] * 3
        assert orders["round_robin"] == ["Stage1", "Stage2"] * 3 + ["Stage2"] * 3, "in turn while both can fire"

    def test_the_turn_wraps_round_and_starts_at_the_first_declared(self):
        graph = shared_input(["A", "B", "C"], tie_break="round_robin")
        assert graph._last_pick is None
        assert run(deep_steps(graph, 7)) == ["A", "B", "C", "A", "B", "C", "A"]
        assert graph._last_pick == 0, "the declaration index of the last pick"

    def test_only_the_top_priority_takes_turns(self):
        graph = shared_input(["A", "B", "C"], priorities={"B": lambda g: 1.0, "C": lambda g: 1.0},
                             tie_break="round_robin")
        assert run(deep_steps(graph, 4)) == ["B", "C", "B", "C"]

    def test_a_custom_selector_neither_reads_nor_moves_the_cursor(self):
        graph = shared_input(["A", "B", "C"], tie_break="round_robin")
        assert run(deep_steps(graph, 2)) == ["A", "B"]
        graph.transition_selector = lambda g, fireable: fireable[-1] if fireable else None
        assert run(deep_steps(graph, 2)) == ["C", "C"]
        assert graph._last_pick == 1, "still B"
        graph.transition_selector = None
        assert run(deep_steps(graph, 1)) == ["C"], "the default selector goes on from B"

    def test_a_disabled_transition_is_never_a_candidate(self):
        graph = shared_input(["A", "B", "C"], tie_break="round_robin")
        ctx = RunContext(graph=graph)
        ctx.disabled.add("B")
        summary = run(Runner.run(ctx, stop_after_n_firings=4))
        assert summary.fired == {"A": 2, "C": 2}
        assert [t.name for t in graph.transition_history] == ["C"]

    def test_each_net_has_its_own_cursor_and_a_loaded_net_none(self):
        graph = shared_input(["A", "B"], tie_break="round_robin")
        other = shared_input(["A", "B"], tie_break="round_robin")
        run(deep_steps(graph, 1))
        assert (graph._last_pick, other._last_pick) == (0, None)
        NetJson.load_into(graph, NetJson.save(graph))
        assert graph._last_pick is None, "the cursor is not saved: a loaded net starts its round afresh"
        assert run(deep_steps(graph, 1)) == ["A"]

    def test_execute_graph_and_the_runner_take_the_same_turns(self):
        log = run(ExecutableGraphOperations.execute_graph(shared_input(["A", "B"], tie_break="round_robin"),
                                                          stop_after_n_firings=4, transition_history_length=4))
        assert [t.name for t in log[0].transition_history] == ["A", "B", "A", "B"]
        graph = shared_input(["A", "B"], tie_break="round_robin")
        assert run(Runner.run(RunContext(graph=graph), stop_after_n_firings=3)).fired == {"A": 2, "B": 1}

    def test_a_tie_break_switched_to_round_robin_resumes_after_the_last_pick(self):
        graph = shared_input(["A", "B", "C"])
        assert run(deep_steps(graph, 2)) == ["A", "A"]
        assert graph._last_pick == 0, "every pick of the default selector moves the cursor"
        graph.tie_break = "round_robin"
        assert run(deep_steps(graph, 2)) == ["B", "C"]
        graph.tie_break = "last_declared"
        assert run(deep_steps(graph, 1)) == ["C"]
        graph.tie_break = "round_robin"
        assert run(deep_steps(graph, 1)) == ["A"], "after C, wrapping round"

    def test_the_setting_is_checked(self):
        with pytest.raises(ValueError):
            shared_input(["A"], tie_break="random")
        assert shared_input(["A"]).tie_break == "first_declared"


# --- the interleaved deep step -------------------------------------------------------------------


def tick(x: int) -> int:
    return x + 1


def build_counter(low=()) -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Low", int, list(low)),
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


def keep_time(clock: int) -> int:
    return clock + 1


def clockwork(boxes, live_outer: bool = True, **kwargs) -> ExecutableGraph:
    """A shelf of boxes holding counters, and, when ``live_outer``, an outer
    ``Tick`` that can always fire (it takes the clock token and puts it back)."""
    nodes = [ListPlaceNode("Shelf", Box, list(boxes), nested=True)]
    if live_outer:
        nodes += [
            ListPlaceNode("Clock", int, [0]),
            ArgumentEdgeToTransition("Clock", "Tick", "clock"),
            FunctionTransitionNode("Tick", keep_time),
            ReturnedEdgeFromTransition("Tick", "Clock"),
        ]
    return ExecutableGraphOperations.construct_graph(nodes, **kwargs)


def box(label: str, low) -> Box:
    return Box(label=label, counter=build_counter(low))


class TestInterleaved:

    def test_the_default_is_outer_first_and_starves_an_inner_net_under_a_live_outer_net(self):
        graph = clockwork([box("a", [1, 2])])
        assert graph.deep_policy == "outer_first"
        assert run(deep_steps(graph, 5)) == ["Tick"] * 5
        assert graph.place_named("Shelf").tokens[0].counter.place_named("High").tokens == [], "starved"

    def test_interleaved_gives_the_inner_net_its_turn(self):
        graph = clockwork([box("a", [1, 2])], deep_policy="interleaved")
        fired = run(deep_steps(graph, 6))
        assert fired == ["Tick", "Shelf/#1/counter/Up", "Tick", "Shelf/#1/counter/Up", "Tick", "Tick"]
        assert graph.place_named("Shelf").tokens[0].counter.place_named("High").tokens == [2, 3]
        assert graph.step_count == 6 and graph.fired_counts == {"Tick": 4, "Shelf/#1/counter/Up": 2}

    def test_two_live_inner_nets_alternate(self):
        under = {}
        for policy in ("outer_first", "interleaved"):
            graph = clockwork([box("a", [1, 2, 3]), box("b", [4, 5, 6])], live_outer=False, deep_policy=policy)
            under[policy] = [name.split("/")[1] for name in run(deep_steps(graph, 6))]
        assert under["outer_first"] == ["#1"] * 3 + ["#2"] * 3
        assert under["interleaved"] == ["#1", "#2"] * 3

    def test_quiescence_is_the_same_and_the_turn_starts_after_the_last(self):
        graph = clockwork([box("a", [1]), box("b", [2])], live_outer=False, deep_policy="interleaved")
        first, second = (b.counter for b in graph.place_named("Shelf").tokens)
        assert run(deep_steps(graph, 3)) == ["Shelf/#1/counter/Up", "Shelf/#2/counter/Up", None]
        assert graph._last_turn == second.net_id, "the cursor: the net that fired last"
        NestedNets.put(first, "Low", [7])
        NestedNets.put(second, "Low", [8])
        assert run(deep_steps(graph, 1)) == ["Shelf/#1/counter/Up"], "round from after b: the outer net, then a"

    def test_the_cursor_is_forgotten_by_a_load_and_a_gone_net_starts_the_round_at_the_outer_net(self):
        graph = clockwork([box("a", [1, 2])], deep_policy="interleaved")
        assert run(deep_steps(graph, 2)) == ["Tick", "Shelf/#1/counter/Up"]
        graph.place_named("Shelf").tokens.clear()  # the box is gone (resynced at the next step)
        assert run(deep_steps(graph, 1)) == ["Tick"]
        graph = clockwork([box("a", [1, 2])], deep_policy="interleaved")
        run(deep_steps(graph, 1))
        graph.forget_cursors()
        assert graph._last_turn is None and run(deep_steps(graph, 1)) == ["Tick"]

    def test_a_deeper_net_takes_its_turn_and_every_net_between_counts_it(self):
        def holder(label: str, inner_boxes) -> Box:
            return Box(label=label, counter=ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Inside", Box, list(inner_boxes), nested=True),
            ]))
        graph = clockwork([holder("outer box", [box("inner box", [1])])], deep_policy="interleaved")
        middle = graph.place_named("Shelf").tokens[0].counter
        cycle = [path for path, _net in NestedNets.deep_cycle(graph)]
        assert cycle == [(), ("Shelf", "#1", "counter"), ("Shelf", "#1", "counter", "Inside", "#1", "counter")]
        assert run(deep_steps(graph, 3)) == ["Tick", "Shelf/#1/counter/Inside/#1/counter/Up", "Tick"]
        assert middle.fired_counts == {"Inside/#1/counter/Up": 1} and middle.step_count == 1

    def test_the_stepped_nets_policy_governs_the_whole_step(self):
        middle = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Inside", Box, [box("a", [1, 2]), box("b", [3, 4])], nested=True),
        ], deep_policy="interleaved")  # an inner net's own setting: unused when the outer net steps
        graph = clockwork([Box(label="holder", counter=middle)], live_outer=False)
        fired = run(deep_steps(graph, 4))
        assert [name.split("/")[4] for name in fired] == ["#1", "#1", "#2", "#2"], "outer first all the way down"
        assert middle._last_turn == middle.place_named("Inside").tokens[1].counter.net_id, "its cursor is kept all the same"
        middle = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Inside", Box, [box("a", [1, 2]), box("b", [3, 4])], nested=True),
        ])
        graph = clockwork([Box(label="holder", counter=middle)], live_outer=False, deep_policy="interleaved")
        assert [name.split("/")[4] for name in run(deep_steps(graph, 4))] == ["#1", "#2", "#1", "#2"]

    def test_a_failing_inner_firing_took_its_turn(self):
        def explode(x: int) -> int:
            raise RuntimeError("boom")
        fragile = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Low", int, [1]),
            ArgumentEdgeToTransition("Low", "Up", "x"),
            FunctionTransitionNode("Up", explode),
            ReturnedEdgeFromTransition("Up", "High"),
            ListPlaceNode("High", int),
        ])
        graph = clockwork([Box(label="f", counter=fragile)], deep_policy="interleaved")
        assert run(deep_steps(graph, 1)) == ["Tick"]
        with pytest.raises(TransitionFailedError):
            run(deep_steps(graph, 1))
        assert graph._last_turn == fragile.net_id
        assert run(deep_steps(graph, 1)) == ["Tick"]

    def test_a_policy_switched_to_interleaved_resumes_after_the_net_that_fired_last(self):
        graph = clockwork([box("a", [1, 2]), box("b", [3, 4])], live_outer=False)
        first, second = (b.counter for b in graph.place_named("Shelf").tokens)
        assert run(deep_steps(graph, 1)) == ["Shelf/#1/counter/Up"]
        assert graph._last_turn == first.net_id, "recorded under outer_first too"
        graph.deep_policy = "interleaved"
        assert run(deep_steps(graph, 2)) == ["Shelf/#2/counter/Up", "Shelf/#1/counter/Up"]
        graph = clockwork([box("a", [1])])
        assert run(deep_steps(graph, 1)) == ["Tick"] and graph._last_turn == graph.net_id
        graph.deep_policy = "interleaved"
        assert run(deep_steps(graph, 1)) == ["Shelf/#1/counter/Up"], "after the outer net: the box"

    def test_execute_graph_and_the_sequential_runner_honour_it(self):
        graph = clockwork([box("a", [1, 2])], deep_policy="interleaved")
        graph, fired = run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=4))
        assert fired == 4 and graph.fired_counts == {"Tick": 2, "Shelf/#1/counter/Up": 2}
        graph = clockwork([box("a", [1, 2])], deep_policy="interleaved")
        summary = run(Runner.run(RunContext(graph=graph), stop_after_n_firings=4))
        assert summary.fired == {"Tick": 2, "Shelf/#1/counter/Up": 2}
        graph = clockwork([box("a", [1, 2]), box("b", [3])], live_outer=False, deep_policy="interleaved")
        summary = run(Runner.run(RunContext(graph=graph)))
        assert summary.quiesced and summary.n_fired == 3

    def test_the_setting_is_checked(self):
        with pytest.raises(ValueError):
            clockwork([], deep_policy="inner_first")
