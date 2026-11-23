"""The shared conformance net ``couriers`` (PROTOCOL.md §11, §13.5): orders are
loaded onto the courier they name, and a call sends that courier out, so that a
transcript shows keyed edges that choose their token by another input of the
same firing, in their take and mutate forms.

Its names, places, transitions, keys and seed are fixed in PROTOCOL.md §11, so
that the Rust runner can build the same net from the text alone and replay
``tests/protocol/16_keyed_by_input.json``. ``Load`` takes the front order and
hands its body, live, the courier whose ``name`` is the order's ``courier``
(a ``MutateWhereEdge`` bound to the order); ``Dispatch`` takes the front call
and takes the courier the call names (a ``TakeWhereEdge`` bound to the call).
An order whose courier is not there waits, and the orders behind it go first.
``Dispatch`` fails for a courier with nothing to carry, and the net restores what
a failed firing took: this library sets that for the whole net
(``restore_tokens_on_failure``), the Rust core for each transition, so the Rust
twin sets it on both. Each transition declares its source edge before its bound
edge, so its ``consumed`` lists the source first.
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
from petritype.core.keys import Clause, Filter, MutateWhereEdge, TakeWhereEdge


class Courier(BaseModel):
    """A courier, and how many parcels it carries."""

    name: str
    parcels: int


class Order(BaseModel):
    """An item, and the courier it is for."""

    item: str
    courier: str


class Call(BaseModel):
    """A call for a courier to set out."""

    courier: str


class NothingToCarry(Exception):
    """A courier called out with no parcel."""


def load(order: Order, courier: Courier) -> Order:
    """One more parcel for the courier the order names, in place; the order goes on unchanged."""
    courier.parcels += 1
    return order


def dispatch(call: Call, courier: Courier) -> Courier:
    """The courier the call names sets out, unless it has nothing to carry."""
    if courier.parcels == 0:
        raise NothingToCarry(f"{courier.name} has nothing to carry")
    return courier


# The courier whose name is the courier key of the token taken from ``place``.
def named_by(place: str) -> Filter:
    return Filter((Clause("name", input=place, input_key="courier"),))


@petri_net(name="couriers", description="Orders are loaded onto the courier they name, and a call sends that courier out")
def couriers() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Couriers", Courier, [
            Courier(name="Ada", parcels=0), Courier(name="Ben", parcels=0), Courier(name="Dot", parcels=0),
        ], keys=("name",)),
        ListPlaceNode("Orders", Order, [
            Order(item="Lamp", courier="Cleo"), Order(item="Book", courier="Ben"), Order(item="Kite", courier="Ada"),
        ], keys=("courier",)),
        ListPlaceNode("Calls", Call, keys=("courier",)),
        ListPlaceNode("Loaded", Order),
        ListPlaceNode("On Road", Courier),
        FunctionTransitionNode("Load", load),
        ArgumentEdgeToTransition("Orders", "Load", "order"),
        MutateWhereEdge("Couriers", "Load", "courier", where=named_by("Orders")),
        ReturnedEdgeFromTransition("Load", "Loaded"),
        FunctionTransitionNode("Dispatch", dispatch),
        ArgumentEdgeToTransition("Calls", "Dispatch", "call"),
        TakeWhereEdge("Couriers", "Dispatch", "courier", where=named_by("Calls")),
        ReturnedEdgeFromTransition("Dispatch", "On Road"),
    ], restore_tokens_on_failure=True)
