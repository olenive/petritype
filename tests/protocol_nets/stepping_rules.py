"""The shared conformance net ``stepping-rules`` (PROTOCOL.md §11, §12.3): places
that hold nets under each of the four stepping rules, and a second level, so that
a transcript shows which transition each ``step`` fires.

Its names, places, transitions, seeds and templates are fixed in PROTOCOL.md §11,
so that the Rust runner can build the same net from the text alone and replay
``tests/protocol/10_stepping_rules.json``. Every body passes its token on
unchanged, and every token is an integer or a net, so that both runtimes write
the same lines.

The outer net holds, in this order, ``Late`` (outer first), ``Ring`` (take turns),
``Early`` (inner first) and ``Shelf`` (passive), each with one net, then ``Inbox``
and ``Outbox`` with ``Forward`` between them. ``Early``'s net is a ``Team``, whose
own ``Helpers`` place takes turns with it: the second level. From the seed, one
deep step a ``step``: the team's ``Plan``, its helper's ``Work`` twice, then the
outer net and the ring's worker in turn (``Forward``, ``Work``, ``Forward``,
``Work``, ``Forward``), then the late worker's ``Work``; then nothing, the
shelf's worker being passive.
"""

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.core.serialisation import Templates


def pass_on(job: int) -> int:
    return job


@Templates.template("Worker")
def worker(jobs: int = 0) -> ExecutableGraph:
    """``Todo -Work-> Done``, with the jobs ``0`` to ``jobs - 1`` to do."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Todo", int, list(range(jobs))),
        ListPlaceNode("Done", int),
        FunctionTransitionNode("Work", pass_on),
        ArgumentEdgeToTransition("Todo", "Work", "job"),
        ReturnedEdgeFromTransition("Work", "Done"),
    ])


@Templates.template("Team")
def team() -> ExecutableGraph:
    """``Todo -Plan-> Done`` with one job, and ``Helpers``, a place of nets that
    takes turns with the team, holding one worker with two jobs."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Todo", int, [0]),
        ListPlaceNode("Done", int),
        ListPlaceNode("Helpers", ExecutableGraph, [worker(jobs=2)], nested=True, stepping="take_turns"),
        FunctionTransitionNode("Plan", pass_on),
        ArgumentEdgeToTransition("Todo", "Plan", "job"),
        ReturnedEdgeFromTransition("Plan", "Done"),
    ])


@petri_net(name="stepping-rules", description="Nets under each stepping rule take their turns")
def stepping_rules() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Late", ExecutableGraph, [worker(jobs=1)], nested=True, stepping="outer_first"),
        ListPlaceNode("Ring", ExecutableGraph, [worker(jobs=2)], nested=True, stepping="take_turns"),
        ListPlaceNode("Early", ExecutableGraph, [team()], nested=True, stepping="inner_first"),
        ListPlaceNode("Shelf", ExecutableGraph, [worker(jobs=5)], nested=True, stepping="passive"),
        ListPlaceNode("Inbox", int, [0, 1, 2]),
        ListPlaceNode("Outbox", int),
        FunctionTransitionNode("Forward", pass_on),
        ArgumentEdgeToTransition("Inbox", "Forward", "job"),
        ReturnedEdgeFromTransition("Forward", "Outbox"),
    ])
