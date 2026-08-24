import marimo

__generated_with = "0.23.9"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # Read arcs in action — a live, adjustable scaler

    `Scale` **consumes** an item from `Inbox`, **reads** two places *without consuming them*,
    and writes `item × multiplier` to `Outbox`:

    - **`Multiplier`** is read via a **`SnapshotEdge`** (drawn **dashed, no arrowhead**) — a
      read-only parameter. Adjust it live with ➖ / ➕ and watch later items scale
      differently; the place is never consumed and the transition can't disturb it.
    - **`Processed`** is a running total updated via a **`MutateEdge`** (drawn **dashed with a
      diamond head**) — `Scale` increments the tally *in place* each fire, without consuming it.

    The net runs on marimo's refresh clock: add a few items and watch them auto-scale one per
    tick. The two **dashed** edges are the read arcs — distinct from the solid consuming /
    producing arrows.
    """)
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
        MutateEdge,
        ReturnedEdgeFromTransition,
        SnapshotEdge,
    )
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.runtime import Extend, RunContext, Runner, SetTokens

    return (
        ArgumentEdgeToTransition,
        BaseModel,
        ExecutableGraphOperations,
        Extend,
        FunctionTransitionNode,
        ListPlaceNode,
        MutateEdge,
        ReturnedEdgeFromTransition,
        RunContext,
        Runner,
        RustworkxGraph,
        RustworkxToGraphviz,
        SetTokens,
        SnapshotEdge,
        asyncio,
        graphviz_draw,
        io,
    )


@app.cell
def _(BaseModel):
    class Tally(BaseModel):
        n: int

    return (Tally,)


@app.cell
def _(
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReturnedEdgeFromTransition,
    RustworkxGraph,
    SnapshotEdge,
    Tally,
):
    def scale(item: int, factor: int, tally: Tally) -> int:
        tally.n += 1  # MutateEdge: increment the running total *in place*
        return item * factor  # factor read via SnapshotEdge (non-consuming)

    def build_graph():
        nodes_and_edges = [
            ListPlaceNode(name="Inbox", type=int, tokens=[]),
            ListPlaceNode(name="Multiplier", type=int, tokens=[2]),
            ListPlaceNode(name="Processed", type=Tally, tokens=[Tally(n=0)]),
            ArgumentEdgeToTransition("Inbox", "Scale", "item"),
            SnapshotEdge("Multiplier", "Scale", "factor"),
            MutateEdge("Processed", "Scale", "tally"),
            FunctionTransitionNode(name="Scale", function=scale),
            ReturnedEdgeFromTransition("Scale", "Outbox"),
            ListPlaceNode(name="Outbox", type=int),
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
        _graph = session["ctx"].graph
        _na, _ea = RustworkxToGraphviz.activation_coloured_attr_functions(_graph)
        return half_image(
            graphviz_draw(session["pydigraph"], node_attr_fn=_na, edge_attr_fn=_ea, method="dot")
        )

    return (draw,)


@app.cell
def _(mo):
    refresh = mo.ui.refresh(default_interval="0.3s")
    add_button = mo.ui.run_button(label="📦 Add item", kind="neutral")
    minus_button = mo.ui.run_button(label="➖ Multiplier", kind="neutral")
    plus_button = mo.ui.run_button(label="➕ Multiplier", kind="neutral")
    reset_button = mo.ui.run_button(label="↺ Reset", kind="warn")
    return add_button, minus_button, plus_button, refresh, reset_button


@app.cell
def _(RunContext, asyncio, build_graph, reset_button):
    _ = reset_button  # rebuild a fresh net + inbox whenever Reset is clicked
    _graph, _pydigraph = build_graph()
    session = {
        "ctx": RunContext(graph=_graph, inbox=asyncio.Queue()),
        "pydigraph": _pydigraph,
        "next_item": 1,
    }
    return (session,)


@app.cell
async def _(
    Extend,
    Runner,
    SetTokens,
    add_button,
    draw,
    minus_button,
    mo,
    plus_button,
    refresh,
    reset_button,
    session,
):
    _ = refresh.value  # the clock — re-run each refresh interval
    _ctx = session["ctx"]

    if add_button.value:
        _ctx.inbox.put_nowait(Extend("Inbox", [session["next_item"]]))
        session["next_item"] += 1
    elif minus_button.value:
        _cur = _ctx.graph.place_named("Multiplier").tokens[0]
        _ctx.inbox.put_nowait(SetTokens("Multiplier", [max(1, _cur - 1)]))
    elif plus_button.value:
        _cur = _ctx.graph.place_named("Multiplier").tokens[0]
        _ctx.inbox.put_nowait(SetTokens("Multiplier", [_cur + 1]))

    await Runner.step(_ctx)  # drain queued input + scale one item using the current multiplier

    _controls = mo.hstack(
        [add_button, minus_button, plus_button, reset_button, refresh], justify="start"
    )
    _mult = _ctx.graph.place_named("Multiplier").tokens[0]
    _processed = _ctx.graph.place_named("Processed").tokens[0].n
    _inbox_n = len(_ctx.graph.place_named("Inbox").tokens)
    _outbox = _ctx.graph.place_named("Outbox").tokens
    mo.vstack(
        [
            _controls,
            mo.md(
                f"**Multiplier** (SnapshotEdge): ×{_mult}  ·  **Processed** (MutateEdge): "
                f"{_processed}  ·  **Inbox:** {_inbox_n}  ·  **Outbox:** {_outbox}"
            ),
            draw(session),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
