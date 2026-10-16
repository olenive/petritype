"""Transit: vehicles whose cabins are Petri nets of their own (nets as tokens).

The example of ``NETS_AS_TOKENS.md`` §2, and the conformance net ``transit`` of
PROTOCOL.md §11. An outer net, the city, holds vehicle tokens standing at the
stops of a loop (Depot, Central, Harbour, Airport, and round again) or in
transit; each vehicle holds an inner net, its cabin, whose structure depends on
the kind of vehicle (a bus seats and stands its passengers, a ferry has two
decks, a shuttle only seats). The outer net never shares a place with a cabin:
it reaches inside only through the vehicle tokens its bodies hold, putting
passengers in at the door, taking them out, opening the doors, setting the
cabin's ``stopped`` parameter and posting an announcement. Passengers are plain
strings.

The net runs its journey on its own. The vehicle's state is where its doors
are: its cabin holds one token per door, the door's name, in one of two
places, ``Doors Open`` and ``Doors Closed`` (a bus has one door, ``Front``; a
ferry two, ``Bow`` and ``Stern``). A door open with no announcement standing
is boarding, a door open with an announcement standing is alighting, every door
closed is under way. The city's guards read it (board while boarding, alight
while alighting, depart once every door is closed), the cabin closes the doors
(``Close Doors``, one door a firing, once everyone at the door has settled) and
``Arrive`` opens them in its own body as it puts up the announcement. So plain
stepping, the city first and a cabin only when the city has nothing to fire,
carries every passenger to ``Arrived`` with nothing disabled and no transition
picked by name. A passenger who alights goes to ``Arrived``, never back onto a
platform, where they would board again. The city keeps the engine's default
deep-step policy, outer first, because its guards let it fall quiet while a cabin
has work; ``deep_policy="interleaved"`` is the policy for a net that cannot give
its outer transitions guards, since it lets every live net fire in turn.

Run it to watch the journey, with the city's event log showing the inner firings
under the path of the vehicle they happened in::

    uv run python examples/tokens_as_nets/transit.py
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
from petritype.core.serialisation import Templates

# The stops, in the order of the loop every vehicle follows.
LOCATIONS = ("Depot", "Central", "Harbour", "Airport")
NEXT_STOP = {stop: LOCATIONS[(index + 1) % len(LOCATIONS)] for index, stop in enumerate(LOCATIONS)}
Kind = Literal["Bus", "Tram", "Ferry", "Shuttle"]

# The doors of each template, by name, in the order they are seeded and closed.
DOORS = {"Cabin": ("Front",), "Deck": ("Bow", "Stern"), "Pod": ("Front",)}
# What is in a cabin but is not a passenger.
NOT_PASSENGERS = ("Announcements", "Doors Open", "Doors Closed")


class Vehicle(BaseModel):
    """A vehicle token: its cabin is an inner net, named ``cabin`` whatever the kind.
    Its JSON form is its fields with the cabin in its saved form (template,
    marking, params, enabled), so a vehicle can be saved, injected and restored.
    ``destination`` is set only while the vehicle is in transit."""

    name: str
    kind: Kind
    destination: Optional[str] = None
    cabin: ExecutableGraph

    def __petritype_nets__(self) -> dict[str, ExecutableGraph]:
        return {"cabin": self.cabin}


# --- the inner nets: one template per kind -------------------------------------------------


def passenger_moves(passenger: str) -> str:
    return passenger


def door_closes(door: str) -> str:
    """One door closes: its name passes from ``Doors Open`` to ``Doors Closed``."""
    return door


def discard(announcement: str) -> None:
    """Everyone has left: the announcement is done with."""
    return None


def _count(graph: ExecutableGraph, place: str) -> int:
    return len(graph.place_named(place).tokens)


def boarding(cabin: ExecutableGraph) -> bool:
    """A door is open and no announcement stands: passengers may come aboard."""
    return _count(cabin, "Doors Open") > 0 and _count(cabin, "Announcements") == 0


def alighting(cabin: ExecutableGraph) -> bool:
    """An announcement stands: passengers make for the door. It stands only while the
    doors are open (``Arrive`` opens them as it puts it up, and ``Close Doors``
    waits until it is cleared)."""
    return _count(cabin, "Announcements") > 0


def under_way(cabin: ExecutableGraph) -> bool:
    """Every door is closed."""
    return _count(cabin, "Doors Open") == 0


def _doors_places(doors: tuple[str, ...]) -> list:
    """The three places every template has besides where passengers go:
    announcements, and the two places of the doors, every door seeded open."""
    return [
        ListPlaceNode("Announcements", str),
        ListPlaceNode("Doors Open", str, list(doors)),
        ListPlaceNode("Doors Closed", str),
    ]


def _doors_transitions(passenger_places: tuple[str, ...]) -> list:
    """The two transitions common to every template: ``Close Doors`` moves one door
    from ``Doors Open`` to ``Doors Closed`` once nobody is left at the door and no
    announcement stands (nobody need be aboard: an empty vehicle closes up and moves
    on), so a vehicle with two doors fires it twice; ``Clear Announcement``
    consumes the announcement once nobody is aboard at all."""

    def settled(graph: ExecutableGraph) -> bool:
        return _count(graph, "Door") == 0 and _count(graph, "Announcements") == 0

    def everyone_off(graph: ExecutableGraph) -> bool:
        return all(_count(graph, place) == 0 for place in passenger_places)

    return [
        FunctionTransitionNode("Close Doors", door_closes, guard=settled),
        ArgumentEdgeToTransition("Doors Open", "Close Doors", "door"),
        ReturnedEdgeFromTransition("Close Doors", "Doors Closed"),
        FunctionTransitionNode("Clear Announcement", discard, guard=everyone_off),
        ArgumentEdgeToTransition("Announcements", "Clear Announcement", "announcement"),
    ]


def _seat_free(graph: ExecutableGraph) -> bool:
    return _count(graph, "Seats") < graph.params["seats"] and boarding(graph)


@Templates.template("Cabin")
def build_cabin(seats: int = 2, capacity: int = 6) -> ExecutableGraph:
    """``Cabin`` (Bus, Tram), one door, ``Front``: while boarding, passengers at the
    door sit while there are seats, else stand; once alighting they go to the door
    from wherever they are."""

    def no_seat_free(graph: ExecutableGraph) -> bool:
        return _count(graph, "Seats") >= graph.params["seats"] and boarding(graph)

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", str),
        ListPlaceNode("Seats", str),
        ListPlaceNode("Standing", str),
        *_doors_places(DOORS["Cabin"]),
        FunctionTransitionNode("Sit", passenger_moves, guard=_seat_free),
        ArgumentEdgeToTransition("Door", "Sit", "passenger"),
        ReturnedEdgeFromTransition("Sit", "Seats"),
        FunctionTransitionNode("Stand", passenger_moves, guard=no_seat_free),
        ArgumentEdgeToTransition("Door", "Stand", "passenger"),
        ReturnedEdgeFromTransition("Stand", "Standing"),
        FunctionTransitionNode("Go To Door", passenger_moves, guard=alighting),
        ArgumentEdgeToTransition("Seats", "Go To Door", "passenger"),
        ReturnedEdgeFromTransition("Go To Door", "Door"),
        FunctionTransitionNode("Go To Door From Standing", passenger_moves, guard=alighting),
        ArgumentEdgeToTransition("Standing", "Go To Door From Standing", "passenger"),
        ReturnedEdgeFromTransition("Go To Door From Standing", "Door"),
        *_doors_transitions(("Door", "Seats", "Standing")),
    ], params={"stopped": True, "seats": seats, "capacity": capacity}, template="Cabin")


@Templates.template("Deck")
def build_deck(capacity: int = 40) -> ExecutableGraph:
    """``Deck`` (Ferry), two doors, ``Bow`` and ``Stern``: while boarding, passengers
    go below from the door; they may go up at any time; once alighting they come
    back to the door from either deck."""

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", str),
        ListPlaceNode("Lower Deck", str),
        ListPlaceNode("Upper Deck", str),
        *_doors_places(DOORS["Deck"]),
        FunctionTransitionNode("Go Below", passenger_moves, guard=boarding),
        ArgumentEdgeToTransition("Door", "Go Below", "passenger"),
        ReturnedEdgeFromTransition("Go Below", "Lower Deck"),
        FunctionTransitionNode("Go Up", passenger_moves),
        ArgumentEdgeToTransition("Lower Deck", "Go Up", "passenger"),
        ReturnedEdgeFromTransition("Go Up", "Upper Deck"),
        FunctionTransitionNode("Go To Door", passenger_moves, guard=alighting),
        ArgumentEdgeToTransition("Lower Deck", "Go To Door", "passenger"),
        ReturnedEdgeFromTransition("Go To Door", "Door"),
        FunctionTransitionNode("Go To Door From Upper Deck", passenger_moves, guard=alighting),
        ArgumentEdgeToTransition("Upper Deck", "Go To Door From Upper Deck", "passenger"),
        ReturnedEdgeFromTransition("Go To Door From Upper Deck", "Door"),
        *_doors_transitions(("Door", "Lower Deck", "Upper Deck")),
    ], params={"stopped": True, "capacity": capacity}, template="Deck")


@Templates.template("Pod")
def build_pod(seats: int = 4) -> ExecutableGraph:
    """``Pod`` (Shuttle), one door, ``Front``: seats only, no standing, so it carries
    no more than it seats."""

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", str),
        ListPlaceNode("Seats", str),
        *_doors_places(DOORS["Pod"]),
        FunctionTransitionNode("Sit", passenger_moves, guard=_seat_free),
        ArgumentEdgeToTransition("Door", "Sit", "passenger"),
        ReturnedEdgeFromTransition("Sit", "Seats"),
        FunctionTransitionNode("Go To Door", passenger_moves, guard=alighting),
        ArgumentEdgeToTransition("Seats", "Go To Door", "passenger"),
        ReturnedEdgeFromTransition("Go To Door", "Door"),
        *_doors_transitions(("Door", "Seats")),
    ], params={"stopped": True, "seats": seats, "capacity": seats}, template="Pod")


# Registered by the decorators above: what rebuilds a cabin from a saved vehicle.
TEMPLATES = {name: Templates.get(name) for name in ("Cabin", "Deck", "Pod")}


# --- the outer net: bodies reach inside the vehicle they hold ------------------------------


def aboard(vehicle: Vehicle) -> int:
    """How many passengers the vehicle carries, wherever they are in its cabin."""
    return sum(len(place.tokens) for place in vehicle.cabin.places if place.name not in NOT_PASSENGERS)


def board(passenger: str, vehicle: Vehicle) -> None:
    """One passenger steps in at the door. A sink: it produces nothing."""
    NestedNets.put(vehicle.cabin, "Door", [passenger])


def alight(vehicle: Vehicle) -> str:
    """The passenger nearest the door steps out, and has arrived."""
    return NestedNets.take(vehicle.cabin, "Door")[0]


def departure_from(stop: str):
    """The body of ``Depart <stop>``. ``destination`` is the net parameter of that
    name, handed to the body because it declares a parameter so named and nothing
    else supplies it: when it names a place every departure goes there, and when it
    is ``None`` the vehicle goes on to the next stop of the loop."""

    def depart(vehicle: Vehicle, destination: Optional[str]) -> Vehicle:
        vehicle.destination = destination if destination is not None else NEXT_STOP[stop]
        NestedNets.set_param(vehicle.cabin, "stopped", False)
        return vehicle
    depart.__name__ = f"depart_{stop.lower()}"
    return depart


def arrive(vehicle: Vehicle) -> Vehicle:
    """The vehicle stops, opens its doors (every door, in order, from ``Doors
    Closed`` to ``Doors Open``) and announces where it is, which sets its
    passengers alighting. The doors open here, in the outer body, and not in an
    inner transition: they are all closed until then, so ``Depart`` would fire
    again before the cabin had its turn."""
    cabin = vehicle.cabin
    NestedNets.set_param(cabin, "stopped", True)
    NestedNets.put(cabin, "Doors Open", NestedNets.take(cabin, "Doors Closed", _count(cabin, "Doors Closed")))
    NestedNets.put(cabin, "Announcements", [f"Arrived at {vehicle.destination}"])
    return vehicle


def route_arrival(vehicle: Vehicle) -> dict[str, Vehicle]:
    """Output distribution for ``Arrive``: to the place the destination names, which
    the vehicle then forgets."""
    destination, vehicle.destination = vehicle.destination, None
    return {destination: vehicle}


def _front_vehicle(graph: ExecutableGraph, stop: str) -> Vehicle:
    return graph.place_named(stop).tokens[0]


def can_board(stop: str):
    def guard(graph: ExecutableGraph) -> bool:
        vehicle = _front_vehicle(graph, stop)
        return boarding(vehicle.cabin) and aboard(vehicle) < vehicle.cabin.params["capacity"]
    guard.__name__ = f"can_board_at_{stop}"
    return guard


def can_alight(stop: str):
    def guard(graph: ExecutableGraph) -> bool:
        cabin = _front_vehicle(graph, stop).cabin
        return alighting(cabin) and _count(cabin, "Door") > 0
    guard.__name__ = f"can_alight_at_{stop}"
    return guard


def can_depart(stop: str):
    def guard(graph: ExecutableGraph) -> bool:
        return under_way(_front_vehicle(graph, stop).cabin)
    guard.__name__ = f"can_depart_from_{stop}"
    return guard


def city_nodes_and_edges(vehicles: dict[str, list[Vehicle]], waiting: dict[str, list[str]]) -> list:
    """Every node and edge of the city, with the given seed: the vehicles standing
    at each stop and the passengers waiting on each platform."""
    nodes = [ListPlaceNode(stop, Vehicle, vehicles.get(stop, []), nested=True) for stop in LOCATIONS]
    nodes += [ListPlaceNode(f"Platform@{stop}", str, waiting.get(stop, [])) for stop in LOCATIONS]
    nodes += [
        ListPlaceNode("In Transit", Vehicle, nested=True),
        ListPlaceNode("Arrived", str),
    ]
    for stop in LOCATIONS:
        nodes += [
            FunctionTransitionNode(f"Board {stop}", board, guard=can_board(stop)),
            ArgumentEdgeToTransition(f"Platform@{stop}", f"Board {stop}", "passenger"),
            MutateEdge(stop, f"Board {stop}", "vehicle"),
        ]
    for stop in LOCATIONS:
        nodes += [
            FunctionTransitionNode(f"Alight {stop}", alight, guard=can_alight(stop)),
            MutateEdge(stop, f"Alight {stop}", "vehicle"),
            ReturnedEdgeFromTransition(f"Alight {stop}", "Arrived"),
        ]
    for stop in LOCATIONS:
        nodes += [
            FunctionTransitionNode(f"Depart {stop}", departure_from(stop), guard=can_depart(stop)),
            ArgumentEdgeToTransition(stop, f"Depart {stop}", "vehicle"),
            ReturnedEdgeFromTransition(f"Depart {stop}", "In Transit"),
        ]
    nodes += [
        FunctionTransitionNode("Arrive", arrive, output_distribution_function=route_arrival),
        ArgumentEdgeToTransition("In Transit", "Arrive", "vehicle"),
        *[ReturnedEdgeFromTransition("Arrive", stop) for stop in LOCATIONS],
    ]
    return nodes


# The seed's passengers, by the platform they wait on.
WAITING = {"Depot": ["Ada", "Bram", "Cato"], "Central": ["Dara", "Eve"], "Harbour": ["Finn", "Gwen"]}
PASSENGERS = tuple(passenger for queue in WAITING.values() for passenger in queue)


def build_city(
    record_events: bool = True, restore_tokens_on_failure: bool = False, destination: Optional[str] = None,
) -> ExecutableGraph:
    """The seed: a bus with two seats at the depot and a ferry at the harbour, both
    boarding and empty; seven passengers waiting on three platforms; no
    destination override unless one is given, so every vehicle follows the loop."""
    return ExecutableGraphOperations.construct_graph(
        city_nodes_and_edges(
            vehicles={
                "Depot": [Vehicle(name="Bus 1", kind="Bus", cabin=build_cabin(seats=2))],
                "Harbour": [Vehicle(name="Ferry 1", kind="Ferry", cabin=build_deck())],
            },
            waiting={stop: list(queue) for stop, queue in WAITING.items()},
        ),
        params={"destination": destination},
        record_events=record_events,
        restore_tokens_on_failure=restore_tokens_on_failure,
    )


@petri_net(name="transit", description="Vehicles board, carry and alight passengers")
def transit(destination: str | None = None) -> ExecutableGraph:
    """The city as the runner loads it. ``destination`` is the net parameter of
    that name, the override of the loop, and its annotation is its declared type:
    the runner decodes ``set_param`` by it (PROTOCOL.md §6.4), so the override
    takes a place name or ``null``, in either order, and refuses a number."""
    return build_city(destination=destination)


# --- stepping ---------------------------------------------------------------------------------


async def step(city: ExecutableGraph) -> Optional[str]:
    """One firing, outer or inner, the engine's own choice: the city's first
    fireable transition in declared order, else the first cabin with anything to
    fire, in place order then token order. Returns what fired (an inner firing
    under its path, ``"Depot/#1/cabin/Sit"``), or ``None`` if nothing could."""
    return await ExecutableGraphOperations.fire_one_transition(city, *ExecutableGraphOperations.maps(city))


def travelling(city: ExecutableGraph) -> int:
    """How many passengers have not arrived: waiting on a platform or aboard a vehicle."""
    waiting = sum(_count(city, f"Platform@{stop}") for stop in LOCATIONS)
    vehicles = [vehicle for place in (*LOCATIONS, "In Transit") for vehicle in city.place_named(place).tokens]
    return waiting + sum(aboard(vehicle) for vehicle in vehicles)


def everyone_arrived(city: ExecutableGraph) -> bool:
    return travelling(city) == 0


async def journey(city: ExecutableGraph, budget: int = 200) -> list:
    """Step until every passenger is in ``Arrived``, or ``budget`` firings have been
    made, or nothing can fire; returns the city's event log. The vehicles never
    stop going round, so the passengers, not the net, say when the journey is over."""
    for _ in range(budget):
        if everyone_arrived(city) or await step(city) is None:
            break
    return city.drain_events()


def describe_log(events: list) -> list[str]:
    lines = []
    for event in events:
        if isinstance(event, Nested) and isinstance(event.event, Fired):
            lines.append(f"{'/'.join(event.path)}: {event.event.transition}")
        elif isinstance(event, Fired):
            lines.append(event.transition)
    return lines


def main() -> None:
    city = build_city()
    events = asyncio.run(journey(city))
    for number, line in enumerate(describe_log(events), 1):
        print(f"{number:3}. {line}")
    print("arrived:", ", ".join(city.place_named("Arrived").tokens))


if __name__ == "__main__":
    main()
