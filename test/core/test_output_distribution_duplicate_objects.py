"""Distribution of duplicate object references via an output distribution function.

Covers the case where a transition's ``output_distribution_function`` routes
the *same* object to several destination places, across two different token
types, with ``allow_token_copying=True``. Every destination must receive
exactly one token — none may be silently dropped.

Sync bodies + ``asyncio.run`` because ``execute_graph`` is async and this
repo doesn't enable pytest-asyncio auto-mode (matches test_step_count.py).
"""

import asyncio
from datetime import date

from pydantic import BaseModel

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)


class Span(BaseModel):
    """A token type with non-JSON-primitive (``date``) fields."""
    label: str
    start: date
    end: date


class Count(BaseModel):
    """A token type with only JSON-primitive fields."""
    label: str
    value: int = 0


# Two destinations of the first type, three of the second — mirroring a fan-out
# where the transition returns the same object reference multiple times.
_SPAN_PLACES = ["SpanA", "SpanB"]
_COUNT_PLACES = ["CountA", "CountB", "CountC"]
_ALL_DESTINATIONS = _SPAN_PLACES + _COUNT_PLACES


def _build_fanout_graph():
    span = Span(label="x", start=date(2026, 1, 1), end=date(2026, 6, 1))
    count = Count(label="y")

    def distribute(span_in: Span, count_in: Count) -> tuple:
        # Return the SAME object reference repeatedly — the case that exercises
        # the id(token)-based copy path in add_tokens_to_places.
        return (span_in, span_in, count_in, count_in, count_in)

    def distributor(result: tuple) -> dict:
        span_a, span_b, count_a, count_b, count_c = result
        return {
            "SpanA": span_a,
            "SpanB": span_b,
            "CountA": count_a,
            "CountB": count_b,
            "CountC": count_c,
        }

    nodes = [
        ListPlaceNode("SpanSource", Span, [span]),
        ListPlaceNode("CountSource", Count, [count]),
        ListPlaceNode("SpanA", Span),
        ListPlaceNode("SpanB", Span),
        ListPlaceNode("CountA", Count),
        ListPlaceNode("CountB", Count),
        ListPlaceNode("CountC", Count),
        ArgumentEdgeToTransition("SpanSource", "Distribute", "span_in"),
        ArgumentEdgeToTransition("CountSource", "Distribute", "count_in"),
        FunctionTransitionNode("Distribute", distribute, output_distribution_function=distributor),
        ReturnedEdgeFromTransition("Distribute", "SpanA"),
        ReturnedEdgeFromTransition("Distribute", "SpanB"),
        ReturnedEdgeFromTransition("Distribute", "CountA"),
        ReturnedEdgeFromTransition("Distribute", "CountB"),
        ReturnedEdgeFromTransition("Distribute", "CountC"),
    ]
    return ExecutableGraphOperations.construct_graph(nodes, allow_token_copying=True)


def _place(graph, name):
    return next(p for p in graph.places if p.name == name)


def test_distribution_populates_every_destination():
    """Firing once must give every destination place exactly one token, even
    though the same object reference was routed to two of them."""
    graph = _build_fanout_graph()
    graph, fired = asyncio.run(
        ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
    )
    assert fired == 1
    assert _place(graph, "SpanSource").tokens == []
    assert _place(graph, "CountSource").tokens == []

    counts = {name: len(_place(graph, name).tokens) for name in _ALL_DESTINATIONS}
    missing = {n: c for n, c in counts.items() if c != 1}
    assert not missing, f"destinations did not each get exactly one token: {counts}"

    for name in _SPAN_PLACES:
        assert isinstance(_place(graph, name).tokens[0], Span)
    for name in _COUNT_PLACES:
        assert isinstance(_place(graph, name).tokens[0], Count)
