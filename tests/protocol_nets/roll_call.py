"""The shared conformance net ``roll-call`` (PROTOCOL.md §11, §5.1, §13.5): a
roll call that reads the squads in a barracks without taking them, so that a
transcript shows the three read edges on a place whose tokens carry nets: a
``read_all`` edge, a ``read`` edge and a ``read_where`` edge bound to another
input. No squad moves, keeps another id or has its net copied, and no line
tells of one.

Its names, places, transitions, keys, template and seed are fixed in
PROTOCOL.md §11, so that the Rust runner can build the same net from the text
alone and replay ``tests/protocol/17_read_edges.json``. A squad is a net built
from the template ``Squad``, one place ``Members`` of strings, and then given
its members; its leader is its first member and its size its number of members,
and a roll line writes it as ``<leader> <size>``. Each transition takes the
front of its own place of requests and reads the squads, and produces one line
into ``Roll``. Each declares its take edge first.

The template is registered, so a squad has a saved form and ``Barracks`` is
serializable through it, as the ``stepping-rules`` net registers its own.
"""

from pydantic import BaseModel

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    NestedNets,
    ReadAllEdge,
    ReadEdge,
    ReturnedEdgeFromTransition,
)
from petritype.core.keys import Clause, Filter, ReadWhereEdge
from petritype.core.serialisation import Templates


class Inspection(BaseModel):
    """An inspection of the squad whose leader it names."""

    leader: str


@Templates.template("Squad")
def squad_template() -> ExecutableGraph:
    """The ``Squad`` template: one place, ``Members``, of strings, holding no
    tokens, and no transitions. No parameters."""
    return ExecutableGraphOperations.construct_graph([ListPlaceNode("Members", str)])


def squad(*members: str) -> ExecutableGraph:
    """A squad built from the template, then given its members."""
    net = squad_template()
    NestedNets.put(net, "Members", list(members))
    return net


def members(squad: ExecutableGraph) -> list[str]:
    return list(squad.place_named("Members").tokens)


def leader(squad: ExecutableGraph) -> str:
    """The squad's first member, or ``""`` for an empty squad."""
    return next(iter(members(squad)), "")


def written(squad: ExecutableGraph) -> str:
    """A squad as a roll line writes it: ``<leader> <size>``."""
    return f"{leader(squad)} {len(members(squad))}"


def call_the_roll(caller: str, squads: list[ExecutableGraph]) -> str:
    """Every squad, oldest first, or ``nobody``."""
    return f"{caller}: " + (", ".join(written(squad) for squad in squads) or "nobody")


def check_the_front(checker: str, squad: ExecutableGraph) -> str:
    """The front squad."""
    return f"{checker}: {written(squad)}"


def inspect(inspection: Inspection, squad: ExecutableGraph) -> str:
    """The squad the inspection names."""
    return f"Inspected {written(squad)}"


@petri_net(name="roll-call", description="A roll call reads the squads in the barracks without taking them")
def roll_call() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Barracks", ExecutableGraph, [squad("Ada", "Ben"), squad("Cy")], nested=True,
                      keys={"leader": leader}),
        ListPlaceNode("Calls", str),
        ListPlaceNode("Checks", str),
        ListPlaceNode("Inspections", Inspection, keys=("leader",)),
        ListPlaceNode("Roll", str),
        FunctionTransitionNode("Call the Roll", call_the_roll),
        ArgumentEdgeToTransition("Calls", "Call the Roll", "caller"),
        ReadAllEdge("Barracks", "Call the Roll", "squads"),
        ReturnedEdgeFromTransition("Call the Roll", "Roll"),
        FunctionTransitionNode("Check the Front", check_the_front),
        ArgumentEdgeToTransition("Checks", "Check the Front", "checker"),
        ReadEdge("Barracks", "Check the Front", "squad"),
        ReturnedEdgeFromTransition("Check the Front", "Roll"),
        FunctionTransitionNode("Inspect", inspect),
        ArgumentEdgeToTransition("Inspections", "Inspect", "inspection"),
        ReadWhereEdge("Barracks", "Inspect", "squad",
                      where=Filter((Clause("leader", input="Inspections", input_key="leader"),))),
        ReturnedEdgeFromTransition("Inspect", "Roll"),
    ])
