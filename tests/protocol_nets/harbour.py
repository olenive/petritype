"""The shared conformance net ``harbour`` (PROTOCOL.md §11, §12.1, §12.6, §12.7):
ships that are nets of their own, each holding a subnet place, so that a
transcript shows a subnet inside a net that a token carries: its path through
the token's step, its nested id, reading and stepping it, its path as the token
moves, and its end when the token is consumed.

Its names, places, transitions, templates and seed are fixed in PROTOCOL.md
§11, so that the Rust runner can build the same net from the text alone and
replay ``tests/protocol/14_subnet_in_a_token.json``. The outer net's places,
``Quay`` and ``Sea``, hold ships, and a ship is a net, so each holds one inner
net named with the empty string. A ship stows the crate on its ``Deck`` into the
port ``Hatch`` of its subnet place ``Hold``, whose own transition settles it into
its ``Bay``. The outer net's ``Sail`` takes the ship from the quay and puts it
to sea unchanged, and ``Scrap`` takes it from the sea and puts nothing back.
Every body hands its token on unchanged, and none fails.

The ship's and the hold's nets are stamped with the template names ``Ship`` and
``Hold`` by ``construct_graph(template=...)`` and not registered by those names,
as the ``levels`` net does with ``Level``: the transcript never rebuilds a net
from its saved form (§12.5).
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


def moves(crate: str) -> str:
    """A crate moves on, unchanged."""
    return crate


def sail(ship: ExecutableGraph) -> ExecutableGraph:
    """A ship puts to sea, unchanged: its nets go with it."""
    return ship


def scrap(ship: ExecutableGraph) -> None:
    """A ship is broken up: nothing is put back."""
    return None


def hold() -> ExecutableGraph:
    """The ``Hold`` template: ``Hatch -Settle-> Bay``, with ``Hatch`` its port.
    No parameters, no tokens."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Hatch", str, port=True),
        ListPlaceNode("Bay", str),
        FunctionTransitionNode("Settle", moves),
        ArgumentEdgeToTransition("Hatch", "Settle", "crate"),
        ReturnedEdgeFromTransition("Settle", "Bay"),
    ], template="Hold")


def ship() -> ExecutableGraph:
    """The ``Ship`` template: ``Deck``, holding one crate, ``"Crate"``, and the
    subnet place ``Hold``, a ``Hold``, inner first, with ``Stow`` from the deck
    into the hold's port ``Hatch``. No parameters."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Deck", str, ["Crate"]),
        SubnetPlaceNode("Hold", hold()),
        FunctionTransitionNode("Stow", moves),
        ArgumentEdgeToTransition("Deck", "Stow", "crate"),
        ReturnedEdgeFromTransition("Stow", Port("Hold", "Hatch")),
    ], template="Ship")


@petri_net(name="harbour", description="A ship stows its crate in its hold, sails and is scrapped")
def harbour() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Quay", ExecutableGraph, [ship()], nested=True),
        ListPlaceNode("Sea", ExecutableGraph, nested=True),
        FunctionTransitionNode("Sail", sail),
        ArgumentEdgeToTransition("Quay", "Sail", "ship"),
        ReturnedEdgeFromTransition("Sail", "Sea"),
        FunctionTransitionNode("Scrap", scrap),
        ArgumentEdgeToTransition("Sea", "Scrap", "ship"),
    ])
