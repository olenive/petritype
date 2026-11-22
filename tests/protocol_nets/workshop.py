"""The shared conformance net ``workshop`` (PROTOCOL.md §6.3, §7.1, §11, §12.7):
pots glazed on a bench and unloaded from a kiln through its port, where a
cracked pot fails the firing that holds it, so that a transcript shows a failed
firing that loses its tokens, one that gives them back, and both for a firing
that took its token from a port place of a subnet.

Its names, places, transitions, template, load parameter and seed are fixed in
PROTOCOL.md §11, so that the Rust runner can build the same net from the text
alone and replay ``tests/protocol/12_failures.json``. ``Bench`` and ``Shelf``
hold pots, which are strings; ``Kiln`` is a subnet place whose subnet has one
place, its port ``Hatch``, and no transition. ``Glaze`` takes a pot from the
bench and ``Unload`` takes one from the kiln's hatch, each putting it on the
shelf unchanged, and each body fails for a pot whose text begins with
``"Cracked"``. The load parameter ``restore`` says whether a failed firing of the
net puts its tokens back: this library sets it for the whole net
(``restore_tokens_on_failure``), the Rust core for each transition, so the Rust
twin gives both transitions the same setting. Every place starts empty; the
transcript brings each pot in with ``inject``, so that one transition at most is
fireable at a time.

The kiln's net is stamped with the template name ``Kiln`` by
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


class Cracked(Exception):
    """A pot that breaks in the hands of the firing that holds it."""


def handle(pot: str) -> str:
    """A whole pot moves on unchanged; a cracked one fails the firing."""
    if pot.startswith("Cracked"):
        raise Cracked(f"{pot} broke")
    return pot


def kiln() -> ExecutableGraph:
    """The ``Kiln`` template: one place, ``Hatch``, its port, and no transition.
    No parameters, no tokens."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Hatch", str, port=True),
    ], template="Kiln")


@petri_net(
    name="workshop",
    description="Pots are glazed on a bench and unloaded from a kiln, and a cracked pot fails the firing that holds it",
)
def workshop(restore: bool = False) -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Bench", str),
        SubnetPlaceNode("Kiln", kiln()),
        ListPlaceNode("Shelf", str),
        FunctionTransitionNode("Glaze", handle),
        ArgumentEdgeToTransition("Bench", "Glaze", "pot"),
        ReturnedEdgeFromTransition("Glaze", "Shelf"),
        FunctionTransitionNode("Unload", handle),
        ArgumentEdgeToTransition(Port("Kiln", "Hatch"), "Unload", "pot"),
        ReturnedEdgeFromTransition("Unload", "Shelf"),
    ], restore_tokens_on_failure=restore)
