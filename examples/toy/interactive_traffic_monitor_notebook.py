"""Live UI for the interactive traffic monitor (companion to
interactive_traffic_monitor.py).

A marimo notebook that runs the autonomous net and lets you inject your own
traffic level live -- play/pause auto-run, single-step, and an Inject button.
Demonstrates next-fire responsiveness: an injected reading is handled before
the net samples again.

marimo is not a petritype dependency, so run it with a one-off environment, from
the repo root:

    uvx marimo edit examples/toy/interactive_traffic_monitor_notebook.py
    # or:  uv run --with marimo marimo edit examples/toy/interactive_traffic_monitor_notebook.py
"""

import marimo

__generated_with = "0.10.12"
app = marimo.App(width="medium")


@app.cell
def _():
    import contextlib
    import io

    import marimo as mo

    # Sibling import: marimo puts the notebook's directory on sys.path. Fall
    # back to the package path when run from elsewhere.
    try:
        from interactive_traffic_monitor import (
            ANOMALY_THRESHOLD,
            UserRequest,
            build_interactive_monitor,
        )
    except ModuleNotFoundError:  # pragma: no cover
        from examples.toy.interactive_traffic_monitor import (
            ANOMALY_THRESHOLD,
            UserRequest,
            build_interactive_monitor,
        )

    from petritype.core.executable_graph_components import ExecutableGraphOperations
    from petritype.core.rustworkx_graph import RustworkxGraph
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    return (
        ANOMALY_THRESHOLD,
        ExecutableGraphOperations,
        RustworkxGraph,
        SimpleGraphvizVisualization,
        UserRequest,
        build_interactive_monitor,
        contextlib,
        io,
        mo,
    )


@app.cell
def _(mo):
    mo.md(
        """
        # Interactive traffic monitor

        The net **auto-samples** a traffic level and **classifies** it. Use the
        controls to **inject your own level** — it's handled on the *next* fire,
        ahead of routine sampling (next-fire responsiveness, no preemption).

        - **Auto-run** — toggle continuous stepping (paced by the tick control).
        - **Step ×3** — advance manually.
        - **slider + Inject** — override the next reading with your level.
        """
    )
    return


@app.cell
def _(build_interactive_monitor):
    # Built once; mutated in place by the action cells below.
    graph = build_interactive_monitor()
    return (graph,)


@app.cell
def _(mo):
    # A counter that the action cells bump to force the display to re-render
    # (marimo reacts to value changes, not in-place mutation of `graph`).
    get_tick, set_tick = mo.state(0)
    return get_tick, set_tick


@app.cell
def _(mo):
    level = mo.ui.slider(
        0.0, 1.0, value=0.95, step=0.01, label="User traffic level", show_value=True
    )
    inject = mo.ui.run_button(label="Inject reading")
    step = mo.ui.run_button(label="Step ×3")
    playing = mo.ui.switch(label="Auto-run")
    refresher = mo.ui.refresh(default_interval="1s", label="tick")
    return inject, level, playing, refresher, step


@app.cell
def _(inject, level, mo, playing, refresher, step):
    mo.hstack(
        [
            mo.vstack([playing, refresher]),
            step,
            mo.vstack([level, inject]),
        ],
        justify="start",
        gap=2,
    )
    return


@app.cell
def _(RustworkxGraph, SimpleGraphvizVisualization, get_tick, graph, io, mo):
    get_tick()  # redraw after each step / inject
    # Rebuild the rustworkx view from the live net and draw it: place ovals show
    # their current tokens, transition boxes are the functions. Redrawn on every
    # tick so you watch tokens flow.
    _pyg = RustworkxGraph.from_executable_graph(graph)
    _img = SimpleGraphvizVisualization.graph(_pyg)  # PIL image (needs graphviz `dot`)
    _buf = io.BytesIO()
    _img.save(_buf, format="PNG")
    mo.image(_buf.getvalue(), alt="Petri net state", width=720)
    return


@app.cell
async def _(
    ExecutableGraphOperations, contextlib, graph, io, playing, refresher, set_tick
):
    refresher.value  # depend on the tick so this re-runs each interval
    if playing.value:
        with contextlib.redirect_stdout(io.StringIO()):  # mute engine debug prints
            await ExecutableGraphOperations.execute_graph(graph, max_transitions=2)
        set_tick(lambda v: v + 1)
    return


@app.cell
async def _(ExecutableGraphOperations, contextlib, graph, io, set_tick, step):
    if step.value:
        with contextlib.redirect_stdout(io.StringIO()):
            await ExecutableGraphOperations.execute_graph(graph, max_transitions=3)
        set_tick(lambda v: v + 1)
    return


@app.cell
def _(UserRequest, graph, inject, level, set_tick):
    if inject.value:
        graph.place_named("Requests").tokens.append(UserRequest(level=level.value))
        set_tick(lambda v: v + 1)
    return


@app.cell
def _(ANOMALY_THRESHOLD, get_tick, graph, mo):
    get_tick()  # re-render whenever an action bumps the tick

    requests = graph.place_named("Requests").tokens
    readings = graph.place_named("Readings").tokens
    log = graph.place_named("Log").tokens

    def _row(c):
        flag = "🚨" if c.verdict == "ANOMALY" else ""
        who = "👤 user" if c.source == "user" else "auto"
        return f"| {flag} | {who} | {c.level:.3f} | {c.verdict} |"

    recent = log[-15:][::-1]
    table = "\n".join(
        ["| | source | level | verdict |", "|--|--|--|--|"] + [_row(c) for c in recent]
    )

    mo.md(
        f"""
        **Pending** — requests: `{len(requests)}` · readings: `{len(readings)}` ·
        anomaly threshold: `{ANOMALY_THRESHOLD}`

        **Log (newest first)**

        {table if recent else "_(nothing yet — press **Step ×3** or enable **Auto-run**)_"}
        """
    )
    return


if __name__ == "__main__":
    app.run()
