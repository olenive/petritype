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
        # Execution modes — **sequential**

        Two **independent** branches, each a 3-stage chain `Start → s1 → s2 → s3 → Done`,
        every stage taking ~0.4 s. This notebook drives the net with
        `RunContext(mode=ExecutionMode.SEQUENTIAL)`: one transition fires and fully completes
        before the next is chosen. **Watch** the token walk all the way down branch A, *then*
        down branch B — total wall-clock ≈ the **sum** of the durations (~2.4 s).

        The net, the transition functions, and the `Runner` are identical to
        `02_concurrent.py` — the **only** difference is `ExecutionMode.SEQUENTIAL` vs
        `ExecutionMode.CONCURRENT`. That's the whole point: **one definition, two ways to run.**
        """
    )
    return


@app.cell
def _():
    import asyncio
    import io

    from rustworkx.visualization import graphviz_draw

    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.runtime import ExecutionMode, RunContext, Runner

    import net

    return ExecutionMode, RunContext, Runner, RustworkxToGraphviz, asyncio, graphviz_draw, io, net


@app.cell
def _(io, mo):
    def half_image(pil_image):
        """Render a PIL image at ~three-quarters its native width so it fits on screen."""
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        return mo.image(src=buffer.getvalue(), width=pil_image.width * 3 // 4)

    return (half_image,)


@app.cell
async def _(
    ExecutionMode, RunContext, Runner, RustworkxToGraphviz, asyncio, graphviz_draw, half_image, io, mo, net
):
    net.reset_timeline()
    _graph, _pydigraph = net.build_graph()

    def _draw(graph):
        # Colour by current activation; the attr functions read the live marking, so the
        # same pydigraph re-renders the up-to-date state each step.
        _na, _ea = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
        return half_image(graphviz_draw(_pydigraph, node_attr_fn=_na, edge_attr_fn=_ea, method="dot"))

    async def _observe(graph):
        mo.output.replace(mo.vstack([mo.md("### Sequential — running…"), _draw(graph)]))
        await asyncio.sleep(0.5)  # slow the animation for watchability (not the firing)

    _ctx = RunContext(graph=_graph, mode=ExecutionMode.SEQUENTIAL, observers=(_observe,))
    await Runner.run_to_completion(_ctx)

    # Resting view: final marking + the real timeline (animation pauses excluded).
    mo.vstack(
        [
            _draw(_graph),
            mo.md("### Sequential timeline"),
            mo.md(net.render_timeline(net.TIMELINE)),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
