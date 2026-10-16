"""The priced shop (``examples/edges_on_empty_places/priced_shop.py``), the net the shop
notebook draws: what each promotion takes off an order, and the survey behaving as it does in
the conformance shop, with typed tokens."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.edges_on_empty_places.priced_shop import (  # noqa: E402
    PROMOTIONS, SCRIPT, SHIPPING, build_priced_shop, fireable, order, price_order, promotion, put,
)
from petritype.core.executable_graph_components import ExecutableGraphOperations  # noqa: E402


def step(graph) -> int:
    _, fired = asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    return fired


def names(graph, place: str) -> list[str]:
    return [p.name for p in graph.place_named(place).tokens]


def test_the_example_day_prices_two_orders_and_leaves_the_promotions_running():
    graph = build_priced_shop()
    for tokens in SCRIPT:
        put(graph, tokens)
        assert step(graph) == 1
    assert step(graph) == 0
    first, second = graph.place_named("Priced").tokens
    assert (first.total, first.savings) == (1200 + SHIPPING, [])
    assert [(s.promotion, s.amount) for s in second.savings] == [("10% off", 450), ("Free shipping", SHIPPING)]
    assert second.total == 4500 + SHIPPING - 450 - SHIPPING
    assert names(graph, "Promotions") == ["10% off", "Free shipping"]


def test_a_promotion_with_a_minimum_applies_only_above_it():
    five_off = promotion("£5 off orders over £30")
    assert price_order(order(1, "Teapot"), [five_off]).total == 2800 + SHIPPING
    assert price_order(order(2, "Hamper"), [five_off]).total == 4500 + SHIPPING - 500


def test_every_promotion_at_once_is_worked_out_on_the_order_as_it_stands():
    receipt = price_order(order(1, "Hamper"), list(PROMOTIONS))
    assert [s.amount for s in receipt.savings] == [450, 500, SHIPPING]
    assert receipt.total == 4500 + SHIPPING - 450 - 500 - SHIPPING


def test_promotions_alone_fire_nothing_and_the_survey_leaves_them_running():
    graph = build_priced_shop()
    put(graph, {"Promotions": [promotion("10% off")]})
    assert fireable(graph) == []
    put(graph, {"Orders": [order(1, "Tea for two")]})
    assert step(graph) == 1
    assert names(graph, "Promotions") == ["10% off"]
    assert graph.place_named("Priced").tokens[0].total == 1200 + SHIPPING - 120
