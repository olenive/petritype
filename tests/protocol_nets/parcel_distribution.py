"""The shared conformance net ``parcel-distribution`` (PROTOCOL.md §11).

A twin of the Rust port ``petritype-rs/petritype/examples/parcel_distribution.rs``,
not of the public marimo example ``examples/toy/parcel_distribution.py`` it was
ported from: that one is perpetual, paced by wall-clock guards and random, and a
transcript has to be reproducible. Place names, transition names, seeds, edge
kinds, output slots, the deferral rule and the guard are the Rust example's, so
that a transcript recorded against either runtime replays against the other.

``Receiving -Sort-> Sorting -Stage-> Staging Area -Dispatch Truck-> Dispatched``,
seeded with eight parcels. ``Dispatch Truck`` takes the whole platform at once
(a ``take_all`` edge) and writes the parcels it leaves behind back to it (slot
1); every fourth parcel misses its first truck. The truck waits for at least two
parcels, unless nothing more is coming.

``Parcel`` is a pydantic model, so every place is ``serializable`` (the Rust
side needs ``Serialize``/``Deserialize`` on its ``Parcel`` and every place
marked ``serializable`` to agree).
"""

from pydantic import BaseModel

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)


class Parcel(BaseModel):
    """A parcel, and how many trucks have already gone without it."""

    id: int
    deferrals: int = 0


def sort(parcel: Parcel) -> Parcel:
    return parcel


def stage(parcel: Parcel) -> Parcel:
    return parcel


def dispatch_truck(parcels: list[Parcel]) -> tuple[list[Parcel], list[Parcel]]:
    """Slot 0 leaves on the truck, slot 1 waits for the next one. Every fourth
    parcel misses its first truck (the public example rolls a die here)."""
    gone, left = [], []
    for parcel in parcels:
        if parcel.id % 4 == 0 and parcel.deferrals == 0:
            left.append(parcel.model_copy(update={"deferrals": parcel.deferrals + 1}))
        else:
            gone.append(parcel)
    return gone, left


def _count(graph: ExecutableGraph, place: str) -> int:
    return next(len(node.tokens) for node in graph.places if node.name == place)


def truck_is_worth_sending(graph: ExecutableGraph) -> bool:
    """A truck waits for a load, unless nothing more is coming."""
    return _count(graph, "Staging Area") >= 2 or (
        _count(graph, "Receiving") == 0 and _count(graph, "Sorting") == 0
    )


@petri_net(name="parcel-distribution", description="Sort, stage and dispatch parcels")
def parcel_distribution() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Receiving", Parcel, [Parcel(id=i) for i in range(1, 9)]),
        ListPlaceNode("Sorting", Parcel),
        ListPlaceNode("Staging Area", Parcel),
        ListPlaceNode("Dispatched", Parcel),
        FunctionTransitionNode("Sort", sort),
        FunctionTransitionNode("Stage", stage),
        FunctionTransitionNode("Dispatch Truck", dispatch_truck, guard=truck_is_worth_sending),
        ArgumentEdgeToTransition("Receiving", "Sort", "parcel"),
        ReturnedEdgeFromTransition("Sort", "Sorting"),
        ArgumentEdgeToTransition("Sorting", "Stage", "parcel"),
        ReturnedEdgeFromTransition("Stage", "Staging Area"),
        ArgumentEdgeToTransition("Staging Area", "Dispatch Truck", "parcels"),
        ReturnedEdgeFromTransition("Dispatch Truck", "Dispatched", 0),
        ReturnedEdgeFromTransition("Dispatch Truck", "Staging Area", 1),
    ])
