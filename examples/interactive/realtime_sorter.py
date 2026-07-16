import marimo

__generated_with = "0.23.9"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        r"""
        # Real-time parcel sorter — a net running on a clock you poke live

        The same `Inbox → Sort → Sorted` net, but now it **runs on its own**: marimo's
        `mo.ui.refresh` timer is the clock, firing `Runner.step` every ~0.25 s and draining any
        queued input each tick. You inject **while it runs**:

        - **📦 New parcel** → `Extend("Inbox", [Parcel(...)])` (applied immediately on click).
        - **⏯ Toggle sorter** → `Disable`/`Enable("Sort")`. Toggle it **off** and watch parcels
          pile up in the Inbox; toggle **on** and watch them drain, one per tick.
        - **🗑 Clear sorted** → `SetTokens("Sorted", [])`.
        - **↺ Reset** → fresh net.

        Buttons only *produce commands onto the inbox*; the `Runner` is the only thing that
        touches the net. Clicks apply at once; the timer auto-advances any backlog and refreshes
        the view at its interval.

        > In a notebook, marimo owns the clock, so the timer drives `Runner.step`.
        > `Runner.run_indefinitely(ctx, tick=...)` is the equivalent for when **you** own the
        > loop — a server or script — ticking on its own asyncio clock until `ctx.stop` is set.
        > Petritype fires as fast as you ask; the visible cadence here is marimo's refresh rate.
        """
    )
    return


@app.cell
def _():
    import asyncio
    import io

    from pydantic import BaseModel
    from rustworkx.visualization import graphviz_draw

    from petritype.core.executable_graph_components import (
        ArgumentEdgeToTransition,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
    )
    from petritype.core.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.runtime import Disable, Enable, Extend, RunContext, Runner, SetTokens

    return (
        ArgumentEdgeToTransition,
        BaseModel,
        Disable,
        Enable,
        ExecutableGraphOperations,
        Extend,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
        RunContext,
        Runner,
        RustworkxGraph,
        RustworkxToGraphviz,
        SetTokens,
        asyncio,
        graphviz_draw,
        io,
    )


@app.cell
def _(BaseModel):
    class Parcel(BaseModel):
        id: int

    return (Parcel,)


@app.cell
def _(
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    Parcel,
    ReturnedEdgeFromTransition,
    RustworkxGraph,
):
    def sort_parcel(parcel: Parcel) -> Parcel:
        return parcel

    def build_graph():
        nodes_and_edges = [
            ListPlaceNode(name="Inbox", type=Parcel, tokens=[]),
            ArgumentEdgeToTransition("Inbox", "Sort", "parcel"),
            FunctionTransitionNode(name="Sort", function=sort_parcel),
            ReturnedEdgeFromTransition("Sort", "Sorted"),
            ListPlaceNode(name="Sorted", type=Parcel),
        ]
        graph = ExecutableGraphOperations.construct_graph(nodes_and_edges)
        return graph, RustworkxGraph.from_executable_graph(graph)

    return (build_graph,)


@app.cell
def _(io, mo):
    def half_image(pil_image):
        """Render a PIL image at ~three-quarters its native width so it fits on screen."""
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        return mo.image(src=buffer.getvalue(), width=pil_image.width * 3 // 4)

    return (half_image,)


@app.cell
def _(RustworkxToGraphviz, graphviz_draw, half_image):
    def draw(session):
        """Re-render the live net; activation colours read the current marking."""
        _graph = session["ctx"].graph
        _na, _ea = RustworkxToGraphviz.activation_coloured_attr_functions(_graph)
        return half_image(
            graphviz_draw(session["pydigraph"], node_attr_fn=_na, edge_attr_fn=_ea, method="dot")
        )

    return (draw,)


@app.cell
def _(mo):
    # Defined here, displayed in the render cell so the controls sit right above the plot.
    refresh = mo.ui.refresh(default_interval="0.25s")
    add_button = mo.ui.run_button(label="📦 New parcel", kind="neutral")
    toggle_button = mo.ui.run_button(label="⏯ Toggle sorter", kind="neutral")
    clear_button = mo.ui.run_button(label="🗑 Clear sorted", kind="warn")
    reset_button = mo.ui.run_button(label="↺ Reset", kind="warn")
    return add_button, clear_button, refresh, reset_button, toggle_button


@app.cell
def _(RunContext, asyncio, build_graph, reset_button):
    _ = reset_button  # rebuild a fresh net + inbox whenever Reset is clicked
    _graph, _pydigraph = build_graph()
    session = {
        "ctx": RunContext(graph=_graph, inbox=asyncio.Queue()),
        "pydigraph": _pydigraph,
        "next_id": 1,
    }
    return (session,)


@app.cell
async def _(
    Disable,
    Enable,
    Extend,
    Parcel,
    Runner,
    SetTokens,
    add_button,
    clear_button,
    draw,
    mo,
    refresh,
    reset_button,
    session,
    toggle_button,
):
    # This cell is the clock (re-runs on each `refresh` tick) AND the input handler (re-runs on
    # each button click). Handling clicks here means a command is enqueued and applied in the
    # same run — no waiting for the next tick.
    _ = refresh.value
    _ctx = session["ctx"]

    if add_button.value:
        _ctx.inbox.put_nowait(Extend("Inbox", [Parcel(id=session["next_id"])]))
        session["next_id"] += 1
    elif toggle_button.value:
        _ctx.inbox.put_nowait(Enable("Sort") if "Sort" in _ctx.disabled else Disable("Sort"))
    elif clear_button.value:
        _ctx.inbox.put_nowait(SetTokens("Sorted", []))

    await Runner.step(_ctx)  # drain queued input + fire one transition

    _controls = mo.hstack(
        [add_button, toggle_button, clear_button, reset_button, refresh], justify="start"
    )
    _sorter = "🟥 OFF" if "Sort" in _ctx.disabled else "🟩 ON"
    _inbox_n = len(_ctx.graph.place_named("Inbox").tokens)
    _sorted_ids = [p.id for p in _ctx.graph.place_named("Sorted").tokens]
    mo.vstack(
        [
            _controls,
            mo.md(
                f"**Sorter:** {_sorter}  ·  **Inbox waiting:** {_inbox_n}  ·  "
                f"**Sorted:** {_sorted_ids}"
            ),
            draw(session),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
