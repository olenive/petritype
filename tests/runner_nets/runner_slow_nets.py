"""A net whose body blocks for a minute, for heartbeats during a body and for
``shutdown`` abandoning it."""

import time

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)


def linger(x: int) -> int:
    time.sleep(60)
    return x


@petri_net(name="sleeper")
def sleeper():
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, [1]),
        ArgumentEdgeToTransition("In", "Linger", "x"),
        FunctionTransitionNode("Linger", linger),
        ReturnedEdgeFromTransition("Linger", "Out"),
        ListPlaceNode("Out", int),
    ])
