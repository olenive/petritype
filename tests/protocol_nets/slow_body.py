"""A net whose one body sleeps, ``slow-body``, for watching what the wire carries
while a Python body runs (``tests/test_runner_slow_body.py``, and the viewer's
process tests in ``petritype-bevy/tests/remote_process.rs``).

``In -Work-> Out``, seeded with two jobs. ``Work`` sleeps ``delay_s`` seconds (a
load parameter, default 1.0) and passes its job on. Two jobs, so that after one
firing there is a token left in ``In`` and one in ``Out``.

``Job`` is a pydantic model, so every place is ``serializable``.
"""

import time

from pydantic import BaseModel

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)


class Job(BaseModel):
    """A job to work on."""

    id: int


@petri_net(name="slow-body", description="One transition whose body sleeps")
def slow_body(delay_s: float = 1.0) -> ExecutableGraph:
    def work(job: Job) -> Job:
        time.sleep(delay_s)
        return job

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", Job, [Job(id=1), Job(id=2)]),
        ListPlaceNode("Out", Job),
        FunctionTransitionNode("Work", work),
        ArgumentEdgeToTransition("In", "Work", "job"),
        ReturnedEdgeFromTransition("Work", "Out"),
    ])
