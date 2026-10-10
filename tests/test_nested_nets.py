"""Nets as tokens (``NETS_AS_TOKENS.md``, Python track phase 1: R1 to R4, R7, R10).

The transit example (``examples/tokens_as_nets/transit.py``) is the acceptance net; section 6 of
the design document gives the expected results pinned here. Outer transitions the
design's script fires by hand are held back with ``RunContext.disabled`` while
the net runs to quiet, so the driver's own order (outer first, then inner nets in
place, token and net order) reproduces the script.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.tokens_as_nets.transit import (  # noqa: E402
    HELD_BACK, Vehicle, acceptance_script, build_cabin, build_city, fire, run_to_quiet,
)
from petritype.core.events import Added, Failed, Fired, Mutated, Nested, ParamChanged, Removed  # noqa: E402
from petritype.core.executable_graph_components import (  # noqa: E402
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    NestedNets,
    NetParams,
    ReturnedEdgeFromTransition,
    TransitionFailedError,
)
from petritype.runtime import ExecutionMode, Extend, RunContext, Runner, SetNetParam  # noqa: E402


def run(coroutine):
    return asyncio.run(coroutine)


def quiet(graph: ExecutableGraph, holding_back=HELD_BACK):
    return run(run_to_quiet(graph, holding_back))


def tokens(graph: ExecutableGraph, place: str) -> list:
    return list(graph.place_named(place).tokens)


def firings(events) -> list[tuple[tuple[str, ...], str]]:
    """``(path, transition)`` for every firing in the log, outer ones with an empty path."""
    out = []
    for event in events:
        if isinstance(event, Fired):
            out.append(((), event.transition))
        elif isinstance(event, Nested) and isinstance(event.event, Fired):
            out.append((event.path, event.event.transition))
    return out


# --- small nets --------------------------------------------------------------------------


def step(x: int) -> int:
    return x + 1


def build_counter(start: list[int], template: str = "Counter") -> ExecutableGraph:
    """``Low -Up-> High``: one firing per seed token."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Low", int, start),
        ArgumentEdgeToTransition("Low", "Up", "x"),
        FunctionTransitionNode("Up", step),
        ReturnedEdgeFromTransition("Up", "High"),
        ListPlaceNode("High", int),
    ], params={"limit": 10}, template=template)


class Box(BaseModel):
    """A token holding one named inner net."""

    label: str
    inner: ExecutableGraph

    def __petritype_nets__(self) -> dict[str, ExecutableGraph]:
        return {"counter": self.inner}


class Crate(BaseModel):
    """A token holding two named inner nets."""

    left: ExecutableGraph
    right: ExecutableGraph

    def __petritype_nets__(self) -> dict[str, ExecutableGraph]:
        return {"left": self.left, "right": self.right}


def build_shelf(boxes: list[Box], passive: bool = False, record_events: bool = True, **options) -> ExecutableGraph:
    """An outer net with nothing of its own to fire: ``Shelf`` holds boxes."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Shelf", Box, boxes, nested=True, passive=passive),
    ], record_events=record_events, **options)


# --- R1: declaring nested tokens ------------------------------------------------------------


class TestDeclaringNestedTokens:

    def test_a_vehicle_place_builds_and_its_inner_net_is_found(self):
        city = build_city()
        bus = tokens(city, "Depot")[0]
        assert NestedNets.of(bus) == {"cabin": bus.cabin}
        assert bus.cabin.template == "Cabin"

    def test_a_graph_is_its_own_unnamed_inner_net(self):
        inner = build_counter([1])
        assert NestedNets.of(inner) == {"": inner}
        outer = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Nets", ExecutableGraph, [inner], nested=True),
        ])
        assert [path for path, *_ in NestedNets.walk(outer)] == [("Nets", "#1", "")]

    def test_a_token_without_the_protocol_is_refused_at_seed_time(self):
        with pytest.raises(TypeError, match="nested but its token cannot hold nets"):
            ListPlaceNode("Shelf", int, [1], nested=True)

    def test_a_token_without_the_protocol_is_refused_at_deposit_time(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Seed", int, [1]),
            ArgumentEdgeToTransition("Seed", "Make", "x"),
            FunctionTransitionNode("Make", lambda x: object()),
            ReturnedEdgeFromTransition("Make", "Shelf"),
            ListPlaceNode("Shelf", object, nested=True),
        ])
        # A deposit that does not fit its place propagates as the original error
        # (the engine's stage-3 contract), after recording the firing as Failed.
        with pytest.raises(TypeError, match="cannot hold nets"):
            run(ExecutableGraphOperations.execute_graph(graph))
        assert tokens(graph, "Shelf") == []

    def test_a_token_without_the_protocol_is_refused_by_extend(self):
        graph = ExecutableGraphOperations.construct_graph([ListPlaceNode("Shelf", object, nested=True)])
        ctx = RunContext(graph=graph, inbox=asyncio.Queue(), strict=True)
        ctx.inbox.put_nowait(Extend("Shelf", [object()]))
        with pytest.raises(ValueError, match="cannot hold nets"):
            Runner.drain_inbox(ctx)
        assert tokens(graph, "Shelf") == []

    def test_a_bad_protocol_result_is_refused(self):
        class Liar:
            def __petritype_nets__(self):
                return {"n": 1}
        with pytest.raises(TypeError, match="dict of str to ExecutableGraph"):
            ListPlaceNode("Shelf", Liar, [Liar()], nested=True)

    def test_passive_needs_nested(self):
        with pytest.raises(ValueError, match="passive but not nested"):
            ListPlaceNode("Shelf", Box, passive=True)

    def test_every_graph_has_its_own_net_id(self):
        a, b = build_counter([]), build_counter([])
        assert a.net_id != b.net_id and a.net_id > 0


# --- R2: running inner nets ------------------------------------------------------------------


class TestRunningInnerNets:

    def test_execute_graph_runs_inner_nets_to_quiet(self):
        shelf = build_shelf([Box(label="a", inner=build_counter([1, 2])), Box(label="b", inner=build_counter([3]))])
        _, fired = run(ExecutableGraphOperations.execute_graph(shelf, stop_after_n_firings=None))
        assert fired == 3
        a, b = tokens(shelf, "Shelf")
        assert tokens(a.inner, "High") == [2, 3] and tokens(b.inner, "High") == [4]
        assert a.inner.fired_counts == {"Up": 2} and a.inner.step_count == 2
        # The outer net counts inner firings under their path.
        assert shelf.fired_counts == {"Shelf/#1/counter/Up": 2, "Shelf/#2/counter/Up": 1}
        assert shelf.step_count == 3

    def test_order_is_outer_first_then_place_token_net_order_repeated(self):
        crate = Crate(left=build_counter([1]), right=build_counter([2, 3]))
        shelf = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Seed", int, [0]),
            ArgumentEdgeToTransition("Seed", "Own", "x"),
            FunctionTransitionNode("Own", step),
            ReturnedEdgeFromTransition("Own", "Done"),
            ListPlaceNode("Done", int),
            ListPlaceNode("Shelf", Box, [Box(label="a", inner=build_counter([1, 2]))], nested=True),
            ListPlaceNode("Crates", Crate, [crate], nested=True),
        ], record_events=True)
        run(ExecutableGraphOperations.execute_graph(shelf, stop_after_n_firings=None))
        assert firings(shelf.drain_events()) == [
            ((), "Own"),
            (("Shelf", "#2", "counter"), "Up"),
            (("Shelf", "#2", "counter"), "Up"),
            (("Crates", "#3", "left"), "Up"),
            (("Crates", "#3", "right"), "Up"),
            (("Crates", "#3", "right"), "Up"),
        ]

    def test_the_runner_counts_inner_firings_and_quiesces_on_them(self):
        shelf = build_shelf([Box(label="a", inner=build_counter([1, 2]))])
        summary = run(Runner.run(RunContext(graph=shelf)))
        assert summary.quiesced and summary.n_fired == 2
        assert summary.fired == {"Shelf/#1/counter/Up": 2}

    def test_the_runner_paces_inner_firings(self):
        shelf = build_shelf([Box(label="a", inner=build_counter([1, 2, 3]))])
        ctx = RunContext(graph=shelf)
        first = run(Runner.run(ctx, stop_after_n_firings=2))
        assert not first.quiesced and first.n_fired == 2
        second = run(Runner.run(ctx))
        assert second.quiesced and second.n_fired == 1

    def test_concurrent_mode_runs_inner_nets_too(self):
        shelf = build_shelf([Box(label="a", inner=build_counter([1, 2]))])
        summary = run(Runner.run(RunContext(graph=shelf, mode=ExecutionMode.CONCURRENT)))
        assert summary.quiesced and summary.n_fired == 2
        assert tokens(tokens(shelf, "Shelf")[0].inner, "High") == [2, 3]

    def test_a_passive_place_leaves_its_nets_unfired_until_a_body_steps_them(self):
        async def open_box(box: Box) -> Box:
            await ExecutableGraphOperations.execute_graph(box.inner, stop_after_n_firings=None)
            return box

        shelf = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Shelf", Box, [Box(label="a", inner=build_counter([1, 2]))], nested=True, passive=True),
            ArgumentEdgeToTransition("Shelf", "Open", "box"),
            FunctionTransitionNode("Open", open_box, guard=lambda g: g.params["open"]),
            ReturnedEdgeFromTransition("Open", "Opened"),
            ListPlaceNode("Opened", Box, nested=True, passive=True),
        ], params={"open": False}, record_events=True)
        _, fired = run(ExecutableGraphOperations.execute_graph(shelf, stop_after_n_firings=None))
        assert fired == 0
        assert tokens(tokens(shelf, "Shelf")[0].inner, "High") == []
        NetParams.set(shelf, "open", True)
        _, fired = run(ExecutableGraphOperations.execute_graph(shelf, stop_after_n_firings=None))
        assert fired == 1
        box = tokens(shelf, "Opened")[0]
        assert tokens(box.inner, "High") == [2, 3]
        # The body's inner firings reached the outer log, under the box's path at the time.
        assert firings(shelf.drain_events()) == [
            (("Opened", "#2", "counter"), "Up"), (("Opened", "#2", "counter"), "Up"), ((), "Open"),
        ]

    def test_the_acceptance_steps_two_and_four_reach_quiet_with_the_inner_firings(self):
        city = build_city()
        for _ in range(3):
            run(fire(city, "Board Depot"))
        quiet(city)
        bus = tokens(city, "Depot")[0]
        assert bus.cabin.fired_counts == {"Sit": 2, "Stand": 1}
        assert tokens(city, "Platform@Depot") == []
        assert (tokens(bus.cabin, "Seats"), tokens(bus.cabin, "Standing"), tokens(bus.cabin, "Door")) == (
            ["Ada", "Bram"], ["Cato"], [],
        )
        run(fire(city, "Depart Depot"))
        before = dict(bus.cabin.fired_counts)
        quiet(city)
        assert bus.cabin.fired_counts == before, "nothing inside can move while not stopped"
        run(fire(city, "Arrive"))
        quiet(city)
        assert tokens(city, "Central") == [bus]
        assert bus.cabin.fired_counts == {"Sit": 2, "Stand": 1, "Go To Door": 2, "Go To Door From Standing": 1}
        assert tokens(bus.cabin, "Door") == ["Ada", "Bram", "Cato"]
        # The announcement stands until the last passenger has left (see build_cabin).
        assert tokens(bus.cabin, "Announcements") == ["Arrived at Central"]


# --- R3: reaching inside -------------------------------------------------------------------


class TestReachingInside:

    def test_board_alight_depart_arrive(self):
        city = build_city()
        run(acceptance_script(city))
        bus = tokens(city, "Central")[0]
        assert bus.name == "Bus 1" and bus.destination is None
        assert tokens(city, "Platform@Central") == ["Ada", "Bram"]
        assert tokens(bus.cabin, "Door") == ["Cato"]
        assert tokens(city, "Depot") == [] and tokens(city, "In Transit") == []
        assert bus.cabin.params["stopped"] is True
        ferry = tokens(city, "Harbour")[0]
        assert ferry.cabin.fired_counts == {}

    def test_a_body_receives_a_net_parameter_by_name(self):
        city = build_city()
        NetParams.set(city, "destination", "Airport")
        run(fire(city, "Depart Depot"))
        assert tokens(city, "In Transit")[0].destination == "Airport"
        run(fire(city, "Arrive"))
        assert [v.name for v in tokens(city, "Airport")] == ["Bus 1"]

    def test_net_params_are_declared_not_a_free_store(self):
        city = build_city()
        with pytest.raises(KeyError):
            NetParams.set(city, "speed", 3)
        city.drain_events()
        NetParams.set(city, "destination", "Harbour")
        assert city.drain_events() == [ParamChanged(None, "destination")]

    def test_set_net_param_command(self):
        city = build_city()
        ctx = RunContext(graph=city, inbox=asyncio.Queue(), strict=True)
        ctx.inbox.put_nowait(SetNetParam("destination", "Harbour"))
        assert Runner.drain_inbox(ctx) == 1
        assert city.params["destination"] == "Harbour"
        ctx.inbox.put_nowait(SetNetParam("speed", 1))
        with pytest.raises(ValueError, match="declares no parameter"):
            Runner.drain_inbox(ctx)

    def test_a_failing_board_leaves_the_passenger_on_the_platform(self):
        def board_then_fail(passenger: str, vehicle: Vehicle) -> None:
            NestedNets.put(vehicle.cabin, "Door", [passenger])
            raise RuntimeError("doors jammed")

        city = build_city(restore_tokens_on_failure=True)
        city.transition_named("Board Depot").function = board_then_fail
        with pytest.raises(TransitionFailedError) as caught:
            run(fire(city, "Board Depot"))
        assert caught.value.restored
        assert tokens(city, "Platform@Depot") == ["Ada", "Bram", "Cato"]
        # Conservation, not rollback: what the body did inside the cabin stays done.
        bus = tokens(city, "Depot")[0]
        assert tokens(bus.cabin, "Door") == ["Ada"]
        events = city.drain_events()
        assert isinstance(events[-1], Failed) and events[-1].restored
        assert Nested(("Depot", "#1", "cabin"), bus.cabin.net_id, Added("Door", 1)) in events

    def test_put_and_take_check_types_and_record_events(self):
        cabin = build_cabin()
        cabin.record_events = True
        with pytest.raises(TypeError):
            NestedNets.put(cabin, "Door", [42])
        with pytest.raises(KeyError):
            NestedNets.put(cabin, "Hold", ["Ada"])
        assert NestedNets.put(cabin, "Door", ["Ada", "Bram"]) == [1, 2]
        with pytest.raises(ValueError, match="holds 2 tokens, not 3"):
            NestedNets.take(cabin, "Door", 3)
        assert NestedNets.take(cabin, "Door") == ["Ada"]
        assert tokens(cabin, "Door") == ["Bram"] and cabin.place_named("Door").token_ids == (2,)
        assert cabin.drain_events() == [Added("Door", 1), Added("Door", 2), Removed("Door", 1)]

    def test_a_sink_transition_fires_and_deposits_nothing(self):
        seen = []
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [1, 2]),
            ArgumentEdgeToTransition("In", "Drop", "x"),
            FunctionTransitionNode("Drop", lambda x: seen.append(x)),
        ], record_events=True)
        _, fired = run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=None))
        assert fired == 2 and seen == [1, 2] and tokens(graph, "In") == []
        assert [e.produced for e in graph.drain_events() if isinstance(e, Fired)] == [(), ()]

    def test_a_sink_transition_must_return_none(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("In", int, [1]),
            ArgumentEdgeToTransition("In", "Drop", "x"),
            FunctionTransitionNode("Drop", lambda x: x),
        ])
        with pytest.raises(TransitionFailedError) as caught:
            run(ExecutableGraphOperations.execute_graph(graph))
        assert "must return None" in str(caught.value.__cause__)


# --- R4 and R7: observability through the boundary -----------------------------------------


class TestObservability:

    def test_the_log_names_each_inner_firing_under_the_place_the_bus_stood_in(self):
        city = build_city()
        events = run(acceptance_script(city))
        assert firings(events) == [
            ((), "Board Depot"), ((), "Board Depot"), ((), "Board Depot"),
            (("Depot", "#1", "cabin"), "Sit"),
            (("Depot", "#1", "cabin"), "Sit"),
            (("Depot", "#1", "cabin"), "Stand"),
            ((), "Depart Depot"),
            ((), "Arrive"),
            (("Central", "#7", "cabin"), "Go To Door"),
            (("Central", "#7", "cabin"), "Go To Door"),
            (("Central", "#7", "cabin"), "Go To Door From Standing"),
            ((), "Alight Central"), ((), "Alight Central"),
        ]

    def test_what_a_body_did_inside_comes_before_its_firing(self):
        city = build_city()
        run(fire(city, "Board Depot"))
        bus = tokens(city, "Depot")[0]
        inside, mutated, fired = city.drain_events()
        assert inside == Nested(("Depot", "#1", "cabin"), bus.cabin.net_id, Added("Door", 1))
        assert mutated == Mutated("Depot", 1)
        assert isinstance(fired, Fired)
        assert (fired.transition, fired.consumed, fired.produced) == ("Board Depot", (("Platform@Depot", 3),), ())

    def test_the_bus_keeps_its_nested_id_from_depot_to_central(self):
        city = build_city()
        bus = tokens(city, "Depot")[0]
        events = run(acceptance_script(city))
        nested = [e for e in events if isinstance(e, Nested)]
        assert nested and {e.nested_id for e in nested} == {bus.cabin.net_id}
        assert {e.path for e in nested} == {
            ("Depot", "#1", "cabin"), ("In Transit", "#6", "cabin"), ("Central", "#7", "cabin"),
        }
        assert NestedNets.find(city, bus.cabin.net_id) == (("Central", "#7", "cabin"), bus.cabin)
        assert NestedNets.resolve(city, ("Central", "#7", "cabin")) is bus.cabin
        with pytest.raises(LookupError, match="no token #1"):
            NestedNets.resolve(city, ("Depot", "#1", "cabin"))

    def test_inner_events_are_wrapped_once_whatever_the_depth(self):
        inner = build_counter([1])
        middle = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Shelf", Box, [Box(label="m", inner=inner)], nested=True),
        ])
        outer = build_shelf([Box(label="o", inner=middle)])
        run(ExecutableGraphOperations.execute_graph(outer, stop_after_n_firings=None))
        assert outer.last_fired == "Shelf/#1/counter/Shelf/#1/counter/Up"
        events = outer.drain_events()
        fired = [e for e in events if isinstance(e, Nested) and isinstance(e.event, Fired)]
        assert len(fired) == 1
        assert fired[0].path == ("Shelf", "#1", "counter", "Shelf", "#1", "counter")
        assert fired[0].nested_id == inner.net_id
        assert not any(isinstance(e.event, Nested) for e in events if isinstance(e, Nested))

    def test_inner_events_are_kept_when_the_token_is_consumed(self):
        """R7: the firing that drops a nested token drains its events first."""
        def crush(box: Box) -> None:
            return None

        shelf = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Shelf", Box, [Box(label="a", inner=build_counter([1]))], nested=True),
            ArgumentEdgeToTransition("Shelf", "Crush", "box"),
            FunctionTransitionNode("Crush", crush, guard=lambda g: g.params["crush"]),
        ], params={"crush": False}, record_events=True)
        box = tokens(shelf, "Shelf")[0]
        run(ExecutableGraphOperations.execute_graph(shelf, stop_after_n_firings=None))  # the inner Up fires
        assert box.inner.fired_counts == {"Up": 1}
        NetParams.set(shelf, "crush", True)
        NestedNets.put(box.inner, "Low", [5])  # an undrained inner event right before the crush
        run(ExecutableGraphOperations.execute_graph(shelf, stop_after_n_firings=None))
        assert tokens(shelf, "Shelf") == []
        events = shelf.drain_events()
        assert Nested(("Shelf", "#1", "counter"), box.inner.net_id, Added("Low", 3)) in events
        assert [e.transition for e in events if isinstance(e, Fired)] == ["Crush"]

    def test_inner_nets_record_exactly_when_the_outer_does(self):
        shelf = build_shelf([Box(label="a", inner=build_counter([1]))], record_events=False)
        run(ExecutableGraphOperations.execute_graph(shelf, stop_after_n_firings=None))
        assert shelf.drain_events() == []
        box = tokens(shelf, "Shelf")[0]
        assert box.inner.record_events is False and box.inner.drain_events() == []

    def test_drain_events_gathers_what_inner_nets_recorded_on_their_own(self):
        shelf = build_shelf([Box(label="a", inner=build_counter([]))])
        box = tokens(shelf, "Shelf")[0]
        NestedNets.put(box.inner, "Low", [7])
        assert shelf.drain_events() == [Nested(("Shelf", "#1", "counter"), box.inner.net_id, Added("Low", 1))]
        assert shelf.drain_events() == []


# --- the visualiser: marking tokens that hold nets ------------------------------------------


class TestSummary:

    def test_a_nested_token_is_summarised_by_its_fields_and_inner_counts(self):
        city = build_city()
        bus = tokens(city, "Depot")[0]
        NestedNets.put(bus.cabin, "Door", ["Ada", "Bram"])
        assert NestedNets.summary(bus) == "Vehicle(name='Bus 1', kind='Bus', destination=None) \u2282 cabin: 2"
        assert NestedNets.summary(bus, maximum_length=20) == "Vehicle(name='Bus 1'..."
        assert NestedNets.summary(build_counter([1, 2])) == "net: 2"
        assert NestedNets.summary(Crate(left=build_counter([1]), right=build_counter([]))) == "Crate() \u2282 left: 1, right: 0"
        assert NestedNets.holds_nets(bus) and not NestedNets.holds_nets("Ada")

    def test_the_graphviz_label_marks_nested_tokens(self):
        pytest.importorskip("rustworkx")
        from petritype.plotting.rustworkx_to_graphviz import NodeLabel
        city = build_city()
        bus = tokens(city, "Depot")[0]
        label = NodeLabel.default_place(city.place_named("Depot"))
        assert label.splitlines()[2] == "Vehicle(name='Bus 1', kind='Bus', destination=None) \u2282 cabin: 0"
        assert NodeLabel.default_token_summariser("Ada") == "Ada"
        assert NodeLabel.inner_nets(bus) == {"cabin": bus.cabin}


# --- R10: depth two ---------------------------------------------------------------------


class Passenger(BaseModel):
    name: str
    luggage: ExecutableGraph

    def __petritype_nets__(self) -> dict[str, ExecutableGraph]:
        return {"luggage": self.luggage}


class Bus(BaseModel):
    cabin: ExecutableGraph

    def __petritype_nets__(self) -> dict[str, ExecutableGraph]:
        return {"cabin": self.cabin}


def unpack(item: str) -> str:
    return item


class TestDepthTwo:

    def build(self):
        luggage = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Packed", str, ["socks", "book"]),
            ArgumentEdgeToTransition("Packed", "Unpack", "item"),
            FunctionTransitionNode("Unpack", unpack),
            ReturnedEdgeFromTransition("Unpack", "Unpacked"),
            ListPlaceNode("Unpacked", str),
        ])
        cabin = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Door", Passenger, [Passenger(name="Ada", luggage=luggage)], nested=True),
        ])
        return ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Station", Bus, [Bus(cabin=cabin)], nested=True),
        ], record_events=True), luggage

    def test_a_passenger_carrying_luggage_unpacks_two_levels_down(self):
        station, luggage = self.build()
        _, fired = run(ExecutableGraphOperations.execute_graph(station, stop_after_n_firings=None))
        assert fired == 2 and tokens(luggage, "Unpacked") == ["socks", "book"]
        assert station.fired_counts == {"Station/#1/cabin/Door/#1/luggage/Unpack": 2}
        events = [e for e in station.drain_events() if isinstance(e, Nested)]
        assert {e.path for e in events} == {("Station", "#1", "cabin", "Door", "#1", "luggage")}
        assert {e.nested_id for e in events} == {luggage.net_id}
        assert NestedNets.resolve(station, ("Station", "#1", "cabin", "Door", "#1", "luggage")) is luggage
        assert NestedNets.find(station, luggage.net_id)[0] == ("Station", "#1", "cabin", "Door", "#1", "luggage")

    def test_resolve_names_the_failing_segment(self):
        station, _ = self.build()
        with pytest.raises(LookupError, match="no nested place 'Lobby'"):
            NestedNets.resolve(station, ("Lobby", "#1", "cabin"))
        with pytest.raises(LookupError, match="holds no net named 'boot'"):
            NestedNets.resolve(station, ("Station", "#1", "boot"))
        with pytest.raises(LookupError, match="three segments per level"):
            NestedNets.resolve(station, ("Station", "#1"))
        assert NestedNets.find(station, 10 ** 9) is None
