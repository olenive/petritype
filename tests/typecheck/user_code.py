"""Code a user writes against the public API, as the README teaches it, which a
type checker must accept.

``just typecheck`` (pyright, in CI) checks this file in pyright's standard mode,
and checks none of the library's own code: it is the promise that ``py.typed``
makes, that a program written this way type-checks in its author's editor. A
change to the public API that makes a checker reject such code fails here; a
use the README gains belongs here too.

It is checked, not run: pytest does not collect it, and the wheel does not hold
it. It is valid code all the same, and ``python tests/typecheck/user_code.py``
runs it.

``ExecutableGraph.place_named`` returns ``None`` for a name the net does not
have, so this code narrows the place before it reads its tokens.
"""

import asyncio
from typing import Any

from pydantic import BaseModel

from petritype import (
    ArgumentEdgeToTransition,
    ClosedNetError,
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    ExecutionMode,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    MutateWhereEdge,
    PetritypeError,
    ReadAllEdge,
    ReadEdge,
    ReadWhereEdge,
    ReturnedEdgeFromTransition,
    RunContext,
    RunSummary,
    Runner,
    SnapshotEdge,
    SubnetPlaceNode,
    SurveyEdge,
    TakeWhereEdge,
    TooManyFiringsError,
    TransitionFailedError,
    petri_net,
)
from petritype.core.keys import Clause, Filter, Match, Query
from petritype.core.nested import NestedNets, Port
from petritype.runtime import Disable, Enable, Extend, Fire, SetNetParam, SetParam, SetTokens


# --- The quickstart, by position and by keyword ----------------------------------------------


def double(x: int) -> int:
    return x * 2


def quickstart() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Input", int, [1, 2, 3]),
        ArgumentEdgeToTransition("Input", "Double", "x"),
        FunctionTransitionNode("Double", double),
        ReturnedEdgeFromTransition("Double", "Output"),
        ListPlaceNode("Output", int),
    ])


def quickstart_by_keyword() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode(name="Input", type=int, tokens=[1, 2, 3]),
        ArgumentEdgeToTransition(place_node_name="Input", transition_node_name="Double", argument="x"),
        FunctionTransitionNode(name="Double", function=double),
        ReturnedEdgeFromTransition(transition_node_name="Double", place_node_name="Output", return_index=None),
        ListPlaceNode(name="Output", type=int),
    ], expect_acyclic=True)


def tokens_of(graph: ExecutableGraph, name: str) -> list[Any]:
    """The tokens of a place, narrowed: ``place_named`` may return ``None``, and a
    subnet place holds no tokens of its own."""
    place = graph.place_named(name)
    if not isinstance(place, ListPlaceNode):
        raise KeyError(name)
    return place.tokens


async def run_the_quickstart() -> None:
    graph, fired = await ExecutableGraphOperations.execute_graph(quickstart(), stop_after_n_firings=3)
    assert fired == 3
    output = graph.place_named("Output")
    assert output is not None
    assert output.tokens == [2, 4, 6]
    assert tokens_of(graph, "Output") == [2, 4, 6]
    assert graph.place("Output").tokens == [2, 4, 6]  # the lookup that raises for a name the net lacks

    graph, _ = await ExecutableGraphOperations.execute_graph(quickstart_by_keyword(), stop_after_n_firings=None)
    assert tokens_of(graph, "Output") == [2, 4, 6]


# --- Every input edge, keys and filters -------------------------------------------------------


class Courier(BaseModel):
    name: str
    parcels: int = 0


class Order(BaseModel):
    item: str
    courier: str


class Tally(BaseModel):
    loaded: int = 0


class Inspection(BaseModel):
    by: str
    courier: str


def courier_of(order: Order) -> str:
    return order.courier


def load(order: Order, courier: Courier, rate: float, tally: Tally, promotions: list[str]) -> str:
    """Takes an order and, live, the courier it names; reads a copy of the rate and of
    every promotion, and counts the load in place."""
    courier.parcels += 1
    tally.loaded += 1
    return f"{order.item} on {courier.name} at {rate} with {len(promotions)} promotions"


def dispatch(courier: Courier, waste: list[str]) -> Courier:
    """Takes the courier the filter keeps, and every piece of waste, none or more."""
    return courier


def inspect_courier(inspection: Inspection, courier: Courier) -> str:
    return f"{inspection.by}: {courier.name} carries {courier.parcels}"


def front_courier(ticket: str, courier: Courier) -> str:
    return f"{ticket}: {courier.name} is in front"


def roll_call(ticket: str, couriers: list[Courier]) -> str:
    return f"{ticket}: {len(couriers)} couriers"


def has_orders(graph: ExecutableGraph) -> bool:
    """A guard: it narrows the place, as ``tokens_of`` does."""
    orders = graph.place_named("Orders")
    return isinstance(orders, ListPlaceNode) and len(orders.tokens) > 0


def order_pressure(graph: ExecutableGraph) -> float:
    return float(len(tokens_of(graph, "Orders")))


def named_by(place: str, key: str) -> Filter:
    """The courier whose ``name`` is the key ``key`` of this firing's token from ``place``."""
    return Filter((Clause("name", input=place, input_key=key),))


@petri_net(name="depot", mode="batch", description="Orders loaded onto couriers, every input edge used once")
def depot() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders", Order, [Order(item="Lamp", courier="Ada"), Order(item="Book", courier="Ben")],
                      keys={"courier": courier_of}),
        ListPlaceNode("Couriers", Courier, [Courier(name="Ada"), Courier(name="Ben")], keys=("name",)),
        ListPlaceNode("Rate", float, [1.5]),
        ListPlaceNode("Tally", Tally, [Tally()]),
        ListPlaceNode("Promotions", str, ["Spring"]),
        ListPlaceNode("Waste", str, ["Box"]),
        ListPlaceNode("Inspections", Inspection, [Inspection(by="Audit", courier="Ben")], keys=["courier"]),
        ListPlaceNode("Tickets", str, ["Front", "Roll"]),
        ListPlaceNode("Receipts", str),
        ListPlaceNode("Reports", str),
        ListPlaceNode("On Road", Courier),
        FunctionTransitionNode("Load", load, guard=has_orders, priority=order_pressure),
        ArgumentEdgeToTransition("Orders", "Load", "order"),
        MutateWhereEdge("Couriers", "Load", "courier", where=named_by("Orders", "courier")),
        SnapshotEdge("Rate", "Load", "rate"),
        MutateEdge("Tally", "Load", "tally"),
        SurveyEdge("Promotions", "Load", "promotions"),
        ReturnedEdgeFromTransition("Load", "Receipts"),
        FunctionTransitionNode("Dispatch", dispatch, execution="thread", group="Road"),
        TakeWhereEdge("Couriers", "Dispatch", "courier", where=Filter.where(name="Ada")),
        DrainEdge("Waste", "Dispatch", "waste"),
        ReturnedEdgeFromTransition("Dispatch", "On Road"),
        FunctionTransitionNode("Inspect", inspect_courier),
        ArgumentEdgeToTransition("Inspections", "Inspect", "inspection"),
        ReadWhereEdge("Couriers", "Inspect", "courier", where=named_by("Inspections", "courier")),
        ReturnedEdgeFromTransition("Inspect", "Reports"),
        FunctionTransitionNode("Front", front_courier),
        ArgumentEdgeToTransition("Tickets", "Front", "ticket"),
        ReadEdge("Couriers", "Front", "courier"),
        ReturnedEdgeFromTransition("Front", "Reports"),
        FunctionTransitionNode("Roll Call", roll_call),
        ArgumentEdgeToTransition("Tickets", "Roll Call", "ticket"),
        ReadAllEdge("Couriers", "Roll Call", "couriers"),
        ReturnedEdgeFromTransition("Roll Call", "Reports"),
    ], tie_break="round_robin", record_events=True)


def every_edge_by_keyword() -> list[object]:
    """Each edge class built with its arguments named, ``where=`` and ``reaches=``
    among them; ``reaches`` is given by keyword alone."""
    return [
        ArgumentEdgeToTransition(place_node_name="Field", transition_node_name="Use", argument="unit",
                                 reaches=("Orders In",)),
        TakeWhereEdge(place_node_name="Couriers", transition_node_name="Dispatch", argument="courier",
                      where=Filter.where(name=["Ada", "Ben"]), reaches=()),
        MutateEdge(place_node_name="Field", transition_node_name="Hand Over", argument="unit",
                   reaches=["Orders In", "Shots Out"]),
        DrainEdge(place_node_name="Waste", transition_node_name="Dispatch", argument="waste"),
        SnapshotEdge(place_node_name="Rate", transition_node_name="Load", argument="rate"),
        SurveyEdge(place_node_name="Promotions", transition_node_name="Load", argument="promotions"),
        MutateEdge(place_node_name="Tally", transition_node_name="Load", argument="tally"),
        MutateWhereEdge(place_node_name="Couriers", transition_node_name="Load", argument="courier",
                        where=Filter((Clause("name", values=("Ada",)),))),
        ReadEdge(place_node_name="Couriers", transition_node_name="Front", argument="courier"),
        ReadWhereEdge(place_node_name="Couriers", transition_node_name="Inspect", argument="courier",
                      where=Filter((Clause("name", param="favourite"),))),
        ReadAllEdge(place_node_name="Couriers", transition_node_name="Roll Call", argument="couriers"),
        ReturnedEdgeFromTransition(transition_node_name="Load", place_node_name="Receipts"),
        FunctionTransitionNode(name="Load", function=load, kwargs=None, guard=has_orders, priority=None,
                               execution="inline", group=None),
    ]


async def query_the_depot() -> None:
    graph, _ = await ExecutableGraphOperations.execute_graph(depot(), stop_after_n_firings=None)
    ExecutableGraphCheck.assert_acyclic(graph)
    matches: list[Match] = Query.matches(graph, Filter.where(courier="Ada"))
    for match in matches:
        print(match.place, match.token_id, match.keys, match.token)
    print(Query.matches(graph, Filter.where(name="Ben").in_places("Couriers", "On Road")))
    print(Query.distinct(graph, "name"))
    print(tokens_of(graph, "Reports"), graph.drain_events()[:1], graph.fired_counts)


# --- Nets as tokens, ports and subnet places -------------------------------------------------


def fire_order(order: str) -> str:
    return f"Shot: {order}"


def build_unit() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders In", str, port=True),
        ListPlaceNode("Shots Out", str, port=True),
        FunctionTransitionNode("Fire", fire_order),
        ArgumentEdgeToTransition("Orders In", "Fire", "order"),
        ReturnedEdgeFromTransition("Fire", "Shots Out"),
    ])


def hand_over(order: str, unit: ExecutableGraph) -> None:
    NestedNets.put(unit, "Orders In", [order])


def enter(hero: str) -> str:
    return hero


def leave(hero: str) -> str:
    return f"{hero} has left"


def build_level() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Lobby", str, port=True),
        ListPlaceNode("Exit Hall", str, port=True),
        FunctionTransitionNode("Walk", enter),
        ArgumentEdgeToTransition("Lobby", "Walk", "hero"),
        ReturnedEdgeFromTransition("Walk", "Exit Hall"),
    ])


def world() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders", str, ["Advance"]),
        ListPlaceNode("Field", ExecutableGraph, [build_unit()], nested=True, stepping="take_turns"),
        FunctionTransitionNode("Hand Over", hand_over),
        ArgumentEdgeToTransition("Orders", "Hand Over", "order"),
        MutateEdge("Field", "Hand Over", "unit", reaches=("Orders In",)),
        ListPlaceNode("Heroes", str, ["Ada"]),
        ListPlaceNode("Home", str),
        SubnetPlaceNode("Level 1", build_level()),
        SubnetPlaceNode(name="Level 2", graph=build_level(), stepping="passive", group="Dungeon"),
        FunctionTransitionNode("Enter", enter),
        ArgumentEdgeToTransition("Heroes", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", Port("Level 1", "Lobby")),
        FunctionTransitionNode("Leave", leave),
        ArgumentEdgeToTransition(Port("Level 1", "Exit Hall"), "Leave", "hero"),
        ReturnedEdgeFromTransition("Leave", "Home"),
    ])


async def run_the_world() -> None:
    graph, _ = await ExecutableGraphOperations.execute_graph(world(), stop_after_n_firings=None)
    assert tokens_of(graph, "Home") == ["Ada has left"]
    level = graph.subnet("Level 1")
    assert level is not None and level.is_closed
    NestedNets.put(level, "Lobby", ["Ben"])

    post = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Inbox", str, port=True),
        ListPlaceNode("Ledger", str),
    ])
    NestedNets.put(post, "Inbox", ["Dear Ada"])
    try:
        NestedNets.put(post, "Ledger", ["IOU"])
    except ClosedNetError as error:
        print(error.net, error.place, error.act)


# --- The runtime: Runner, RunContext and the inbox ------------------------------------------


def scale(x: int, factor: int = 2) -> int:
    return x * factor


def scaler() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Input", int, [1, 2, 3]),
        ArgumentEdgeToTransition("Input", "Scale", "x"),
        FunctionTransitionNode("Scale", scale, kwargs={"factor": 2}),
        ReturnedEdgeFromTransition("Scale", "Output"),
        ListPlaceNode("Output", int),
    ], params={"speed": 1})


def redraw(graph: ExecutableGraph) -> None:
    print(sorted(graph.in_flight), graph.last_fired, graph.step_count)


async def drive() -> RunSummary:
    inbox: asyncio.Queue = asyncio.Queue()
    ctx = RunContext(graph=scaler(), mode=ExecutionMode.CONCURRENT, observers=(redraw,), inbox=inbox,
                     error_after_n_firings=100)
    inbox.put_nowait(Extend("Input", [4]))
    inbox.put_nowait(SetParam("Scale", {"factor": 3}))
    inbox.put_nowait(Disable("Scale"))
    inbox.put_nowait(Enable("Scale"))
    inbox.put_nowait(Fire("Scale"))
    inbox.put_nowait(SetNetParam("speed", 2))
    inbox.put_nowait(SetTokens("Output", []))
    applied: int = Runner.drain_inbox(ctx)
    summary = await Runner.run(ctx, stop_after_n_firings=1)
    if not summary.quiesced:
        summary = await Runner.run(ctx)
    print(applied, summary.n_fired, summary.fired, tokens_of(ctx.graph, "Output"))

    sequential = RunContext(scaler(), ExecutionMode.SEQUENTIAL, stop=asyncio.Event())
    fired_now: int = await Runner.step(sequential)
    graph: ExecutableGraph = await Runner.run_to_completion(sequential)
    assert sequential.stop is not None
    sequential.stop.set()
    await Runner.run_indefinitely(sequential, tick=0.01)
    print(fired_now, tokens_of(graph, "Output"))
    return summary


# --- Errors ------------------------------------------------------------------------------------


def charge(order: Order) -> str:
    raise ValueError(f"card declined for {order.item}")


async def fail() -> None:
    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders", Order, [Order(item="Kite", courier="Dot")]),
        FunctionTransitionNode("Charge", charge),
        ArgumentEdgeToTransition("Orders", "Charge", "order"),
        ReturnedEdgeFromTransition("Charge", "Receipts"),
        ListPlaceNode("Receipts", str),
    ])
    try:
        await ExecutableGraphOperations.execute_graph(graph)
    except TransitionFailedError as error:
        print(error.transition_name, error.consumed, error.restored, repr(error.__cause__))
    try:
        await Runner.run(RunContext(graph=quickstart(), error_after_n_firings=1))
    except TooManyFiringsError as error:
        print(error, error.summary)
    except PetritypeError as error:
        print("any library error", error)


async def main() -> None:
    await run_the_quickstart()
    every_edge_by_keyword()
    await query_the_depot()
    await run_the_world()
    await drive()
    await fail()


if __name__ == "__main__":
    asyncio.run(main())
