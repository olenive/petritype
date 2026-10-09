"""Transit: vehicles whose cabins are Petri nets of their own (nets as tokens).

The example of ``NETS_AS_TOKENS.md`` §2, and the conformance net ``transit`` of
PROTOCOL.md §11. An outer net, the city, holds vehicle tokens standing at
locations or in transit; each vehicle holds an inner net, its cabin, whose
structure depends on the kind of vehicle (a bus seats and stands its passengers,
a ferry has two decks, a shuttle only seats). The outer net never shares a place
with a cabin: it reaches inside only through the vehicle tokens its bodies hold,
putting passengers in at the door, taking them out, setting the cabin's
``stopped`` parameter and posting an announcement. Passengers are plain strings.

Run it to see the acceptance script of the design document, with the outer
net's event log showing the inner firings under the path of the vehicle they
happened in::

    uv run python examples/transit.py
"""

from __future__ import annotations

import asyncio
from typing import Literal, Optional

from pydantic import BaseModel

from petritype import petri_net
from petritype.core.events import Fired, Nested
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReturnedEdgeFromTransition,
)
from petritype.core.nested import NestedNets
from petritype.runtime import RunContext, Runner

LOCATIONS = ("Depot", "Central", "Harbour", "Airport")
Kind = Literal["Bus", "Tram", "Ferry", "Shuttle"]


class Vehicle(BaseModel):
    """A vehicle token: its cabin is an inner net, named ``cabin`` whatever the kind."""

    name: str
    kind: Kind
    destination: Optional[str] = None
    cabin: ExecutableGraph

    def __petritype_nets__(self) -> dict[str, ExecutableGraph]:
        return {"cabin": self.cabin}


# --- the inner nets: one template per kind -------------------------------------------------


def passenger_moves(passenger: str) -> str:
    return passenger


def discard(announcement: str) -> None:
    return None


def _count(graph: ExecutableGraph, place: str) -> int:
    return len(graph.place_named(place).tokens)


def _stopped_and_announced(graph: ExecutableGraph) -> bool:
    return bool(graph.params["stopped"]) and _count(graph, "Announcements") > 0


def _no_announcement(graph: ExecutableGraph) -> bool:
    return _count(graph, "Announcements") == 0


def _nobody_aboard(*places: str):
    def guard(graph: ExecutableGraph) -> bool:
        return all(_count(graph, place) == 0 for place in places)
    guard.__name__ = "nobody_aboard"
    return guard


def build_cabin(seats: int = 2, capacity: int = 6) -> ExecutableGraph:
    """``Cabin`` (Bus, Tram): passengers at the door sit while there are seats,
    else stand; when the vehicle is stopped and an arrival has been announced
    they go to the door, and nobody sits or stands again while the announcement
    stands (they are leaving, not boarding); the announcement is cleared once
    nobody is aboard at all, the door included.

    The design document has ``Sit`` guarded by seats alone and the announcement
    cleared once nobody is seated or standing; built that way the cabin re-seats
    the passengers at the door the moment the announcement clears, and can never
    hold them at the door, so these two guards differ from it."""

    def seat_free(graph: ExecutableGraph) -> bool:
        return _count(graph, "Seats") < graph.params["seats"] and _no_announcement(graph)

    def no_seat_free(graph: ExecutableGraph) -> bool:
        return _count(graph, "Seats") >= graph.params["seats"] and _no_announcement(graph)

    nobody_aboard = _nobody_aboard("Door", "Seats", "Standing")

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", str),
        ListPlaceNode("Seats", str),
        ListPlaceNode("Standing", str),
        ListPlaceNode("Announcements", str),
        FunctionTransitionNode("Sit", passenger_moves, guard=seat_free),
        ArgumentEdgeToTransition("Door", "Sit", "passenger"),
        ReturnedEdgeFromTransition("Sit", "Seats"),
        FunctionTransitionNode("Stand", passenger_moves, guard=no_seat_free),
        ArgumentEdgeToTransition("Door", "Stand", "passenger"),
        ReturnedEdgeFromTransition("Stand", "Standing"),
        FunctionTransitionNode("Go To Door", passenger_moves, guard=_stopped_and_announced),
        ArgumentEdgeToTransition("Seats", "Go To Door", "passenger"),
        ReturnedEdgeFromTransition("Go To Door", "Door"),
        FunctionTransitionNode("Go To Door From Standing", passenger_moves, guard=_stopped_and_announced),
        ArgumentEdgeToTransition("Standing", "Go To Door From Standing", "passenger"),
        ReturnedEdgeFromTransition("Go To Door From Standing", "Door"),
        FunctionTransitionNode("Clear Announcement", discard, guard=nobody_aboard),
        ArgumentEdgeToTransition("Announcements", "Clear Announcement", "announcement"),
    ], params={"stopped": True, "seats": seats, "capacity": capacity}, template="Cabin")


def build_deck(capacity: int = 40) -> ExecutableGraph:
    """``Deck`` (Ferry): passengers go below from the door, may go up, and come back
    to the door from either deck once stopped and announced; the same two guards
    as the cabin's keep arriving passengers at the door."""

    nobody_aboard = _nobody_aboard("Door", "Lower Deck", "Upper Deck")

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", str),
        ListPlaceNode("Lower Deck", str),
        ListPlaceNode("Upper Deck", str),
        ListPlaceNode("Announcements", str),
        FunctionTransitionNode("Go Below", passenger_moves, guard=_no_announcement),
        ArgumentEdgeToTransition("Door", "Go Below", "passenger"),
        ReturnedEdgeFromTransition("Go Below", "Lower Deck"),
        FunctionTransitionNode("Go Up", passenger_moves),
        ArgumentEdgeToTransition("Lower Deck", "Go Up", "passenger"),
        ReturnedEdgeFromTransition("Go Up", "Upper Deck"),
        FunctionTransitionNode("Go To Door", passenger_moves, guard=_stopped_and_announced),
        ArgumentEdgeToTransition("Lower Deck", "Go To Door", "passenger"),
        ReturnedEdgeFromTransition("Go To Door", "Door"),
        FunctionTransitionNode("Go To Door From Upper Deck", passenger_moves, guard=_stopped_and_announced),
        ArgumentEdgeToTransition("Upper Deck", "Go To Door From Upper Deck", "passenger"),
        ReturnedEdgeFromTransition("Go To Door From Upper Deck", "Door"),
        FunctionTransitionNode("Clear Announcement", discard, guard=nobody_aboard),
        ArgumentEdgeToTransition("Announcements", "Clear Announcement", "announcement"),
    ], params={"stopped": True, "capacity": capacity}, template="Deck")


def build_pod(seats: int = 4) -> ExecutableGraph:
    """``Pod`` (Shuttle): seats only, no standing."""

    def seat_free(graph: ExecutableGraph) -> bool:
        return _count(graph, "Seats") < graph.params["seats"] and _no_announcement(graph)

    nobody_aboard = _nobody_aboard("Door", "Seats")

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", str),
        ListPlaceNode("Seats", str),
        ListPlaceNode("Announcements", str),
        FunctionTransitionNode("Sit", passenger_moves, guard=seat_free),
        ArgumentEdgeToTransition("Door", "Sit", "passenger"),
        ReturnedEdgeFromTransition("Sit", "Seats"),
        FunctionTransitionNode("Go To Door", passenger_moves, guard=_stopped_and_announced),
        ArgumentEdgeToTransition("Seats", "Go To Door", "passenger"),
        ReturnedEdgeFromTransition("Go To Door", "Door"),
        FunctionTransitionNode("Clear Announcement", discard, guard=nobody_aboard),
        ArgumentEdgeToTransition("Announcements", "Clear Announcement", "announcement"),
    ], params={"stopped": True, "seats": seats, "capacity": seats}, template="Pod")


TEMPLATES = {"Cabin": build_cabin, "Deck": build_deck, "Pod": build_pod}


# --- the outer net: bodies reach inside the vehicle they hold ------------------------------


def aboard(vehicle: Vehicle) -> int:
    """How many passengers the vehicle carries, wherever they are in its cabin."""
    return sum(len(place.tokens) for place in vehicle.cabin.places if place.name != "Announcements")


def board(passenger: str, vehicle: Vehicle) -> None:
    """One passenger steps in at the door. A sink: it produces nothing."""
    NestedNets.put(vehicle.cabin, "Door", [passenger])


def alight(vehicle: Vehicle) -> str:
    """The passenger nearest the door steps out onto the platform."""
    return NestedNets.take(vehicle.cabin, "Door")[0]


def depart(vehicle: Vehicle, destination: str) -> Vehicle:
    """``destination`` is the net parameter of that name, handed to the body
    because it declares a parameter so named and nothing else supplies it."""
    vehicle.destination = destination
    NestedNets.set_param(vehicle.cabin, "stopped", False)
    return vehicle


def arrive(vehicle: Vehicle) -> Vehicle:
    NestedNets.set_param(vehicle.cabin, "stopped", True)
    NestedNets.put(vehicle.cabin, "Announcements", [f"Arrived at {vehicle.destination}"])
    return vehicle


def route_arrival(vehicle: Vehicle) -> dict[str, Vehicle]:
    """Output distribution for ``Arrive``: to the place the destination names, which
    the vehicle then forgets."""
    destination, vehicle.destination = vehicle.destination, None
    return {destination: vehicle}


def _front_vehicle(graph: ExecutableGraph, location: str) -> Vehicle:
    return graph.place_named(location).tokens[0]


def has_room(location: str):
    def guard(graph: ExecutableGraph) -> bool:
        vehicle = _front_vehicle(graph, location)
        return aboard(vehicle) < vehicle.cabin.params["capacity"]
    guard.__name__ = f"has_room_at_{location}"
    return guard


def someone_at_the_door(location: str):
    def guard(graph: ExecutableGraph) -> bool:
        return len(_front_vehicle(graph, location).cabin.place_named("Door").tokens) > 0
    guard.__name__ = f"someone_at_the_door_at_{location}"
    return guard


def city_nodes_and_edges(depot: list[Vehicle], harbour: list[Vehicle], waiting_at_depot: list[str]):
    """Every node and edge of the city, with the given seed."""
    nodes = [
        ListPlaceNode("Depot", Vehicle, depot, nested=True),
        ListPlaceNode("Central", Vehicle, nested=True),
        ListPlaceNode("Harbour", Vehicle, harbour, nested=True),
        ListPlaceNode("Airport", Vehicle, nested=True),
        ListPlaceNode("Platform@Depot", str, waiting_at_depot),
        ListPlaceNode("Platform@Central", str),
        ListPlaceNode("Platform@Harbour", str),
        ListPlaceNode("Platform@Airport", str),
        ListPlaceNode("In Transit", Vehicle, nested=True),
    ]
    for location in LOCATIONS:
        nodes += [
            FunctionTransitionNode(f"Board {location}", board, guard=has_room(location)),
            ArgumentEdgeToTransition(f"Platform@{location}", f"Board {location}", "passenger"),
            MutateEdge(location, f"Board {location}", "vehicle"),
        ]
    for location in LOCATIONS:
        nodes += [
            FunctionTransitionNode(f"Alight {location}", alight, guard=someone_at_the_door(location)),
            MutateEdge(location, f"Alight {location}", "vehicle"),
            ReturnedEdgeFromTransition(f"Alight {location}", f"Platform@{location}"),
        ]
    for location in LOCATIONS:
        nodes += [
            FunctionTransitionNode(f"Depart {location}", depart),
            ArgumentEdgeToTransition(location, f"Depart {location}", "vehicle"),
            ReturnedEdgeFromTransition(f"Depart {location}", "In Transit"),
        ]
    nodes += [
        FunctionTransitionNode("Arrive", arrive, output_distribution_function=route_arrival),
        ArgumentEdgeToTransition("In Transit", "Arrive", "vehicle"),
        *[ReturnedEdgeFromTransition("Arrive", location) for location in LOCATIONS],
    ]
    return nodes


def build_city(record_events: bool = True, restore_tokens_on_failure: bool = False) -> ExecutableGraph:
    """The acceptance seed: one bus with two seats at the depot, one ferry at the
    harbour, three passengers waiting at the depot."""
    return ExecutableGraphOperations.construct_graph(
        city_nodes_and_edges(
            depot=[Vehicle(name="Bus 1", kind="Bus", cabin=build_cabin(seats=2))],
            harbour=[Vehicle(name="Ferry 1", kind="Ferry", cabin=build_deck())],
            waiting_at_depot=["Ada", "Bram", "Cato"],
        ),
        params={"destination": "Central"},
        record_events=record_events,
        restore_tokens_on_failure=restore_tokens_on_failure,
    )


@petri_net(name="transit", description="Vehicles board, carry and alight passengers")
def transit() -> ExecutableGraph:
    return build_city()


# --- the acceptance script --------------------------------------------------------------------


async def fire(graph: ExecutableGraph, name: str) -> None:
    """Fire one named outer transition, which must be fireable."""
    def pinned(_graph, fireable):
        return next((t for t in fireable if t.name == name), None)
    fired = await ExecutableGraphOperations.fire_one_transition(
        graph, *ExecutableGraphOperations.maps(graph), transition_selector=pinned, descend=False,
    )
    assert fired == name, f"{name} was not fireable"


async def run_to_quiet(graph: ExecutableGraph, holding_back: set[str]) -> None:
    """Run until nothing can fire, outer or inner, with some outer transitions held
    back (the design's script fires those by hand)."""
    await Runner.run(RunContext(graph=graph, disabled=set(holding_back)))


# The outer transitions the design's script fires by hand.
HELD_BACK = {f"{verb} {location}" for verb in ("Alight", "Depart") for location in LOCATIONS} | {"Arrive"}


async def acceptance_script(graph: ExecutableGraph) -> list:
    """Steps 1 to 5 of the design's script; returns the outer net's event log."""
    for _ in range(3):
        await fire(graph, "Board Depot")
    await run_to_quiet(graph, HELD_BACK)                 # inside the bus: Sit, Sit, Stand
    await fire(graph, "Depart Depot")                    # destination: the net parameter
    await run_to_quiet(graph, HELD_BACK)                 # nothing can move while not stopped
    await fire(graph, "Arrive")
    await run_to_quiet(graph, HELD_BACK)                 # the announcement sends everyone to the door
    await fire(graph, "Alight Central")
    await fire(graph, "Alight Central")
    return graph.drain_events()


def describe_log(events: list) -> list[str]:
    lines = []
    for event in events:
        if isinstance(event, Nested) and isinstance(event.event, Fired):
            lines.append(f"{'/'.join(event.path)}: {event.event.transition}")
        elif isinstance(event, Fired):
            lines.append(event.transition)
    return lines


def main() -> None:
    graph = build_city()
    events = asyncio.run(acceptance_script(graph))
    for line in describe_log(events):
        print(line)
    bus = graph.place_named("Central").tokens[0]
    print("at Central:", bus.name, "| door:", bus.cabin.place_named("Door").tokens,
          "| platform:", graph.place_named("Platform@Central").tokens)


if __name__ == "__main__":
    main()
