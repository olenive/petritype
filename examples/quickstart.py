"""The README Quickstart, executable.

This is the single source for that snippet: CI runs this file from a base install
(pydantic only), and the post-publish smoke test fetches and runs it against the
artifact on PyPI. Keep it identical to the ``## Quickstart`` block in README.md --
if you change one, change the other.

The assertions deliberately do not sort: the printed order is part of what the
README documents, so an ordering regression must fail here.
"""

import asyncio
from petritype.core.executable_graph_components import (
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ArgumentEdgeToTransition,
    ReturnedEdgeFromTransition,
)

# 1. Define your types and functions
def double(x: int) -> int:
    return x * 2

# 2. Build the graph
graph = ExecutableGraphOperations.construct_graph([
    ListPlaceNode('Input', int, [1, 2, 3]),
    ArgumentEdgeToTransition('Input', 'Double', 'x'),
    FunctionTransitionNode('Double', double),
    ReturnedEdgeFromTransition('Double', 'Output'),
    ListPlaceNode('Output', int),
])

# 3. Execute
graph, fired = asyncio.run(
    ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=3)
)

print(graph.place_named('Output').tokens)  # [2, 4, 6]

assert graph.place_named('Output').tokens == [2, 4, 6], graph.place_named('Output').tokens
assert fired == 3, fired
print("quickstart OK")
