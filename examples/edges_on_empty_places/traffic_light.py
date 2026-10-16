"""The traffic light: cars wait at a light that cycles red, green and amber, and all go when
it turns green.

The conformance net ``traffic-light`` of PROTOCOL.md §11 (since 2026-10-16; one place for the
light, and two transitions, since later that day), and the example of the edge that *takes*
every token of a place beside a plain read. Every place holds strings. The light is one place,
``Light``, holding one token whose value is its colour: ``"red"``, ``"green"`` or ``"amber"``,
going round in that order (``next_colour``). The token is a ``Colour``, a ``str`` whose JSON form
is that plain string and whose ``color`` is the lamp's hex (red ``#ff6347``, green ``#32cd32``,
amber ``#ffa500``), which the protocol carries as the token's colour. Cars wait in ``Waiting``,
oldest first, and go to ``Passed``. Two transitions, one that moves the cars and one that
changes the light:

- ``Go``, while the light is green and someone is waiting, *drains* ``Waiting`` (a
  ``DrainEdge``: every car waiting, oldest first, all consumed) and *reads* ``Light`` (a
  ``SnapshotEdge``: the colour is read, not taken), and puts the cars in ``Passed``, one token
  each, oldest first. Everyone who waited at red goes once the light is green, and a car
  arriving while it is green goes straight through. The light's token is untouched.
- ``Change`` takes the light and puts the next colour back: red to green, green to amber,
  amber to red. It is unguarded, so it can always fire: the light cycles by itself and the
  net never quiesces, like a real junction: run it a step at a time.

They are declared in that order, and the runner fires one transition a step, the first
declared: at green with cars waiting both can fire, and ``Go`` does. ``Go`` is guarded on
someone waiting as well as on green because a drain accepts an empty place: without that,
``Go`` would fire at every green step taking nothing and ``Change`` would never fire (the
light would stick on green); declared the other way round, ``Change`` would always win at
green and the cars would never go. The guard hands the step to ``Change`` when the queue is
empty, so this example never fires a drain on an empty queue (the shop's survey,
``shop.py``, shows the zero case). The read hands the body the colour and shows the
dependency in the picture; ``Light`` is never empty, so it is the guard that holds the cars
at amber and red.

``Go``'s only consuming edge is a drain, which may take nothing, so it is structurally what
``assert_no_source_transitions`` flags; its guard is what makes it safe, so this example does
not call that check. ``Change`` takes the light from ``Light`` and puts it back, a loop, so
``assert_acyclic`` would reject the net as well. Cars come from outside, put into ``Waiting``
as an ``inject`` does.

The net declares, beside it, two controls and a place style (PROTOCOL.md §5.1), which a viewer
draws without knowing the net and runs with ``press``, the code being here: ``car`` (``+ Car``,
a text box, acting on ``Waiting``) adds a car named as typed, or ``Car N`` when nothing is typed,
``N`` one more than the cars in ``Waiting`` and ``Passed`` together; ``change`` (``Change``, a
button) fires ``Change`` if it is fireable. ``Light`` is filled with its token's colour
(``PlaceStyle(fill="token")``)::

    @petri_net(name="traffic-light", ..., controls=CONTROLS, place_styles=PLACE_STYLES)

Run it to see the transcript's script, what each firing does and the marking it ends on::

    uv run python examples/edges_on_empty_places/traffic_light.py

The notebook beside it draws the junction firing by firing, with cars arriving on a
schedule or by hand::

    uv run --extra examples marimo edit examples/edges_on_empty_places/traffic_light_junction.py
"""

from __future__ import annotations

import asyncio

from petritype import petri_net
from petritype.runtime import ControlSpec, PlaceStyle
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    TokenIds,
)

COLOURS = ("red", "green", "amber")
PLACES = ("Light", "Waiting", "Passed")
# The lamp each colour lights, as the hex a viewer draws the light's token in (the junction
# notebook's tomato, limegreen and orange).
LAMPS = {"red": "#ff6347", "green": "#32cd32", "amber": "#ffa500"}


class Colour(str):
    """The light's token: the string ``"red"``, ``"green"`` or ``"amber"``, which is also its JSON
    form, whose ``color`` is the lamp's hex (``LAMPS``), the token's colour on the wire. Any other
    string is refused, so an ``inject`` of ``"blue"`` into ``Light`` is ``invalid_token``."""

    def __new__(cls, value: str) -> "Colour":
        if value not in LAMPS:
            raise ValueError(f"a light is red, green or amber, not {value!r}")
        return super().__new__(cls, value)

    @property
    def color(self) -> str:
        return LAMPS[self]


def next_colour(colour: Colour) -> Colour:
    """The colour after ``colour``: red, green, amber and red again."""
    return Colour(COLOURS[(COLOURS.index(colour) + 1) % len(COLOURS)])


def colour(graph: ExecutableGraph) -> Colour | None:
    """The light's colour, ``"red"``, ``"green"`` or ``"amber"``: the token in ``Light``.
    ``None`` while a firing has taken the light and not yet put it back."""
    light = graph.place_named("Light").tokens
    return light[0] if light else None


# --- the guard: the light's colour and the queue, read from the net -------------------------


def green_and_someone_waiting(graph: ExecutableGraph) -> bool:
    """``Go``'s guard: the light is green and a car is waiting. Without the second half the
    drain, which accepts an empty place, would let ``Go`` fire at every green taking nothing."""
    return colour(graph) == "green" and bool(graph.place_named("Waiting").tokens)


# --- bodies ---------------------------------------------------------------------------------


def go(cars: list[str], light: Colour) -> list[str]:
    """Every car waiting, oldest first, and a copy of the light's colour, green: every car
    goes past the light, one token each, and the light stays green."""
    return cars


def change(light: Colour) -> Colour:
    """The light: it goes on round, red to green, green to amber, amber to red."""
    return next_colour(light)


# --- the net ---------------------------------------------------------------------------------


def build_traffic_light(record_events: bool = True) -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Light", Colour, [Colour("red")]),
        ListPlaceNode("Waiting", str),
        ListPlaceNode("Passed", str),
        # At green with someone waiting, every car waiting goes; the light is read.
        FunctionTransitionNode("Go", go, guard=green_and_someone_waiting),
        DrainEdge("Waiting", "Go", "cars"),
        SnapshotEdge("Light", "Go", "light"),
        ReturnedEdgeFromTransition("Go", "Passed"),
        # Whatever the colour, the light goes on round.
        FunctionTransitionNode("Change", change),
        ArgumentEdgeToTransition("Light", "Change", "light"),
        ReturnedEdgeFromTransition("Change", "Light"),
    ], record_events=record_events)


# --- what the net declares beside it: its controls and the light's style ----------------------


def car(name: str, graph: ExecutableGraph) -> str:
    """The car a press of ``+ Car`` adds to ``Waiting``: ``name`` when one was typed, else
    ``Car N`` with ``N`` one more than the cars in ``Waiting`` and ``Passed`` together. Read off
    the marking, with no counter of its own, so every runtime names it alike."""
    if name:
        return name
    return f"Car {len(graph.place_named('Waiting').tokens) + len(graph.place_named('Passed').tokens) + 1}"


# In the order a viewer lists them.
CONTROLS = {
    "car": ControlSpec.inject("Waiting", car, "+ Car", kind="text", config={"placeholder": "name"}),
    "change": ControlSpec.fire("Change", "Change"),
}
# The light's place is filled with its token's colour.
PLACE_STYLES = {"Light": PlaceStyle(fill="token")}


@petri_net(
    name="traffic-light",
    description="Cars wait at a light that cycles red, green and amber, and all go when it turns green",
    controls=CONTROLS,
    place_styles=PLACE_STYLES,
)
def traffic_light() -> ExecutableGraph:
    return build_traffic_light()


# --- the transcript's script -----------------------------------------------------------------

# The transition each step fires, and the cars that arrive before it (PROTOCOL.md §11): the
# light goes green with nobody waiting, then amber; two cars arrive at amber and wait through
# red; the light goes green and both go; a car arriving at green goes straight through; the
# light goes amber.
SCRIPT = (
    ("Change", {}),
    ("Change", {}),
    ("Change", {"Waiting": ["Car 1", "Car 2"]}),
    ("Change", {}),
    ("Go", {}),
    ("Go", {"Waiting": ["Car 3"]}),
    ("Change", {}),
)


def put(graph: ExecutableGraph, tokens: dict[str, list[str]]) -> None:
    """Append tokens to places, with fresh ids, as an ``inject`` does."""
    for place, values in tokens.items():
        TokenIds.append(graph, graph.place_named(place), list(values))


def fireable(graph: ExecutableGraph) -> list[str]:
    """The transitions that could fire now, in declaration order."""
    place_nodes, incoming, _, read = ExecutableGraphOperations.maps(graph)
    return [
        transition.name for transition in graph.transitions
        if ExecutableGraphCheck.transition_is_fireable(transition, graph, incoming, place_nodes, read)
    ]


def step(graph: ExecutableGraph) -> int:
    """Fire one transition, as the protocol's ``step`` does; how many fired (0 or 1)."""
    _, fired = asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    return fired


def marking(graph: ExecutableGraph, places=PLACES) -> dict[str, list[str]]:
    return {place: list(graph.place_named(place).tokens) for place in places}


def cars(names: list[str]) -> str:
    """``["Car 1", "Car 2", "Car 3"]`` as ``"Car 1, Car 2 and Car 3"``."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def explain(fired: str, went: list[str], waiting: list[str], light: str, came_at_green: bool = False) -> str:
    """A firing in words: ``went`` are the cars it sent past the light, ``waiting`` the cars
    left waiting after it, ``light`` the colour it left the light, ``came_at_green`` whether
    every car that went arrived while the light was already green."""
    if fired == "Go" and came_at_green:
        return f"Go fired: {cars(went)} went straight through"
    if fired == "Go":
        return f"Go fired: the drain took {cars(went)} past the light; the light was read and left {light}"
    if fired == "Change" and waiting and light == "green":
        return f"Change fired: the light went green; {cars(waiting)} can go"
    if fired == "Change" and waiting:
        wait = "waits" if len(waiting) == 1 else "wait"
        return f"Change fired: the light went {light}; {cars(waiting)} {wait} for green"
    if fired == "Change":
        return f"Change fired: the light went {light}"
    return f"{fired} fired"


async def fire(graph: ExecutableGraph) -> str | None:
    """One firing, the first fireable transition in declaration order, as the protocol's
    ``step`` chooses; what it did in words, or ``None`` when nothing could fire (never, from
    the seed: the light always has somewhere to go)."""
    before = len(graph.place_named("Passed").tokens)
    # Ids only grow and the light gets a fresh one at every Change, so a car whose id is
    # higher than the light's arrived after the light last changed: while it was green.
    light_ids = graph.place_named("Light").token_ids
    came_at_green = bool(light_ids) and all(car > light_ids[0] for car in graph.place_named("Waiting").token_ids)
    _, fired = await ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1)
    if not fired:
        return None
    went = list(graph.place_named("Passed").tokens[before:])
    return explain(graph.last_fired, went, list(graph.place_named("Waiting").tokens), colour(graph),
                   came_at_green)


def main() -> None:
    graph = build_traffic_light(record_events=False)
    print(f"The light is {colour(graph)} and nobody is waiting. Fireable: {fireable(graph)}")
    for n, (_, tokens) in enumerate(SCRIPT, 1):
        if tokens:
            arriving = tokens["Waiting"]
            light = colour(graph)
            put(graph, tokens)
            print(f"{cars(arriving)} {'arrives' if len(arriving) == 1 else 'arrive'} while the light is "
                  f"{light}. Fireable: {fireable(graph)}")
        print(f"Step {n}. {asyncio.run(fire(graph))}.")
    print(f"The marking: {marking(graph)}")
    print(f"Fireable: {fireable(graph)}. The light keeps going round, so the net never quiesces.")


if __name__ == "__main__":
    main()
