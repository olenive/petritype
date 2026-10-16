"""The shop with prices: the net of ``shop.py``, with orders that cost something and
promotions that change what they cost, for the notebook ``shop_day.py``.

``shop.py`` is the conformance net ``shop`` of PROTOCOL.md §11, which both runtimes replay,
so its tokens stay strings. This net has the same one transition, ``Price Order``, with typed
tokens. An ``Order`` is a basket with a subtotal and a shipping charge; a ``Promotion`` takes a
percentage off, takes an amount off orders over a minimum, or makes shipping free; ``Price
Order`` turns an order and the promotions running into a ``Receipt``.

``Price Order`` takes the front order and **surveys** ``Promotions`` (a ``SurveyEdge``): a copy
of every promotion running, zero or more, none consumed. An order is priced with whatever is
running when it is taken, and nothing else, and an empty ``Promotions`` does not hold it back.

Each promotion's saving is worked out on the order as it stands, independently of the others,
and a receipt never comes to less than nothing. Amounts are whole pence.

Run it to see the example day::

    uv run python examples/edges_on_empty_places/priced_shop.py
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict

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


def money(pence: int) -> str:
    """Whole pence as pounds: ``1250`` is ``£12.50``."""
    return f"£{pence // 100}.{pence % 100:02d}"


# --- tokens -----------------------------------------------------------------------------------


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int
    basket: str
    subtotal: int  # pence
    shipping: int  # pence


class Promotion(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    percent_off: int = 0
    amount_off: int = 0  # pence
    minimum: int = 0  # pence: the subtotal an order needs before the promotion applies
    free_shipping: bool = False

    def saving(self, order: Order) -> int:
        """What this promotion takes off ``order``, in pence; 0 where it does not apply."""
        if order.subtotal < self.minimum:
            return 0
        if self.free_shipping:
            return order.shipping
        return order.subtotal * self.percent_off // 100 + self.amount_off


class Saving(BaseModel):
    model_config = ConfigDict(frozen=True)

    promotion: str
    amount: int  # pence


class Receipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    order: Order
    savings: list[Saving]
    total: int  # pence

    @property
    def saved(self) -> int:
        return sum(saving.amount for saving in self.savings)


# The promotions the shop knows how to run, and what it sells.
PROMOTIONS = (
    Promotion(name="10% off", percent_off=10),
    Promotion(name="£5 off orders over £30", amount_off=500, minimum=3000),
    Promotion(name="Free shipping", free_shipping=True),
)
BASKETS = {"Tea for two": 1200, "Teapot": 2800, "Hamper": 4500}
SHIPPING = 450


def promotion(name: str) -> Promotion:
    return next(p for p in PROMOTIONS if p.name == name)


def order(number: int, basket: str) -> Order:
    return Order(number=number, basket=basket, subtotal=BASKETS[basket], shipping=SHIPPING)


# --- bodies -----------------------------------------------------------------------------------


def price_order(order: Order, promotions: list[Promotion]) -> Receipt:
    """The front order, and a copy of every promotion running, possibly none: the receipt
    lists what each promotion that applies takes off. The promotions stay running."""
    savings = [
        Saving(promotion=p.name, amount=p.saving(order)) for p in promotions if p.saving(order) > 0
    ]
    total = max(0, order.subtotal + order.shipping - sum(s.amount for s in savings))
    return Receipt(order=order, savings=savings, total=total)


# --- the net ----------------------------------------------------------------------------------


def build_priced_shop(record_events: bool = False) -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders", Order),
        ListPlaceNode("Promotions", Promotion),
        ListPlaceNode("Priced", Receipt),
        # An order is priced with whatever promotions are running, none included.
        FunctionTransitionNode("Price Order", price_order),
        ArgumentEdgeToTransition("Orders", "Price Order", "order"),
        SurveyEdge("Promotions", "Price Order", "promotions"),
        ReturnedEdgeFromTransition("Price Order", "Priced"),
    ], record_events=record_events)


# --- the example day --------------------------------------------------------------------------

# What is put in before each firing: an order with nothing running, then two promotions
# and a second order.
SCRIPT = (
    {"Orders": [order(1, "Tea for two")]},
    {"Promotions": [promotion("10% off"), promotion("Free shipping")], "Orders": [order(2, "Hamper")]},
)


def put(graph: ExecutableGraph, tokens: dict[str, list]) -> None:
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


def describe(receipt: Receipt) -> str:
    """A receipt in one line."""
    o = receipt.order
    lines = ", ".join(f"{s.promotion} −{money(s.amount)}" for s in receipt.savings) or "no promotion"
    return f"Order {o.number} ({o.basket} {money(o.subtotal)} + {money(o.shipping)} shipping): {lines}; total {money(receipt.total)}"


def main() -> None:
    graph = build_priced_shop()
    for tokens in SCRIPT:
        put(graph, tokens)
        asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
        print(f"{graph.last_fired}: running {[p.name for p in graph.place_named('Promotions').tokens]}")
    for receipt in graph.place_named("Priced").tokens:
        print(describe(receipt))


if __name__ == "__main__":
    main()
