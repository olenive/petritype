"""The shared conformance net ``trays`` (PROTOCOL.md §11, §5.1, §12.7): a unit
that is a net of its own, with two port places, its trays, so that a transcript
shows ``port`` in the topology of a net that a token carries, and outer edges
that declare which of those ports they reach (``reaches``).

Its names, places, transitions, template, guard and seed are fixed in
PROTOCOL.md §11, so that the Rust runner can build the same net from the text
alone and replay ``tests/protocol/18_ports_on_a_token.json``. A unit is a net
built from the template ``Unit``: an order put into its ``Orders In`` is fired
by its ``Fire`` into its ``Shots Out`` as ``"Shot: <order>"``. The net's
``Hand Over`` takes an order and puts it into the front unit's ``Orders In``,
and its ``Collect`` takes every shot out of the front unit's ``Shots Out`` into
``Reports``; each names in its ``reaches`` the port its body writes, as a body
writing a port of a closed net it holds must since 2026-11-27. ``Orders`` is the
net's own port, where a transcript ``inject``s the order: since that day every
net not declared open is closed, the loaded net included.

The template is registered, so a unit has a saved form and ``Field`` is
serializable through it, as the ``roll-call`` net registers its own.
"""

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    NestedNets,
    ReturnedEdgeFromTransition,
)
from petritype.core.serialisation import Templates


def fire(order: str) -> str:
    """A unit fires the order it was handed."""
    return f"Shot: {order}"


@Templates.template("Unit")
def unit_template() -> ExecutableGraph:
    """The ``Unit`` template: the ports ``Orders In`` and ``Shots Out``, of
    strings, holding no tokens, and ``Fire`` from the one to the other. No
    parameters."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders In", str, port=True),
        ListPlaceNode("Shots Out", str, port=True),
        FunctionTransitionNode("Fire", fire),
        ArgumentEdgeToTransition("Orders In", "Fire", "order"),
        ReturnedEdgeFromTransition("Fire", "Shots Out"),
    ])


def hand_over(order: str, unit: ExecutableGraph) -> None:
    """The order goes into the unit's ``Orders In``."""
    NestedNets.put(unit, "Orders In", [order])


def collect(unit: ExecutableGraph) -> list[str]:
    """Every shot comes out of the unit's ``Shots Out``, oldest first."""
    return NestedNets.take(unit, "Shots Out", len(unit.place_named("Shots Out").tokens))


def a_shot_is_waiting(graph: ExecutableGraph) -> bool:
    """The front unit's ``Shots Out`` holds at least one shot."""
    units = graph.place_named("Field").tokens
    return bool(units) and bool(units[0].place_named("Shots Out").tokens)


@petri_net(name="trays", description="A unit is handed an order through one tray and its shots are collected from another")
def trays() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders", str, port=True),
        ListPlaceNode("Field", ExecutableGraph, [unit_template()], nested=True),
        ListPlaceNode("Reports", str),
        FunctionTransitionNode("Hand Over", hand_over),
        ArgumentEdgeToTransition("Orders", "Hand Over", "order"),
        MutateEdge("Field", "Hand Over", "unit", reaches=("Orders In",)),
        FunctionTransitionNode("Collect", collect, guard=a_shot_is_waiting),
        MutateEdge("Field", "Collect", "unit", reaches=("Shots Out",)),
        ReturnedEdgeFromTransition("Collect", "Reports"),
    ])
