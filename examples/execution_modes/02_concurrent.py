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
        # Execution modes — **concurrent**

        The **same** net as `01_sequential.py` — two independent branches, each a 3-stage
        chain `Start → s1 → s2 → s3 → Done`, every stage ~0.4 s — driven with
        `RunContext(mode=ExecutionMode.CONCURRENT)`: each enabled transition's tokens are
        consumed immediately and its body runs as a background task, depositing on completion.
        **Watch** both branches advance in lockstep, one stage at a time — total wall-clock ≈
        the **max** of the durations (~1.2 s) rather than the sum.

        The net, the transition functions, and the `Runner` are identical to
        `01_sequential.py` — the **only** difference is `ExecutionMode.CONCURRENT` vs
        `ExecutionMode.SEQUENTIAL`. **One definition, two ways to run.**
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
        mo.output.replace(mo.vstack([mo.md("### Concurrent — running…"), _draw(graph)]))
        await asyncio.sleep(0.5)  # slow the animation for watchability (not the firing)

    _ctx = RunContext(graph=_graph, mode=ExecutionMode.CONCURRENT, observers=(_observe,))
    await Runner.run_to_completion(_ctx)

    # Resting view: final marking + the real timeline (animation pauses excluded).
    mo.vstack(
        [
            _draw(_graph),
            mo.md("### Concurrent timeline"),
            mo.md(net.render_timeline(net.TIMELINE)),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
