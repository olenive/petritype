"""Hike: a flat net whose concerns all read and change one shared place (groups).

A hiker has one store of energy, the place ``Energy``, holding one token. Three
concerns use it, and each is a **group**, a label on its places and transitions
that only drawings read:

- **Walk**: ``Walk`` takes the next leg from ``Trail``, spends two energy and puts
  the leg into ``Walked``. It needs two energy to set off.
- **Eat**: ``Eat`` takes a snack from ``Pack``, gains three energy and puts the
  snack into ``Eaten``. The hiker eats only when too tired to walk.
- **Rest**: ``Rest`` takes a bench from ``Benches``, gains one energy and puts
  the bench into ``Rested At``. The hiker rests only when too tired to walk and
  with nothing left to eat.

``Energy`` is in no group: every concern reads it in its guard and changes it
through a mutate edge, so the concerns cannot be split into subnets without
moving their rules into the net around them. The net stays flat, and the groups
gather its parts under names for a drawing: each is a dotted frame titled with
its name, and ``Energy`` stands outside them. Being in a group changes nothing
about how the net runs.

The guards leave one transition fireable at a time, so the day goes the same way
whatever the tie-break: walk, eat the apple, walk twice, rest on both benches,
walk the last leg, and then nothing can fire, the trail walked with no energy
left. Run it to watch the day::

    uv run python examples/groups/hike.py

or serve it as a net process, ``hike``, which a viewer can host and draw with its
groups (PROTOCOL.md §5.1)::

    python -m petritype.runner --module examples.groups.hike
"""

from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReturnedEdgeFromTransition,
)


class Energy(BaseModel):
    """How much the hiker has left to spend."""

    level: int


# What a leg costs and what a snack and a rest give back.
WALK_COST = 2
SNACK_GAIN = 3
REST_GAIN = 1

# The seed: the trail's legs, the snack in the pack and the benches along the way.
LEGS = ("Leg 1", "Leg 2", "Leg 3", "Leg 4")
SNACKS = ("Apple",)
BENCHES = ("Bench 1", "Bench 2")


def energy_of(graph: ExecutableGraph) -> int:
    return graph.place("Energy").tokens[0].level


def can_walk(graph: ExecutableGraph) -> bool:
    """``Walk``'s guard: enough energy for a leg."""
    return energy_of(graph) >= WALK_COST


def too_tired(graph: ExecutableGraph) -> bool:
    """``Eat``'s guard: not enough energy for a leg."""
    return energy_of(graph) < WALK_COST


def too_tired_with_nothing_to_eat(graph: ExecutableGraph) -> bool:
    """``Rest``'s guard: not enough energy for a leg, and the pack empty."""
    return too_tired(graph) and not graph.place("Pack").tokens


def walk(leg: str, energy: Energy) -> str:
    energy.level -= WALK_COST
    return leg


def eat(snack: str, energy: Energy) -> str:
    energy.level += SNACK_GAIN
    return snack


def rest(bench: str, energy: Energy) -> str:
    energy.level += REST_GAIN
    return bench


def build_hike(record_events: bool = True) -> ExecutableGraph:
    """The hiker at the start of the day, with three energy. The groups come in
    the order of their first mention: Walk, Eat, Rest."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Energy", Energy, [Energy(level=3)]),
        ListPlaceNode("Trail", str, list(LEGS), group="Walk"),
        ListPlaceNode("Walked", str, group="Walk"),
        ListPlaceNode("Pack", str, list(SNACKS), group="Eat"),
        ListPlaceNode("Eaten", str, group="Eat"),
        ListPlaceNode("Benches", str, list(BENCHES), group="Rest"),
        ListPlaceNode("Rested At", str, group="Rest"),
        FunctionTransitionNode("Walk", walk, guard=can_walk, group="Walk"),
        ArgumentEdgeToTransition("Trail", "Walk", "leg"),
        MutateEdge("Energy", "Walk", "energy"),
        ReturnedEdgeFromTransition("Walk", "Walked"),
        FunctionTransitionNode("Eat", eat, guard=too_tired, group="Eat"),
        ArgumentEdgeToTransition("Pack", "Eat", "snack"),
        MutateEdge("Energy", "Eat", "energy"),
        ReturnedEdgeFromTransition("Eat", "Eaten"),
        FunctionTransitionNode("Rest", rest, guard=too_tired_with_nothing_to_eat, group="Rest"),
        ArgumentEdgeToTransition("Benches", "Rest", "bench"),
        MutateEdge("Energy", "Rest", "energy"),
        ReturnedEdgeFromTransition("Rest", "Rested At"),
    ], record_events=record_events)


@petri_net(name="hike", description="A hiker walks, eats and rests, every concern drawing on one store of energy")
def hike() -> ExecutableGraph:
    """The hike as the protocol runner serves it, built afresh at each load and reset."""
    return build_hike()


async def step(graph: ExecutableGraph) -> Optional[str]:
    """One firing, the engine's own choice; the transition's name, or ``None``
    once nothing can fire."""
    return await ExecutableGraphOperations.fire_one_transition(graph, *ExecutableGraphOperations.maps(graph))


async def day(graph: ExecutableGraph, budget: int = 100) -> list[str]:
    """Step until nothing can fire or ``budget`` firings have been made; returns
    what fired, in order. The day comes to rest by itself."""
    fired = []
    for _ in range(budget):
        name = await step(graph)
        if name is None:
            break
        fired.append(name)
    return fired


def main() -> None:
    graph = build_hike()
    number = 0
    while (name := asyncio.run(step(graph))) is not None:
        number += 1
        print(f"{number:3}. {name}, energy {energy_of(graph)}")
    for group in graph.groups:
        members = [node.name for node in (*graph.places, *graph.transitions) if node.group == group]
        print(f"group {group}: {', '.join(members)}")


if __name__ == "__main__":
    main()
