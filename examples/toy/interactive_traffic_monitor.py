"""Interactive traffic monitor (toy).

An autonomous net samples a "traffic level" over time and classifies it. At any
point an external caller -- a UI, the Petri app's inject handler, or just the
driver loop below -- drops a UserRequest into the `Requests` place to override
the next reading with their own level. A priority-aware selector handles that
injection on the *next* fire, ahead of routine sampling: "throw a switch"
responsiveness, without preemption.

The key pattern: the user supplies only a simple, validated scalar (a float),
but token *construction* stays inside the net. `inject_reading` is an ordinary
transition whose constrained return type (Reading) guarantees a well-formed
token -- exactly as `auto_sample` does for API-sourced data. The net owns the
typing, not the caller.

Run the headless demo:
    python examples/toy/interactive_traffic_monitor.py

For a live UI (slider + inject button + play/pause), see the companion marimo
notebook `interactive_traffic_monitor_notebook.py`.
"""

import asyncio
import random

from pydantic import BaseModel

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)

ANOMALY_THRESHOLD = 0.8
_rng = random.Random(0)  # seeded for a reproducible illustration


class UserRequest(BaseModel):
    level: float  # the only thing crossing the boundary: a simple, validated scalar


class Reading(BaseModel):
    level: float
    source: str  # "auto" | "user"


class Classification(BaseModel):
    level: float
    source: str
    verdict: str  # "ok" | "ANOMALY"


def auto_sample() -> Reading:
    """Source transition (no inputs): emit a routine reading 'from the API'."""
    return Reading(level=round(_rng.uniform(0.3, 0.7), 3), source="auto")


def inject_reading(request: UserRequest) -> Reading:
    """Builder transition: turn the user's parameter into a Reading, just as
    auto_sample produces an API reading. The constrained return type means the
    net -- not the caller -- guarantees a well-formed token."""
    return Reading(level=request.level, source="user")


def classify(reading: Reading) -> Classification:
    verdict = "ANOMALY" if reading.level > ANOMALY_THRESHOLD else "ok"
    return Classification(level=reading.level, source=reading.source, verdict=verdict)


def user_first_selector(
    graph: ExecutableGraph, enabled: list[FunctionTransitionNode]
) -> FunctionTransitionNode | None:
    """Priority: a pending user injection wins, then classify pending readings,
    then auto-sample. This is next-fire responsiveness -- the injected input is
    acted on before the net generates anything new."""
    by_name = {t.name: t for t in enabled}
    for name in ("InjectReading", "Classify", "AutoSample"):
        if name in by_name:
            return by_name[name]
    return None


def build_interactive_monitor() -> ExecutableGraph:
    graph = ExecutableGraphOperations.construct_graph([
        # === PLACES ===
        ListPlaceNode("Requests", UserRequest),  # external input lands here
        ListPlaceNode("Readings", Reading),
        ListPlaceNode("Log", Classification),

        # === TRANSITIONS ===
        FunctionTransitionNode("AutoSample", auto_sample),
        FunctionTransitionNode("InjectReading", inject_reading),
        FunctionTransitionNode("Classify", classify),

        # === EDGES ===
        ReturnedEdgeFromTransition("AutoSample", "Readings"),

        ArgumentEdgeToTransition("Requests", "InjectReading", "request"),
        ReturnedEdgeFromTransition("InjectReading", "Readings"),

        ArgumentEdgeToTransition("Readings", "Classify", "reading"),
        ReturnedEdgeFromTransition("Classify", "Log"),
    ])
    graph.transition_selector = user_first_selector
    return graph


async def main() -> None:
    graph = build_interactive_monitor()

    # 1. Runs autonomously: sample -> classify, repeatedly.
    await ExecutableGraphOperations.execute_graph(graph, max_transitions=6)

    # 2. The user "throws a switch" -- inject an arbitrary high level. In the
    #    Petri app this append is the runtime inject handler; here the driver
    #    stands in for the UI.
    graph.place_named("Requests").tokens.append(UserRequest(level=0.97))

    # 3. Next fires: the injection is handled FIRST (priority selector), then
    #    classified -- so the override takes effect immediately, no preemption.
    await ExecutableGraphOperations.execute_graph(graph, max_transitions=2)

    print("\nLog:")
    for entry in graph.place_named("Log").tokens:
        print(f"  {entry.source:5} level={entry.level:.3f} -> {entry.verdict}")


if __name__ == "__main__":
    asyncio.run(main())
