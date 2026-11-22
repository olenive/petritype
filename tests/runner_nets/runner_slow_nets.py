"""Nets whose body blocks for a minute, for heartbeats during a body and for
``shutdown`` abandoning it: ``sleeper``, a body of the loaded net, and
``deep-sleeper``, the same body in a subnet inside a net that a token carries."""

import time

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    SubnetPlaceNode,
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


@petri_net(name="deep-sleeper")
def deep_sleeper():
    """A ship on the quay, a net its token carries, whose subnet place ``Hold``
    holds a ``sleeper``."""
    hold = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, [1]),
        ArgumentEdgeToTransition("In", "Linger", "x"),
        FunctionTransitionNode("Linger", linger),
        ReturnedEdgeFromTransition("Linger", "Out"),
        ListPlaceNode("Out", int),
    ], template="Sleeping Hold")
    ship = ExecutableGraphOperations.construct_graph([SubnetPlaceNode("Hold", hold)], template="Sleeping Ship")
    return ExecutableGraphOperations.construct_graph([ListPlaceNode("Quay", ExecutableGraph, [ship], nested=True)])
