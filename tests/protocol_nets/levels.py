"""The shared conformance net ``levels`` (PROTOCOL.md §11, §12.7): a small world
with two levels built from one template, heroes entering a level through one
port place and leaving through another, so that a transcript shows the subnet
places, their ports and the two views of a port.

Its names, places, transitions, guards, template and seed are fixed in
PROTOCOL.md §11, so that the Rust runner can build the same net from the text
alone and replay ``tests/protocol/09_subnet_places.json``. It is a cut-down
``examples/subnet_places/levels.py``: one hero, and a level that a hero walks
into, fights in once and leaves.

The world's places, in this order: ``Door``, the subnet places ``Level 1`` and
``Level 2``, and ``Outside``. A level's: ``Lobby`` (a port), ``Arena`` and ``Exit
Hall`` (a port). ``Enter`` takes a hero from the door into level 1's lobby,
``Climb`` from level 1's exit hall into level 2's lobby, and ``Leave`` from level
2's exit hall to ``Outside``. Inside a level, ``Walk In`` takes a hero from the
lobby into the arena, ``Fight`` takes them from the arena back into it, having
fought, and ``Win`` takes a hero who has fought on to the exit hall. The two
guards read the hero at the front of the arena, so at most one of ``Fight`` and
``Win`` is fireable at a time. Every body hands its hero on, changing at most
``fought``, and none fails.

The levels' net is stamped with the template name ``Level`` by
``construct_graph(template=...)`` and not registered by that name, since
``examples/subnet_places/levels.py`` registers a ``Level`` of its own and the two
modules are imported into one test process; nothing in the protocol rebuilds a
subnet by its template's name (§12.5).
"""

from typing import Optional

from pydantic import BaseModel

from petritype import petri_net
from petritype.core.events import Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    SubnetPlaceNode,
)


class Hero(BaseModel):
    """A hero, and whether they have fought in the level they are in."""

    name: str
    fought: bool


def walk_in(hero: Hero) -> Hero:
    """Into the arena, not having fought in this level yet."""
    return hero.model_copy(update={"fought": False})


def fight(hero: Hero) -> Hero:
    return hero.model_copy(update={"fought": True})


def moves(hero: Hero) -> Hero:
    """A hero moves on, unchanged."""
    return hero


def front_of_arena(graph: ExecutableGraph) -> Optional[Hero]:
    tokens = graph.place_named("Arena").tokens
    return tokens[0] if tokens else None


def has_not_fought(graph: ExecutableGraph) -> bool:
    """``Fight``'s guard: the hero at the front of the arena has not fought."""
    hero = front_of_arena(graph)
    return hero is not None and not hero.fought


def has_fought(graph: ExecutableGraph) -> bool:
    """``Win``'s guard: the hero at the front of the arena has fought."""
    hero = front_of_arena(graph)
    return hero is not None and hero.fought


def level() -> ExecutableGraph:
    """The ``Level`` template: ``Lobby -Walk In-> Arena -Fight-> Arena -Win->
    Exit Hall``, with ``Lobby`` and ``Exit Hall`` its ports. No parameters."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Lobby", Hero, port=True),
        ListPlaceNode("Arena", Hero),
        ListPlaceNode("Exit Hall", Hero, port=True),
        FunctionTransitionNode("Walk In", walk_in),
        ArgumentEdgeToTransition("Lobby", "Walk In", "hero"),
        ReturnedEdgeFromTransition("Walk In", "Arena"),
        FunctionTransitionNode("Fight", fight, guard=has_not_fought),
        ArgumentEdgeToTransition("Arena", "Fight", "hero"),
        ReturnedEdgeFromTransition("Fight", "Arena"),
        FunctionTransitionNode("Win", moves, guard=has_fought),
        ArgumentEdgeToTransition("Arena", "Win", "hero"),
        ReturnedEdgeFromTransition("Win", "Exit Hall"),
    ], template="Level")


@petri_net(name="levels", description="Heroes enter two levels through one port and leave through another")
def levels() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", Hero, [Hero(name="Ada", fought=False)]),
        SubnetPlaceNode("Level 1", level()),
        SubnetPlaceNode("Level 2", level()),
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
    ])
