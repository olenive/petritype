"""The shop: orders priced with whatever promotions are running.

The shop of ``EDGES_ON_EMPTY_PLACES.md`` §2, reduced to its survey on 2026-10-16, and the
conformance net ``shop`` of PROTOCOL.md §11. Every place holds strings. ``Price Order``
takes the front order and *surveys* ``Promotions`` (a ``SurveyEdge``: a deep copy of every
promotion, zero or more, none consumed). The survey does not hold the transition back when
``Promotions`` is empty, which it usually is: a shop with no promotion running still sells
things. With an edge that needs a token, ``Price Order`` would need an "empty twin" guarded
on the place being empty. The seed is empty everywhere; what happens is what is put in.

The edge that *takes* every token, zero or more (a ``DrainEdge``), has its own example
beside this one, the traffic light (``traffic_light.py``): every car waiting goes while
the light is green. Its guard keeps it from ever draining an empty queue, so the zero case
is shown here, by the survey.

Run it to see the script, the marking after each firing and ``Price Order`` waiting for an
order however many promotions are running::

    uv run python examples/edges_on_empty_places/shop.py

The notebook beside it runs this net with prices (``priced_shop.py``): switch promotions on
and off, place orders and watch each one priced::

    uv run --extra examples marimo edit examples/edges_on_empty_places/shop_day.py
"""

from __future__ import annotations

import asyncio

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    SurveyEdge,
    TokenIds,
)

PLACES = ("Orders", "Promotions", "Priced", "Discounts Applied")


# --- bodies ---------------------------------------------------------------------------------


def price_order(order: str, promotions: list[str]) -> tuple[str, list[str]]:
    """The front order, and a copy of every promotion running, oldest first,
    possibly none: the order is priced and each promotion applied to it. The
    promotions stay where they are."""
    return order, [f"{promotion} on {order}" for promotion in promotions]


# --- the net ---------------------------------------------------------------------------------


def build_shop(record_events: bool = True) -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        *[ListPlaceNode(name, str) for name in PLACES],
        # An order is priced with whatever promotions are running, none included.
        FunctionTransitionNode("Price Order", price_order),
        ArgumentEdgeToTransition("Orders", "Price Order", "order"),
        SurveyEdge("Promotions", "Price Order", "promotions"),
        ReturnedEdgeFromTransition("Price Order", "Priced", 0),
        ReturnedEdgeFromTransition("Price Order", "Discounts Applied", 1),
    ], record_events=record_events)


@petri_net(name="shop", description="Price orders with whatever promotions are running")
def shop() -> ExecutableGraph:
    return build_shop()


# --- the script ------------------------------------------------------------------------------

# What is put in before each step, and what the step shows: an order with no promotion
# running, then two promotions and a second order (the transcript's script, PROTOCOL.md §11).
SCRIPT = (
    ("Price Order (no promotions)", {"Orders": ["Order 1"]}),
    ("Price Order (two running)", {"Promotions": ["10% off", "Free shipping"], "Orders": ["Order 2"]}),
)
SHOWN = ("Priced", "Discounts Applied", "Promotions")


def put(graph: ExecutableGraph, tokens: dict[str, list[str]]) -> None:
    """Append tokens to places, with fresh ids, as an ``inject`` does."""
    for place, values in tokens.items():
        TokenIds.append(graph, graph.place_named(place), list(values))


def fireable(graph: ExecutableGraph) -> list[str]:
    """The transitions that could fire now, in declaration order."""
    place_nodes, incoming, _, read = ExecutableGraphOperations.maps(graph)
    return [
        transition.name for transition in graph.transitions
        if ExecutableGraphCheck.transition_is_fireable(transition, graph, incoming, place_nodes, read)
    ]


def step(graph: ExecutableGraph) -> int:
    """Fire one transition, as the protocol's ``step`` does; how many fired (0 or 1)."""
    _, fired = asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    return fired


def marking(graph: ExecutableGraph, places=SHOWN) -> dict[str, list[str]]:
    return {place: list(graph.place_named(place).tokens) for place in places}


def main() -> None:
    graph = build_shop(record_events=False)
    for after, tokens in SCRIPT:
        put(graph, tokens)
        step(graph)
        print(f"after {after} ({graph.last_fired}): {marking(graph)}")
    print(f"then nothing is fireable: {fireable(graph)}, a step fires {step(graph)}")
    put(graph, {"Promotions": ["Members' day"]})
    print(f"three promotions running and no order: fireable {fireable(graph)}")


if __name__ == "__main__":
    main()
