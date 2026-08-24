"""Two independent multi-stage branches — the shared net for the execution-modes example.

The *same* definition is run two ways: sequentially (`01_sequential.py`) and concurrently
(`02_concurrent.py`). Those notebooks are identical except for which runner they import from
`runtime.py`, and both **animate** the net as it runs.

Each branch is a 3-stage chain `Start -> s1 -> P1 -> s2 -> P2 -> s3 -> Done`; every stage
takes ~`STEP_SECONDS`. The branches are independent, so the two modes differ visibly:

- sequential: branch A's three stages, then branch B's   -> total ~= SUM (~2.4 s)
- concurrent: both branches advance in lockstep           -> total ~= MAX (~1.2 s)
"""

import asyncio
import time

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.plotting.rustworkx_graph import RustworkxGraph

STEP_SECONDS = 0.4

# Each stage records when it ran so the notebooks can show the difference between modes.
# `reset_timeline()` clears it before each run.
TIMELINE: list[dict] = []


def reset_timeline() -> None:
    TIMELINE.clear()


def _make_stage(name: str):
    """Build a named async transition body: sleep one step, record when it ran, pass the
    token on. The body is `async` and `await`s — so the concurrent runner can overlap it —
    and its `__name__`/`__qualname__` are set so the transition shows a clean label.
    """

    async def stage(token: str) -> str:
        start = time.perf_counter()
        await asyncio.sleep(STEP_SECONDS)
        TIMELINE.append({"name": name, "start": start, "end": time.perf_counter()})
        return token

    stage.__name__ = name
    stage.__qualname__ = name
    return stage


def _branch(letter: str):
    """Nodes + edges for one 3-stage branch: Start -> s1 -> P1 -> s2 -> P2 -> s3 -> Done."""
    start, p1, p2, done = f"Start{letter}", f"{letter}1", f"{letter}2", f"Done{letter}"
    s1, s2, s3 = f"Slow{letter}1", f"Slow{letter}2", f"Slow{letter}3"
    return [
        ListPlaceNode(name=start, type=str, tokens=["go"]),
        ArgumentEdgeToTransition(start, s1, "token"),
        FunctionTransitionNode(name=s1, function=_make_stage(s1)),
        ReturnedEdgeFromTransition(s1, p1),
        ListPlaceNode(name=p1, type=str),
        ArgumentEdgeToTransition(p1, s2, "token"),
        FunctionTransitionNode(name=s2, function=_make_stage(s2)),
        ReturnedEdgeFromTransition(s2, p2),
        ListPlaceNode(name=p2, type=str),
        ArgumentEdgeToTransition(p2, s3, "token"),
        FunctionTransitionNode(name=s3, function=_make_stage(s3)),
        ReturnedEdgeFromTransition(s3, done),
        ListPlaceNode(name=done, type=str),
    ]


def build_graph():
    """A fresh net: two independent 3-stage branches (A defined before B)."""
    nodes_and_edges = [*_branch("A"), *_branch("B")]
    graph = ExecutableGraphOperations.construct_graph(nodes_and_edges)
    pydigraph = RustworkxGraph.from_executable_graph(graph)
    return graph, pydigraph


def summarize(timeline):
    """Normalise the recorded absolute times to start at 0; return total + per-row spans."""
    if not timeline:
        return {"total": 0.0, "rows": []}
    t0 = min(e["start"] for e in timeline)
    total = max(e["end"] for e in timeline) - t0
    rows = sorted(
        ({"name": e["name"], "start": e["start"] - t0, "end": e["end"] - t0} for e in timeline),
        key=lambda r: r["start"],
    )
    return {"total": total, "rows": rows}


def render_timeline(timeline, *, scale=24):
    """A tiny ASCII Gantt of when each transition ran, plus the total wall-clock."""
    s = summarize(timeline)
    if s["total"] <= 0:
        return "_(no transitions fired)_"
    lines = []
    for row in s["rows"]:
        lead = round(row["start"] / s["total"] * scale)
        span = max(1, round((row["end"] - row["start"]) / s["total"] * scale))
        bar = "·" * lead + "█" * span
        lines.append(f"`{row['name']}`  `{bar:<{scale}}`  {row['start']:.2f}s → {row['end']:.2f}s")
    return "\n\n".join(lines) + f"\n\n**Total wall-clock: {s['total']:.2f}s**"
