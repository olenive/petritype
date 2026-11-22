"""The shared conformance net ``cellar`` (PROTOCOL.md §11, §12.7, §13): bottles
on a rack in a cellar, a subnet place whose one port the house reaches from
outside, so that a transcript shows a port's tokens read, changed and filtered
by the net around the subnet.

Its names, places, transition, key, template and seed are fixed in PROTOCOL.md
§11, so that the Rust runner can build the same net from the text alone and
replay ``tests/protocol/13_ports_from_outside.json``. A ``Bottle`` is ``{name,
years}``, and every place of bottles declares one key, ``years``. The house
keeps one bottle on its ``Table`` and reaches the cellar's ``Rack``, its port,
through ``Age``, which mutates the bottle at the front of the rack, adding a
year, and produces nothing. The cellar's net has no transition of its own, so a
deep step always comes to ``Age`` while the rack holds a bottle.

The cellar's net is stamped with the template name ``Cellar`` by
``construct_graph(template=...)`` and not registered by that name, as the
``levels`` net does with ``Level``: nothing in the protocol rebuilds a subnet by
its template's name (§12.5).
"""

from pydantic import BaseModel

from petritype import petri_net
from petritype.core.events import Port
from petritype.core.executable_graph_components import (
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    SubnetPlaceNode,
)


class Bottle(BaseModel):
    """A bottle, and how many years it has aged."""

    name: str
    years: int


KEYS = ("years",)


def age(bottle: Bottle) -> None:
    """One more year for the bottle at the front of the rack, in place."""
    bottle.years += 1


def cellar() -> ExecutableGraph:
    """The ``Cellar`` template: one place, ``Rack``, its port, holding one bottle,
    ``{"name": "Rioja", "years": 0}``, and no transition. No parameters."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Rack", Bottle, [Bottle(name="Rioja", years=0)], keys=KEYS, port=True),
    ], template="Cellar")


@petri_net(name="cellar", description="Bottles age on a rack in a cellar that the house reaches through its port")
def house() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Table", Bottle, [Bottle(name="Claret", years=4)], keys=KEYS),
        SubnetPlaceNode("Cellar", cellar()),
        FunctionTransitionNode("Age", age),
        MutateEdge(Port("Cellar", "Rack"), "Age", "bottle"),
    ])
