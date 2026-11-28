"""The place history on ``ExecutableGraph``: ``input_place_history`` and
``output_place_history``, kept for the last ``place_history_length`` firings of
``execute_graph`` and skipped entirely at a length of zero. A graph holding a
place history dumps (``model_dump``) without a pydantic serialisation warning.
"""

import asyncio
import warnings

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)


def test_place_history_length_zero_skips_input_place_history():
    def inc(x: int) -> int:
        return x + 1

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("A", int, tokens=[1, 2]),
            ListPlaceNode("B", int),
            FunctionTransitionNode("Inc", inc),
            ArgumentEdgeToTransition("A", "Inc", "x"),
            ReturnedEdgeFromTransition("Inc", "B"),
        ]
    )
    asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1, place_history_length=0))
    assert graph.input_place_history == []
    assert graph.output_place_history == []
    assert graph.place_named("B").tokens == [2]

    asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1, place_history_length=1))
    assert len(graph.input_place_history) == 1
    assert len(graph.output_place_history) == 1


def test_a_graph_holding_a_place_history_dumps_without_a_serialisation_warning():
    """Each history holds one list of places per firing, which the fields' types
    must say: typed as a list of places, pydantic warns on every ``model_dump``."""
    def inc(x: int) -> int:
        return x + 1

    graph = ExecutableGraphOperations.construct_graph(
        [
            ListPlaceNode("A", int, tokens=[1]),
            ListPlaceNode("B", int),
            FunctionTransitionNode("Inc", inc),
            ArgumentEdgeToTransition("A", "Inc", "x"),
            ReturnedEdgeFromTransition("Inc", "B"),
        ]
    )
    asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        dumped = graph.model_dump()
    assert [[place["name"] for place in firing] for firing in dumped["input_place_history"]] == [["A"]]
    assert [[place["tokens"] for place in firing] for firing in dumped["output_place_history"]] == [[[2]]]
