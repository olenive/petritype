"""The shared conformance net ``groups`` (PROTOCOL.md §5.1, §11): a net with two
groups, a place in no group, a transition in each group, and a subnet place in
one of the groups whose subnet declares a group of its own, so that a transcript
shows ``topology.groups`` and ``group`` for a net and for an inner net.

Its names, places, transitions, groups, template and seed are fixed in
PROTOCOL.md §11, so that the Rust runner can build the same net from the text
alone and replay ``tests/protocol/11_groups.json``. Letters are sorted in the
group ``Sorting`` and packed in the group ``Packing`` into the port ``Tray`` of
the subnet place ``Box``, which seals them in its own group ``Sealing``. Every
body hands its letter on unchanged, and none fails.

The box's net is stamped with the template name ``Box`` by
``construct_graph(template=...)`` and not registered by that name, as the
``levels`` net does with ``Level``: nothing in the protocol rebuilds a subnet by
its template's name (§12.5).
"""

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


def moves(letter: str) -> str:
    """A letter moves on, unchanged."""
    return letter


def box() -> ExecutableGraph:
    """The ``Box`` template: ``Tray -Seal-> Sealed``, with ``Tray`` its port, and
    ``Tray`` and ``Seal`` in its group ``Sealing``. No parameters, no tokens."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Tray", str, port=True, group="Sealing"),
        ListPlaceNode("Sealed", str),
        FunctionTransitionNode("Seal", moves, group="Sealing"),
        ArgumentEdgeToTransition("Tray", "Seal", "letter"),
        ReturnedEdgeFromTransition("Seal", "Sealed"),
    ], template="Box")


@petri_net(
    name="groups",
    description="Letters are sorted in one group and packed in another, into a box that seals them in a group of its own",
)
def groups() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Inbox", str, ["Letter"]),
        ListPlaceNode("Sorted", str, group="Sorting"),
        SubnetPlaceNode("Box", box(), group="Packing"),
        FunctionTransitionNode("Sort", moves, group="Sorting"),
        ArgumentEdgeToTransition("Inbox", "Sort", "letter"),
        ReturnedEdgeFromTransition("Sort", "Sorted"),
        FunctionTransitionNode("Pack", moves, group="Packing"),
        ArgumentEdgeToTransition("Sorted", "Pack", "letter"),
        ReturnedEdgeFromTransition("Pack", Port("Box", "Tray")),
    ])
