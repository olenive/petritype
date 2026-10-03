"""Nets for ``tests/test_runner_process.py``, imported by a runner subprocess
through ``--module`` (this directory is put on its ``PYTHONPATH``)."""

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)


def greet(x: int) -> int:
    print("hello from a body")
    return x + 1


@petri_net(name="greeter", description="Prints from a body")
def greeter(n: int = 1):
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, list(range(n))),
        ArgumentEdgeToTransition("In", "Greet", "x"),
        FunctionTransitionNode("Greet", greet),
        ReturnedEdgeFromTransition("Greet", "Out"),
        ListPlaceNode("Out", int),
    ])


@petri_net(name="watcher", mode="24/7")
def watcher():
    return greeter(n=1)
