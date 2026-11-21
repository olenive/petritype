"""Levels: a world whose places hold nets of their own (subnet places).

A small world with two levels built from one template. The world has a
``Door``, where heroes wait, and ``Outside``, where they end up; between them
stand two subnet places, ``Level 1`` and ``Level 2``, each holding a net built
by the template ``Level``. A level has two port places, ``Lobby`` and ``Exit
Hall``, through which the world reaches it, and a place of its own, ``Arena``,
which the world cannot see. A hero enters a level through its lobby, fights in
its arena and leaves through its exit hall:

- the world's ``Enter`` takes a hero from ``Door`` into level 1's lobby;
- inside a level, ``Fight`` takes a hero from the lobby into the arena and
  gives them the level's ``reward`` in experience, and ``Win`` takes them on to
  the exit hall;
- the world's ``Climb`` takes a hero from level 1's exit hall into level 2's
  lobby, and ``Leave`` from level 2's exit hall to ``Outside``.

The world never reaches into a level any other way: its edges name a port by
the pair ``Port("Level 1", "Lobby")``, and the level's arena is out of its reach.
A hero crosses a border only by a firing of the world, which takes it from one
place and puts it into another, as every firing does. While a hero rests in a
port both nets see the same token, with the same id, minted by the level.

Both levels keep the default stepping rule, ``"inner_first"``: whenever the
world fires, every level is at rest. So the first hero clears level 1 before the
second enters it, and every hero has cleared level 1 before the first climbs,
since the world's own transitions are tried in the order they are declared. Run
it to watch the journey, with the world's log showing each level's firings under
the path of the level they happened in::

    uv run python examples/subnet_places/levels.py

or serve the world as a net process, ``levels-world``, which a viewer can host
and open each level of (PROTOCOL.md §12.7)::

    python -m petritype.runner --module examples.subnet_places.levels
"""

from __future__ import annotations

import asyncio
from typing import Optional, Union

from pydantic import BaseModel

from petritype import petri_net
from petritype.core.events import Fired, InnerFiring, Nested, PathStep
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.core.nested import Port, SubnetPlaceNode
from petritype.core.serialisation import Templates


class Hero(BaseModel):
    """A hero, and the experience they have gained so far."""

    name: str
    xp: int = 0


# --- the subnet: one template, two levels ---------------------------------------------------


def fight(hero: Hero, reward: int) -> Hero:
    """The hero fights in the arena and gains the level's reward. ``reward`` is the
    level's net parameter of that name, handed to the body because it declares a
    parameter so named and nothing else supplies it."""
    return hero.model_copy(update={"xp": hero.xp + reward})


def win(hero: Hero) -> Hero:
    """The hero leaves the arena for the exit hall."""
    return hero


@Templates.template("Level")
def build_level(reward: int = 1) -> ExecutableGraph:
    """A level: ``Lobby`` and ``Exit Hall`` are its ports, ``Arena`` is its own.
    ``reward`` is how much experience a fight here gives."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Lobby", Hero, port=True),
        ListPlaceNode("Arena", Hero),
        ListPlaceNode("Exit Hall", Hero, port=True),
        FunctionTransitionNode("Fight", fight),
        ArgumentEdgeToTransition("Lobby", "Fight", "hero"),
        ReturnedEdgeFromTransition("Fight", "Arena"),
        FunctionTransitionNode("Win", win),
        ArgumentEdgeToTransition("Arena", "Win", "hero"),
        ReturnedEdgeFromTransition("Win", "Exit Hall"),
    ], params={"reward": reward}, template="Level")


# --- the world --------------------------------------------------------------------------------


def moves(hero: Hero) -> Hero:
    """A hero moves from one place to another, unchanged."""
    return hero


# The seed's heroes, waiting at the door in the order they arrived.
HEROES = ("Ada", "Bram", "Cato")


def build_world(record_events: bool = True) -> ExecutableGraph:
    """The world: three heroes at the door, two levels from one template, level 2
    giving twice level 1's reward, and nobody outside yet."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", Hero, [Hero(name=name) for name in HEROES]),
        SubnetPlaceNode("Level 1", build_level(reward=1)),
        SubnetPlaceNode("Level 2", build_level(reward=2)),
        ListPlaceNode("Outside", Hero),
        FunctionTransitionNode("Enter", moves),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", Port("Level 1", "Lobby")),
        FunctionTransitionNode("Climb", moves),
        ArgumentEdgeToTransition(Port("Level 1", "Exit Hall"), "Climb", "hero"),
        ReturnedEdgeFromTransition("Climb", Port("Level 2", "Lobby")),
        FunctionTransitionNode("Leave", moves),
        ArgumentEdgeToTransition(Port("Level 2", "Exit Hall"), "Leave", "hero"),
        ReturnedEdgeFromTransition("Leave", "Outside"),
    ], record_events=record_events)


@petri_net(
    name="levels-world",
    description="Heroes clear two levels built from one template, entering by the lobby and leaving by the exit hall",
)
def world() -> ExecutableGraph:
    """The world as the protocol runner serves it, built afresh at each load and reset."""
    return build_world()


# --- stepping ---------------------------------------------------------------------------------


async def step(world: ExecutableGraph) -> Optional[Union[str, InnerFiring]]:
    """One firing, the engine's own choice: a level's, while one has anything to
    fire (both are inner first), else the world's first fireable transition in
    declared order. Returns what fired: the world's transition by name, a level's
    as an ``InnerFiring`` (``InnerFiring((PathStep("Level 1"),), "Fight")``), or
    ``None`` once nothing can fire."""
    return await ExecutableGraphOperations.fire_one_transition(world, *ExecutableGraphOperations.maps(world))


def where(path: tuple[PathStep, ...]) -> str:
    """Where an inner net is, in words: ``"Level 1"`` for a subnet, one clause
    per level."""
    return ", in ".join(
        step.place if step.is_subnet else f"the {step.net or 'net'} of token {step.token} in {step.place}"
        for step in reversed(path)
    )


def label(fired: Union[str, InnerFiring]) -> str:
    """What a step fired, in words: the world's transition by name, a level's as
    ``"Fight, in Level 1"``."""
    if isinstance(fired, InnerFiring):
        return f"{fired.transition}, in {where(fired.path)}"
    return fired


async def journey(world: ExecutableGraph, budget: int = 100) -> list:
    """Step until nothing can fire or ``budget`` firings have been made; returns
    the world's event log. The world comes to rest by itself, every hero outside."""
    for _ in range(budget):
        if await step(world) is None:
            break
    return world.drain_events()


def describe_log(events: list) -> list[str]:
    lines = []
    for event in events:
        if isinstance(event, Nested) and isinstance(event.event, Fired):
            lines.append(f"{event.event.transition}, in {where(event.path)}")
        elif isinstance(event, Fired):
            lines.append(event.transition)
    return lines


def main() -> None:
    world = build_world()
    events = asyncio.run(journey(world))
    for number, line in enumerate(describe_log(events), 1):
        print(f"{number:3}. {line}")
    print("outside:", ", ".join(f"{hero.name} ({hero.xp} xp)" for hero in world.place_named("Outside").tokens))


if __name__ == "__main__":
    main()
