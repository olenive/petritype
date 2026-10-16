"""Input edges that accept an empty place (``EDGES_ON_EMPTY_PLACES.md``, Python
track): ``DrainEdge`` takes every token of its place and ``SurveyEdge`` a deep copy
of every token, zero or more, and neither holds its transition back.

Two example nets are the acceptance nets, one per edge (split on 2026-10-16). The shop
of ``examples/edges_on_empty_places/shop.py`` surveys its promotions: the survey tests
run on it. The traffic light of ``examples/edges_on_empty_places/traffic_light.py``
drains its waiting cars while the light is green, and reads the light with a plain
``SnapshotEdge``: the drain tests run on it. Where neither net can express a case, a small
net is built inline. Since later on 2026-10-16 the light is one place, ``Light``, whose token
is its colour, and the net has two transitions: ``Change`` cycles the light, unguarded, and
``Go`` drains ``Waiting`` and reads ``Light``, guarded on green and someone waiting. The read
always has a token to read, so the guard is what holds the cars at amber and red; and the
guard is what keeps ``Go`` from firing on an empty queue, so the drain's zero case is shown
with the guard taken off."""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.edges_on_empty_places import traffic_light as light_example  # noqa: E402
from examples.edges_on_empty_places.shop import (  # noqa: E402
    SCRIPT, build_shop, fireable, marking, price_order, put, shop, step,
)
from examples.edges_on_empty_places.traffic_light import (  # noqa: E402
    Colour, build_traffic_light, go, green_and_someone_waiting, traffic_light,
)
from petritype.core.events import Failed, Fired, Mutated  # noqa: E402
from petritype.core.executable_graph_components import (  # noqa: E402
    ArgumentEdgeToTransition,
    DrainEdge,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    SurveyEdge,
    TransitionFailedError,
)
from petritype.runner.owner import Owner  # noqa: E402
from petritype.runner.registry import Registry  # noqa: E402
from petritype.runtime import ControlSpec, ExecutionMode, Extend, RunContext, Runner  # noqa: E402

# The shop after each step of its script: Priced, Discounts Applied and Promotions.
SHOP_AFTER_EACH_STEP = (
    {"Priced": ["Order 1"], "Discounts Applied": [], "Promotions": []},
    {"Priced": ["Order 1", "Order 2"], "Discounts Applied": ["10% off on Order 2", "Free shipping on Order 2"],
     "Promotions": ["10% off", "Free shipping"]},
)

# The traffic light after each step of the transcript's script (PROTOCOL.md §11).
LIGHT_AFTER_EACH_STEP = (
    {"Light": ["green"], "Waiting": [], "Passed": []},
    {"Light": ["amber"], "Waiting": [], "Passed": []},
    {"Light": ["red"], "Waiting": ["Car 1", "Car 2"], "Passed": []},
    {"Light": ["green"], "Waiting": ["Car 1", "Car 2"], "Passed": []},
    {"Light": ["green"], "Waiting": [], "Passed": ["Car 1", "Car 2"]},
    {"Light": ["green"], "Waiting": [], "Passed": ["Car 1", "Car 2", "Car 3"]},
    {"Light": ["amber"], "Waiting": [], "Passed": ["Car 1", "Car 2", "Car 3"]},
)


def is_green(graph):
    """Half of ``Go``'s guard: the light is green, whoever is waiting."""
    return light_example.colour(graph) == "green"


def fired_events(graph):
    return [event for event in graph.drain_events() if isinstance(event, Fired)]


def ids(graph, place):
    return graph.place_named(place).token_ids


def with_changes(graph, *changes):
    """``graph``'s components with ``changes`` applied: ``(drop, add)`` pairs where
    ``drop`` is a predicate over a component. ``add`` takes the place of the first
    component dropped, so a replaced transition keeps its place in the declaration order
    (which the tie-break reads), or goes at the end when nothing is dropped."""
    components = [*graph.places, *graph.transitions, *graph.argument_edges, *graph.return_edges,
                  *graph.snapshot_edges, *graph.mutate_edges]
    for drop, add in changes:
        kept, at = [], None
        for component in components:
            if not drop(component):
                kept.append(component)
            elif at is None:
                at = len(kept)
        at = len(kept) if at is None else at
        components = [*kept[:at], *add, *kept[at:]]
    return components


def shop_with(*changes):
    return with_changes(build_shop(), *changes)


def light_with(*changes):
    return with_changes(build_traffic_light(), *changes)


def replacing(name, transition):
    """A change replacing the transition called ``name`` with ``transition``."""
    return (lambda c: isinstance(c, FunctionTransitionNode) and c.name == name, [transition])


def seeding(name, place):
    """A change replacing the place called ``name`` with ``place``."""
    return (lambda c: isinstance(c, ListPlaceNode) and c.name == name, [place])


def at_green(graph):
    """``graph`` stepped from the seed, red, to green: one ``Change``."""
    assert light_example.step(graph) == 1 and graph.last_fired == "Change"
    assert light_example.colour(graph) == "green"
    return graph


async def send_all(owner, now, requests):
    """Each request in turn through ``owner``, each launch awaited and landed; the lines
    each one caused."""
    out_lines = []
    for n, (method, params) in enumerate(requests, 1):
        out = await owner.handle({"id": str(n), "method": method, "params": params or {}}, now)
        lines = list(out.lines)
        while out.launch is not None:
            out = await owner.completed(await out.launch.coroutine, now)
            lines.extend(out.lines)
        out_lines.append(lines)
    return out_lines


class TestTheShopSurveys:

    def test_the_script(self):
        graph = build_shop()
        for (after, tokens), expected in zip(SCRIPT, SHOP_AFTER_EACH_STEP, strict=True):
            put(graph, tokens)
            assert step(graph) == 1, after
            assert graph.last_fired == "Price Order", after
            assert marking(graph) == expected, after
        assert fireable(graph) == [] and step(graph) == 0, "then the net is quiescent"
        assert marking(graph, ("Orders",)) == {"Orders": []}

    def test_price_order_waits_for_an_order_whatever_promotions_hold(self):
        graph = build_shop()
        assert fireable(graph) == []
        for promotion in ("10% off", "Free shipping", "Members' day"):
            put(graph, {"Promotions": [promotion]})
            assert "Price Order" not in fireable(graph), "Orders is empty"
        put(graph, {"Orders": ["Order 1"]})
        assert fireable(graph) == ["Price Order"]

    def test_an_empty_promotions_does_not_hold_price_order_back(self):
        graph = build_shop()
        put(graph, {"Orders": ["Order 1"]})
        assert fireable(graph) == ["Price Order"], "Promotions is empty and does not hold it back"

    def test_the_survey_moves_nothing(self):
        graph = build_shop()
        put(graph, {"Promotions": ["10% off", "Free shipping"], "Orders": ["Order 2"]})
        promotions = ids(graph, "Promotions")
        (order,) = ids(graph, "Orders")
        graph.drain_events()
        step(graph)
        (priced,) = fired_events(graph)
        assert priced.consumed == (("Orders", order),), "a survey consumes nothing"
        assert [place for place, _ in priced.produced] == ["Priced", "Discounts Applied", "Discounts Applied"]
        assert ids(graph, "Promotions") == promotions, "the promotions stay, with their ids"

    def test_the_body_gets_a_list_oldest_first_possibly_empty(self):
        seen = []

        def record(order: str, promotions: list[str]) -> tuple[str, list[str]]:
            seen.append((order, promotions))
            return price_order(order, promotions)

        graph = ExecutableGraphOperations.construct_graph(shop_with(
            replacing("Price Order", FunctionTransitionNode("Price Order", record)),
        ))
        for _, tokens in SCRIPT:
            put(graph, tokens)
            step(graph)
        assert seen == [("Order 1", []), ("Order 2", ["10% off", "Free shipping"])]

    def test_a_survey_hands_over_a_copy(self):
        def scribble(order: str, notes: list[list]) -> str:
            for note in notes:
                note.append("scribbled")
            notes.clear()
            return order

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Orders", str, ["Order 1"]),
            ListPlaceNode("Notes", list, [["fragile"], ["gift"]]),
            ListPlaceNode("Priced", str),
            FunctionTransitionNode("Price Order", scribble),
            ArgumentEdgeToTransition("Orders", "Price Order", "order"),
            SurveyEdge("Notes", "Price Order", "notes"),
            ReturnedEdgeFromTransition("Price Order", "Priced"),
        ], record_events=True)
        before = ids(graph, "Notes")
        graph.drain_events()
        assert step(graph) == 1
        assert graph.place_named("Notes").tokens == [["fragile"], ["gift"]], "the place is untouched"
        assert ids(graph, "Notes") == before
        assert not [event for event in graph.drain_events() if isinstance(event, Mutated)]

    def test_a_guard_still_holds_it_back(self):
        graph = ExecutableGraphOperations.construct_graph(shop_with(
            replacing("Price Order", FunctionTransitionNode("Price Order", price_order, guard=lambda g: False)),
        ))
        put(graph, {"Orders": ["Order 1"]})
        assert fireable(graph) == [] and step(graph) == 0

    def test_the_concurrent_runner_runs_the_script_too(self):
        graph = build_shop()
        for _, tokens in SCRIPT:
            put(graph, tokens)
            asyncio.run(Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)))
        assert marking(graph) == SHOP_AFTER_EACH_STEP[-1]


class TestTheTrafficLightDrains:

    def test_the_transcripts_script(self):
        graph = build_traffic_light()
        for (transition, tokens), expected in zip(light_example.SCRIPT, LIGHT_AFTER_EACH_STEP, strict=True):
            light_example.put(graph, tokens)
            assert light_example.step(graph) == 1, transition
            assert graph.last_fired == transition
            assert light_example.marking(graph) == expected, transition

    def test_the_light_goes_round_by_itself(self):
        graph = build_traffic_light(record_events=True)
        graph.drain_events()
        seen = [light_example.colour(graph)]
        for _ in range(3):
            (light,) = ids(graph, "Light")
            assert light_example.step(graph) == 1
            (changed,) = fired_events(graph)
            assert changed.consumed == (("Light", light),), "the light alone"
            ((into, fresh),) = changed.produced
            assert into == "Light" and fresh != light, "the light goes back with a fresh token"
            seen.append(light_example.colour(graph))
        assert seen == ["red", "green", "amber", "red"]
        assert graph.fired_counts == {"Change": 3}, "Change alone, unguarded: nobody is waiting"

    def test_go_is_held_at_red_and_amber_and_with_nobody_waiting(self):
        graph = build_traffic_light()
        assert light_example.fireable(graph) == ["Change"], "red, and nobody waiting"
        light_example.step(graph)  # Change, to green
        assert light_example.fireable(graph) == ["Change"], \
            "green, but nobody waiting: the drain would accept the empty Waiting, and the guard holds Go"
        light_example.step(graph)  # Change, to amber
        light_example.put(graph, {"Waiting": ["Car 1"]})
        assert light_example.fireable(graph) == ["Change"], \
            "amber: the read has a colour to read, and the guard holds Car 1"
        light_example.step(graph)  # Change, to red
        assert light_example.fireable(graph) == ["Change"], "red: Car 1 waits for green"
        light_example.step(graph)  # Change, to green
        assert light_example.fireable(graph) == ["Go", "Change"]
        assert light_example.marking(graph, ("Waiting", "Passed")) == {"Waiting": ["Car 1"], "Passed": []}

    def test_at_green_with_cars_waiting_both_are_fireable_and_go_fires(self):
        def cars_at_green(graph):
            at_green(graph)
            light_example.put(graph, {"Waiting": ["Car 1", "Car 2"]})
            assert sorted(light_example.fireable(graph)) == ["Change", "Go"]
            light_example.step(graph)
            return graph.last_fired, light_example.marking(graph)

        graph = build_traffic_light()
        assert cars_at_green(graph) == (
            "Go", {"Light": ["green"], "Waiting": [], "Passed": ["Car 1", "Car 2"]},
        ), "Go is declared before Change, and the runner fires the first declared"
        assert light_example.fireable(graph) == ["Change"], "and then the light changes"
        built = build_traffic_light()
        go_node, change = (built.transition_named(name) for name in ("Go", "Change"))
        change_first = ExecutableGraphOperations.construct_graph(with_changes(
            built, (lambda c: c is go_node or c is change, [change, go_node]),
        ))
        assert cars_at_green(change_first) == (
            "Change", {"Light": ["amber"], "Waiting": ["Car 1", "Car 2"], "Passed": []},
        ), "declared the other way round, Change always wins at green"
        assert all(light_example.step(change_first) == 1 for _ in range(30))
        assert change_first.fired_counts == {"Change": 32}, "and the cars never go"

    def test_the_drain_takes_every_car_oldest_first(self):
        graph = at_green(build_traffic_light())
        light_example.put(graph, {"Waiting": ["Car 1", "Car 2", "Car 3"]})
        cars = ids(graph, "Waiting")
        graph.drain_events()
        assert light_example.step(graph) == 1 and graph.last_fired == "Go"
        (went,) = fired_events(graph)
        assert went.consumed == tuple(("Waiting", i) for i in cars), "every car, oldest first, and not the light"
        assert [place for place, _ in went.produced] == ["Passed", "Passed", "Passed"], "one token a car"
        assert light_example.marking(graph) == {
            "Light": ["green"], "Waiting": [], "Passed": ["Car 1", "Car 2", "Car 3"],
        }

    def test_go_leaves_the_lights_token_as_it_was(self):
        graph = at_green(build_traffic_light())
        (light,) = ids(graph, "Light")
        for car in ("Car 1", "Car 2"):
            light_example.put(graph, {"Waiting": [car]})
            assert light_example.step(graph) == 1 and graph.last_fired == "Go"
            assert ids(graph, "Light") == (light,), "the read leaves the light as it was, with its id"
        assert light_example.marking(graph, ("Light", "Passed")) == {"Light": ["green"], "Passed": ["Car 1", "Car 2"]}
        assert light_example.step(graph) == 1 and graph.last_fired == "Change"
        assert ids(graph, "Light") != (light,), "Change takes it and puts back a fresh one"

    def test_the_body_gets_every_car_oldest_first_and_the_colour(self):
        seen = []

        def record(cars: list[str], light: Colour) -> list[str]:
            seen.append((cars, light))
            return go(cars, light)

        graph = ExecutableGraphOperations.construct_graph(light_with(
            replacing("Go", FunctionTransitionNode("Go", record, guard=green_and_someone_waiting)),
        ))
        for _, tokens in light_example.SCRIPT:
            light_example.put(graph, tokens)
            light_example.step(graph)
        assert seen == [(["Car 1", "Car 2"], "green"), (["Car 3"], "green")]

    @pytest.mark.parametrize("guard", [None, is_green], ids=["no guard", "green alone"])
    def test_without_its_guard_go_fires_at_green_with_nobody_waiting_and_takes_nothing(self, guard):
        seen = []

        def record(cars: list[str], light: Colour) -> list[str]:
            seen.append(cars)
            return go(cars, light)

        graph = ExecutableGraphOperations.construct_graph(light_with(
            seeding("Light", ListPlaceNode("Light", Colour, [Colour("green")])),
            replacing("Go", FunctionTransitionNode("Go", record, guard=guard)),
        ), record_events=True)
        (light,) = ids(graph, "Light")
        assert light_example.fireable(graph) == ["Go", "Change"], "an empty Waiting does not hold the drain back"
        graph.drain_events()
        assert light_example.step(graph) == 1 and graph.last_fired == "Go"
        (went,) = fired_events(graph)
        assert went.consumed == () and went.produced == (), "the drain's zero case: nothing taken, nothing put"
        assert seen == [[]] and ids(graph, "Light") == (light,)
        assert all(light_example.step(graph) == 1 for _ in range(5))
        assert graph.fired_counts == {"Go": 6}, "Go, declared first, wins every step: the light sticks on green"
        assert light_example.marking(graph) == {"Light": ["green"], "Waiting": [], "Passed": []}
        with pytest.raises(ValueError, match="Go") as raised:
            ExecutableGraphCheck.assert_no_source_transitions(graph)
        assert "Change" not in str(raised.value), "a source: Go's only consuming edge is the drain"

    def test_the_read_alone_would_let_a_car_through_at_red(self):
        graph = ExecutableGraphOperations.construct_graph(light_with(
            replacing("Go", FunctionTransitionNode("Go", go)),
        ))
        light_example.put(graph, {"Waiting": ["Car 1"]})
        assert light_example.fireable(graph) == ["Go", "Change"], \
            "Light is never empty, so without its guard the read lets Car 1 through at red"
        assert light_example.step(graph) == 1 and graph.last_fired == "Go"
        assert light_example.marking(graph) == {"Light": ["red"], "Waiting": [], "Passed": ["Car 1"]}

    def test_the_light_never_stops(self):
        graph = build_traffic_light()
        assert all(light_example.step(graph) == 1 for _ in range(30))
        assert graph.fired_counts == {"Change": 30}
        assert light_example.marking(graph, ("Light",)) == {"Light": ["red"]}

    def test_a_guard_still_holds_the_drain_back(self):
        graph = ExecutableGraphOperations.construct_graph(light_with(
            replacing("Go", FunctionTransitionNode("Go", go, guard=lambda g: False)),
        ))
        light_example.put(at_green(graph), {"Waiting": ["Car 1"]})
        assert light_example.fireable(graph) == ["Change"]
        assert light_example.step(graph) == 1 and graph.last_fired == "Change"
        assert light_example.marking(graph, ("Waiting",)) == {"Waiting": ["Car 1"]}

    @pytest.mark.parametrize("cars", [["Car 1", "Car 2"], []])
    def test_a_failed_go_restores_the_cars_oldest_first_with_their_ids(self, cars):
        def fail(cars: list[str], light: Colour) -> list[str]:
            raise RuntimeError("the road is closed")

        # Guarded on green alone, so that it fires on an empty Waiting too.
        graph = ExecutableGraphOperations.construct_graph(light_with(
            replacing("Go", FunctionTransitionNode("Go", fail, guard=is_green)),
        ), restore_tokens_on_failure=True, record_events=True)
        light_example.put(at_green(graph), {"Waiting": cars})
        before = {place: ids(graph, place) for place in ("Light", "Waiting")}
        graph.drain_events()
        with pytest.raises(TransitionFailedError) as raised:
            light_example.step(graph)
        assert raised.value.restored and raised.value.consumed == {"cars": cars}, "the read consumed nothing"
        assert light_example.marking(graph, ("Light", "Waiting")) == {"Light": ["green"], "Waiting": cars}
        assert {place: ids(graph, place) for place in before} == before
        (failed,) = [event for event in graph.drain_events() if isinstance(event, Failed)]
        assert failed.consumed == tuple(("Waiting", i) for i in before["Waiting"])

    def test_the_concurrent_runner_runs_the_script_too(self):
        graph = build_traffic_light()
        for (transition, tokens), expected in zip(light_example.SCRIPT, LIGHT_AFTER_EACH_STEP, strict=True):
            light_example.put(graph, tokens)
            summary = asyncio.run(Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT),
                                             stop_after_n_firings=1))
            assert summary.fired == {transition: 1} and not summary.quiesced, transition
            assert light_example.marking(graph) == expected, transition


class TestBuildChecks:

    @pytest.mark.parametrize("edge", [DrainEdge, SurveyEdge])
    def test_a_plain_argument_is_refused(self, edge):
        def body(light: str, cars: str) -> None:
            return None

        with pytest.raises(TypeError, match=rf"{edge.__name__} .*'cars' must be annotated list\[str\]"):
            ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Light", str), ListPlaceNode("Waiting", str),
                FunctionTransitionNode("Go", body),
                ArgumentEdgeToTransition("Light", "Go", "light"),
                edge("Waiting", "Go", "cars"),
            ])

    def test_an_unannotated_argument_is_refused(self):
        with pytest.raises(TypeError, match="it has no annotation"):
            ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Light", str), ListPlaceNode("Waiting", str),
                FunctionTransitionNode("Go", lambda light, cars: None),
                ArgumentEdgeToTransition("Light", "Go", "light"),
                DrainEdge("Waiting", "Go", "cars"),
            ])

    def test_a_list_of_another_type_is_refused(self):
        def body(light: str, cars: list[int]) -> None:
            return None

        with pytest.raises(TypeError):
            ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Light", str), ListPlaceNode("Waiting", str),
                FunctionTransitionNode("Go", body),
                ArgumentEdgeToTransition("Light", "Go", "light"),
                DrainEdge("Waiting", "Go", "cars"),
            ])

    def test_a_place_of_lists_drains_into_a_list_of_lists(self):
        def body(baskets: list[list[str]]) -> int:
            return len(baskets)

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Baskets", list[str], [["a"], ["b", "c"]]),
            ListPlaceNode("Counted", int),
            FunctionTransitionNode("Count", body),
            DrainEdge("Baskets", "Count", "baskets"),
            ReturnedEdgeFromTransition("Count", "Counted"),
        ])
        step(graph)
        assert graph.place_named("Counted").tokens == [2] and graph.place_named("Baskets").tokens == []
        with pytest.raises(TypeError, match=r"list\[list\[str\]\]"):
            ExecutableGraphOperations.construct_graph([
                ListPlaceNode("Baskets", list[str]),
                FunctionTransitionNode("Count", lambda baskets: 0),
                DrainEdge("Baskets", "Count", "baskets"),
            ])

    def test_the_kinds_are_what_the_engine_files_them_as(self):
        shop_graph, light_graph = build_shop(), build_traffic_light()
        (survey,) = shop_graph.snapshot_edges
        assert isinstance(survey, SurveyEdge) and isinstance(survey, SnapshotEdge)
        assert survey.place_node_name == "Promotions"
        assert not ExecutableGraphCheck.requires_presence(survey)
        assert all(ExecutableGraphCheck.requires_presence(edge) for edge in shop_graph.argument_edges)

        (drain,) = [edge for edge in light_graph.argument_edges if isinstance(edge, DrainEdge)]
        (read,) = light_graph.snapshot_edges
        assert (drain.place_node_name, drain.transition_node_name) == ("Waiting", "Go")
        assert not ExecutableGraphCheck.requires_presence(drain)
        assert all(ExecutableGraphCheck.requires_presence(edge)
                   for edge in light_graph.argument_edges if edge is not drain)
        assert type(read) is SnapshotEdge and read.place_node_name == "Light"
        assert ExecutableGraphCheck.requires_presence(read), \
            "a plain read needs a token to read (Light always has one: the guard holds the cars back)"


class TestStaticChecks:

    def test_the_shop_has_no_source_and_no_cycle(self):
        graph = build_shop()
        ExecutableGraphCheck.assert_no_source_transitions(graph)
        ExecutableGraphCheck.assert_acyclic(graph)

    def test_the_traffic_lights_go_is_structurally_a_source_and_its_light_goes_round_a_cycle(self):
        # The check reads edges, not guards: Go's only consuming edge is its drain, so it is
        # named, guard and all (the example does not call the check; the unguarded Go above
        # shows what the guard prevents). Change takes the light, so it is not.
        graph = build_traffic_light()
        with pytest.raises(ValueError, match="Go") as raised:
            ExecutableGraphCheck.assert_no_source_transitions(graph)
        assert "Change" not in str(raised.value)
        with pytest.raises(ValueError, match="Light → Change → Light"):
            ExecutableGraphCheck.assert_acyclic(graph)

    def test_a_loop_closed_through_a_drain_is_not_a_cycle(self):
        # Built inline: the traffic light's own loop is a cycle of take edges, so it cannot show this.
        def open_gate(signal: str, queue: list[str]) -> list[str]:
            return queue

        def rejoin(item: str) -> str:
            return item

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Gate", str, ["open"]),
            ListPlaceNode("Queue", str, ["a", "b"]),
            ListPlaceNode("Through", str),
            FunctionTransitionNode("Open Gate", open_gate),
            ArgumentEdgeToTransition("Gate", "Open Gate", "signal"),
            DrainEdge("Queue", "Open Gate", "queue"),
            ReturnedEdgeFromTransition("Open Gate", "Through"),
            FunctionTransitionNode("Rejoin", rejoin),
            ArgumentEdgeToTransition("Through", "Rejoin", "item"),
            ReturnedEdgeFromTransition("Rejoin", "Queue"),
        ], expect_acyclic=True)
        ExecutableGraphCheck.assert_no_source_transitions(graph)
        _, fired = asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=None))
        assert fired == 3, "Open Gate once, Rejoin twice: every round costs a Gate token"
        assert marking(graph, ("Queue", "Through", "Gate")) == {"Queue": ["a", "b"], "Through": [], "Gate": []}

    def test_a_loop_closed_through_a_take_is_still_a_cycle(self):
        def rerun(order: str) -> str:
            return order

        with pytest.raises(ValueError, match="Orders → Price Order → Priced → Rerun → Orders"):
            ExecutableGraphOperations.construct_graph(shop_with((lambda c: False, [
                FunctionTransitionNode("Rerun", rerun),
                ArgumentEdgeToTransition("Priced", "Rerun", "order"),
                ReturnedEdgeFromTransition("Rerun", "Orders"),
            ])), expect_acyclic=True)


class TestOverTheWire:
    NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_the_shop_lists_its_survey_and_consumes_nothing_through_it(self):
        owner = Owner(Registry.from_factories([shop]))
        _, state, injected, stepped = await send_all(owner, self.NOW, [
            ("load", {"net": "shop"}),
            ("get_state", None),
            ("inject", {"place": "Orders", "token": "Order 1"}),
            ("step", None),
        ])
        inputs = {(e["place"], e["transition"]): e
                  for e in state[-1]["result"]["topology"]["edges"] if e["direction"] == "in"}
        assert inputs == {
            ("Orders", "Price Order"): {"place": "Orders", "transition": "Price Order", "direction": "in", "kind": "take"},
            ("Promotions", "Price Order"): {"place": "Promotions", "transition": "Price Order", "direction": "in",
                                            "kind": "survey"},
        }
        assert stepped[0]["method"] == "launched"
        assert stepped[0]["params"]["consumed"] == [["Orders", injected[-1]["result"]["id"]]], \
            "nothing from Promotions"

    @pytest.mark.asyncio
    async def test_the_traffic_light_lists_its_drain_and_its_read_and_consumes_nothing_through_the_read(self):
        owner = Owner(Registry.from_factories([traffic_light]))
        _, state, stepped, injected, gone = await send_all(owner, self.NOW, [
            ("load", {"net": "traffic-light"}),
            ("get_state", None),
            ("step", None),
            ("inject", {"place": "Waiting", "token": "Car 1"}),
            ("step", None),
        ])
        result = state[-1]["result"]
        inputs = {(e["place"], e["transition"]): e["kind"]
                  for e in result["topology"]["edges"] if e["direction"] == "in"}
        assert inputs == {
            ("Waiting", "Go"): "drain",
            ("Light", "Go"): "snapshot",
            ("Light", "Change"): "take",
        }
        (light,) = result["marking"]["places"]["Light"]["tokens"]
        assert stepped[0]["method"] == "launched" and stepped[0]["params"]["transition"] == "Change"
        assert stepped[0]["params"]["consumed"] == [["Light", light["id"]]], "the light alone"
        assert gone[0]["method"] == "launched" and gone[0]["params"]["transition"] == "Go"
        assert gone[0]["params"]["consumed"] == [["Waiting", injected[-1]["result"]["id"]]], \
            "the car alone: nothing from Light, which is read"


class TestTheTrafficLightsControls:
    """What the traffic light declares beside the net (PROTOCOL.md §5.1, §11): the light's
    ``Colour``, the controls ``car`` and ``change``, and the light's place style."""

    NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)

    def test_a_colour_is_a_string_with_the_lamps_hex(self):
        assert {name: Colour(name).color for name in light_example.COLOURS} == {
            "red": "#ff6347", "green": "#32cd32", "amber": "#ffa500",
        }
        assert Colour("red") == "red" and isinstance(Colour("red"), str)
        assert type(light_example.next_colour(Colour("amber"))) is Colour
        with pytest.raises(ValueError):
            Colour("blue")

    @pytest.mark.asyncio
    async def test_a_colour_round_trips_through_inject_and_get_token(self):
        owner = Owner(Registry.from_factories([traffic_light]))
        _, injected, blue = await send_all(owner, self.NOW, [
            ("load", {"net": "traffic-light"}),
            ("inject", {"place": "Light", "token": "green"}),
            ("inject", {"place": "Light", "token": "blue"}),
        ])
        assert injected[0]["method"] == "added" and injected[0]["params"]["color"] == "#32cd32"
        assert blue[-1]["error"]["code"] == "invalid_token"
        light = owner._loaded.outer.graph.place_named("Light").tokens
        assert type(light[-1]) is Colour and light == ["red", "green"]
        (token,) = await send_all(owner, self.NOW, [("get_token", {"id": injected[-1]["result"]["id"]})])
        assert token[-1]["result"]["value"] == "green" and token[-1]["result"]["kind"] == "json"

    @pytest.mark.asyncio
    async def test_the_two_controls_and_the_style_in_the_topology(self):
        owner = Owner(Registry.from_factories([traffic_light]))
        _, state = await send_all(owner, self.NOW, [("load", {"net": "traffic-light"}), ("get_state", None)])
        result = state[-1]["result"]
        assert result["topology"]["controls"] == [
            {"name": "car", "label": "+ Car", "kind": "text", "node": "Waiting", "action": "inject",
             "config": {"placeholder": "name"}},
            {"name": "change", "label": "Change", "kind": "button", "node": "Change", "action": "fire"},
        ]
        assert [place.get("style") for place in result["topology"]["places"]] == [{"fill": "token"}, None, None]
        (light,) = result["marking"]["places"]["Light"]["tokens"]
        assert light["color"] == "#ff6347"

    def test_an_empty_name_numbers_the_car_from_the_marking(self):
        graph = build_traffic_light()
        car = light_example.CONTROLS["car"]
        assert ControlSpec.command(car, "", graph) == Extend("Waiting", ["Car 1"])
        light_example.put(graph, {"Waiting": ["Car 1"], "Passed": ["Bus", "Car 2"]})
        assert ControlSpec.command(car, "", graph) == Extend("Waiting", ["Car 4"])
        assert ControlSpec.command(car, "Taxi", graph) == Extend("Waiting", ["Taxi"])

    @pytest.mark.asyncio
    async def test_pressing_car_adds_a_car_with_no_colour(self):
        owner = Owner(Registry.from_factories([traffic_light]))
        _, first, named = await send_all(owner, self.NOW, [
            ("load", {"net": "traffic-light"}),
            ("press", {"control": "car", "value": ""}),
            ("press", {"control": "car", "value": "Taxi"}),
        ])
        assert [line.get("method") for line in first] == ["added", None]
        assert "color" not in first[0]["params"] and first[-1]["result"]["status"] == "pressed"
        assert named[-1]["result"]["status"] == "pressed"
        assert light_example.marking(owner._loaded.outer.graph)["Waiting"] == ["Car 1", "Taxi"]

    @pytest.mark.asyncio
    async def test_pressing_change_fires_change_unless_it_is_switched_off(self):
        owner = Owner(Registry.from_factories([traffic_light]))
        _, changed, _, held = await send_all(owner, self.NOW, [
            ("load", {"net": "traffic-light"}),
            ("press", {"control": "change"}),
            ("disable", {"transition": "Change"}),
            ("press", {"control": "change"}),
        ])
        assert [line.get("method") for line in changed] == ["launched", "distributed", None]
        assert changed[1]["params"]["produced"][0][3] == "#32cd32", "the new light carries its colour"
        assert changed[-1]["result"]["status"] == "pressed"
        assert held[-1]["result"]["status"] == "nothing" and held[-1]["result"]["reason"] == "Change is not fireable"
        assert light_example.colour(owner._loaded.outer.graph) == "green"


class TestTheVisualiser:

    @staticmethod
    def drawn_inputs(graph):
        pytest.importorskip("rustworkx")
        from petritype.plotting.rustworkx_graph import RustworkxGraph
        from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
        _, edge_attributes = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
        drawn = {}
        for edge in RustworkxGraph.from_executable_graph(graph).edges():
            place = getattr(edge, "source_place_node_name", None)
            transition = getattr(edge, "target_transition_node_name", None)
            if place is not None and transition is not None:
                drawn[(place, transition)] = edge_attributes(edge)
        return drawn

    def test_the_survey_is_dashed_with_a_hollow_head(self):
        drawn = self.drawn_inputs(build_shop())
        assert drawn[("Promotions", "Price Order")] == {
            "style": "dashed", "color": "dimgray", "arrowhead": "empty", "penwidth": "1",
        }
        assert "arrowhead" not in drawn[("Orders", "Price Order")], "a plain take keeps its plain head"

    def test_the_drain_is_solid_and_hollow_and_the_plain_read_dashed_with_a_plain_head(self):
        drawn = self.drawn_inputs(build_traffic_light())
        assert drawn[("Waiting", "Go")] == {"color": "black", "penwidth": "1", "arrowhead": "empty"}
        assert drawn[("Light", "Go")] == {
            "style": "dashed", "color": "dimgray", "arrowhead": "normal", "penwidth": "1",
        }
        assert "arrowhead" not in drawn[("Light", "Change")], "a plain take keeps its plain head"
