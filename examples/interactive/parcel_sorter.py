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
        # Interactive parcel sorter — a declarative control-map

        A tiny net — `Inbox → Sort → Sorted` — that you **poke while it runs**. The input
        controls are declared as a **control-map** `{name: ControlSpec}`, kept *off* the net, and
        rendered by `petritype.marimo_controls` — instead of hand-wiring each button. Every
        control only *produces a command onto the inbox*; the `Runner` is the only thing that
        touches the graph:

        - **📦 New parcel** (button) → `Extend("Inbox", [Parcel(...)])`.
        - **Sorter on** (toggle) → `Enable` / `Disable("Sort")`.
        - **🗑 Clear sorted** (button) → `SetTokens("Sorted", [])`.
        - **▶ Sort one** fires one transition; **↺ Reset** rebuilds.

        Add a few parcels, sort them one at a time, then flip the **Sorter** toggle off and
        watch stepping do nothing until you turn it back on.
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
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.marimo_controls import build_controls, controls_row, drain_controls
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.runtime import ControlSpec, Disable, Enable, Extend, RunContext, Runner, SetTokens

    return (
        ArgumentEdgeToTransition,
        BaseModel,
        ControlSpec,
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
        build_controls,
        controls_row,
        drain_controls,
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
    # Step / Reset are not inbox commands (one fires, one rebuilds), so they stay plain buttons.
    step_button = mo.ui.run_button(label="▶ Sort one", kind="success")
    reset_button = mo.ui.run_button(label="↺ Reset", kind="warn")
    return reset_button, step_button


@app.cell
def _(RunContext, asyncio, build_graph, reset_button):
    _ = reset_button  # rebuild a fresh net + inbox whenever Reset is clicked
    _graph, _pydigraph = build_graph()
    session = {
        "ctx": RunContext(graph=_graph, inbox=asyncio.Queue()),
        "pydigraph": _pydigraph,
        "next_id": 1,
        "last_values": {},
    }
    return (session,)


@app.cell
def _(ControlSpec, Disable, Enable, Extend, Parcel, SetTokens, build_controls, session):
    # The control-map: declarative {name: ControlSpec}. Each `to_command` maps a widget value
    # to an inbox command (None = nothing to do). Rebuilt on Reset (depends on `session`).
    def _add(clicked):
        if not clicked:
            return None
        pid = session["next_id"]
        session["next_id"] += 1
        return Extend("Inbox", [Parcel(id=pid)])

    control_map = {
        "add": ControlSpec("Inbox", "button", "📦 New parcel", _add),
        "sorter": ControlSpec(
            "Sort", "toggle", "Sorter on",
            lambda on: Enable("Sort") if on else Disable("Sort"), {"value": True},
        ),
        "clear": ControlSpec(
            "Sorted", "button", "🗑 Clear sorted",
            lambda clicked: SetTokens("Sorted", []) if clicked else None, {"kind": "warn"},
        ),
    }
    controls = build_controls(control_map)
    return control_map, controls


@app.cell
async def _(
    Runner,
    control_map,
    controls,
    controls_row,
    draw,
    drain_controls,
    mo,
    reset_button,
    session,
    step_button,
):
    _ctx = session["ctx"]
    # Turn any control interaction into inbox commands (buttons on click, toggles on change).
    drain_controls(control_map, controls, _ctx, session["last_values"])
    if step_button.value:
        await Runner.step(_ctx)       # drain the inbox, then fire one transition
    else:
        Runner.drain_inbox(_ctx)      # apply control commands immediately (no fire)

    _sorter = "🟥 OFF" if "Sort" in _ctx.disabled else "🟩 ON"
    _inbox_n = len(_ctx.graph.place_named("Inbox").tokens)
    _sorted_ids = [p.id for p in _ctx.graph.place_named("Sorted").tokens]
    mo.vstack(
        [
            mo.hstack([controls_row(controls, control_map), step_button, reset_button], justify="start"),
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
