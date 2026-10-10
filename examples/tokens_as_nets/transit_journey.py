import marimo

__generated_with = "0.23.9"
app = marimo.App(width="full")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        r"""
        # Transit — a net whose tokens are nets

        The city holds **vehicle tokens**; each vehicle holds a **cabin**, a Petri net of its
        own (`examples/tokens_as_nets/transit.py`, the example of the nets-as-tokens design). The city is drawn
        on the left and, on the right, the cabin of the bus we are following, found by its
        stable `net_id` wherever the bus is standing. A vehicle token's label names its inner
        net and how many tokens it holds (`Vehicle(...) ⊂ cabin: 3`).

        **Step** runs the design's acceptance script one firing at a time: three passengers
        board at the depot, the cabin seats two and stands one, the bus departs and arrives at
        Central, the announcement sends everyone to the door, and two passengers alight. Outer
        firings are driven by hand where the script says so; the inner firings are the engine's
        own, taken when the outer net has nothing to fire. The log under the picture shows each
        inner firing under the path of the vehicle it happened in.

        - **Step** — the next firing of the script, outer or inner.
        - **Repeat Step** — run the whole script.
        - **◀ Back / Forward ▶** — move through the states already visited.
        - **↺ Reset** — rebuild the city.

        **Reading the edges.** An arrow moves tokens: a place-to-transition arrow is consumed
        when the transition fires, a transition-to-place arrow is produced into. A thick grey
        line with a dot at each end is a *mutate edge*: the transition reads the place's front
        token live and may change it, but the token stays where it is. `Board` and `Alight`
        reach into a vehicle's cabin through one; `Depart` and `Arrive` move the vehicle itself.
        """
    )
    return


@app.cell
def _():
    import io
    import time

    from PIL import Image
    from rustworkx.visualization import graphviz_draw

    from petritype.core.events import Fired, Nested
    from petritype.core.executable_graph_components import NestedNets
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.runtime import RunContext, Runner
    from transit import HELD_BACK, build_city, fire

    return (
        Fired,
        HELD_BACK,
        Image,
        Nested,
        NestedNets,
        RunContext,
        Runner,
        RustworkxGraph,
        RustworkxToGraphviz,
        build_city,
        fire,
        graphviz_draw,
        io,
        time,
    )


@app.cell
def _(Image, RustworkxGraph, RustworkxToGraphviz, graphviz_draw, io, mo):
    def draw(graph, pydigraph=None):
        """A net as a PIL image, activation colours from its own history."""
        node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
        return graphviz_draw(
            pydigraph or RustworkxGraph.from_executable_graph(graph),
            node_attr_fn=node_attr_fn, edge_attr_fn=edge_attr_fn, method="dot",
        )

    def side_by_side(left, right, gap=40):
        """The city and the cabin in one picture, tops aligned."""
        canvas = Image.new("RGB", (left.width + gap + right.width, max(left.height, right.height)), "white")
        canvas.paste(left, (0, 0))
        canvas.paste(right, (left.width + gap, 0))
        return canvas

    def to_frame(caption, pil_image):
        """Snapshot a PIL diagram as (caption, png_bytes, display_width_px)."""
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        return (caption, buffer.getvalue(), min(pil_image.width, 1400))

    def render(session):
        caption, png, width = session["history"][session["cursor"]]
        total = len(session["history"]) - 1
        return mo.vstack([
            mo.md(f"**{caption}**  ·  state {session['cursor']} / {total}"),
            mo.image(src=png, width=width),
        ])

    return draw, render, side_by_side, to_frame


@app.cell
def _(NestedNets, draw, side_by_side, to_frame):
    # The design's script: outer firings by hand, "quiet" lets the engine fire the inner nets.
    SCRIPT = [
        ("fire", "Board Depot"), ("fire", "Board Depot"), ("fire", "Board Depot"), ("quiet", None),
        ("fire", "Depart Depot"), ("quiet", None),
        ("fire", "Arrive"), ("quiet", None),
        ("fire", "Alight Central"), ("fire", "Alight Central"),
    ]

    def followed_cabin(session):
        """The cabin of the bus we follow, wherever the bus is now."""
        found = NestedNets.find(session["graph"], session["cabin_id"])
        return found[1] if found is not None else None

    def snapshot(session, caption):
        city = draw(session["graph"], session["pydigraph"])
        cabin = followed_cabin(session)
        picture = side_by_side(city, draw(cabin)) if cabin is not None else city
        session["history"].append(to_frame(caption, picture))

    return SCRIPT, followed_cabin, snapshot


@app.cell
def _(Fired, Nested, Runner, fire, snapshot):
    async def fire_one(session):
        """The next firing of the script, outer or inner. Returns the number fired (0 or 1);
        0 means the script is done, which is the signal to stop auto-stepping."""
        graph, script = session["graph"], session["script"]
        while script:
            kind, name = script[0]
            if kind == "fire":
                script.pop(0)
                await fire(graph, name)
                fired = 1
            else:
                summary = await Runner.run(session["ctx"], stop_after_n_firings=1)
                if summary.n_fired == 0:
                    script.pop(0)
                    continue
                fired = 1
            for event in graph.drain_events():
                if isinstance(event, Fired):
                    session["log"].append(event.transition)
                elif isinstance(event, Nested) and isinstance(event.event, Fired):
                    session["log"].append(f"{'/'.join(event.path)}: {event.event.transition}")
            index = len(session["history"])
            snapshot(session, f"Firing {index} — {graph.last_fired}")
            return fired
        return 0

    return (fire_one,)


@app.cell
def _(mo):
    step_button = mo.ui.run_button(label="Step", kind="neutral")
    repeat_button = mo.ui.run_button(label="Repeat Step", kind="success")
    back_button = mo.ui.run_button(label="◀ Back")
    forward_button = mo.ui.run_button(label="Forward ▶")
    reset_button = mo.ui.run_button(label="↺ Reset", kind="warn")
    return back_button, forward_button, repeat_button, reset_button, step_button


@app.cell
def _(HELD_BACK, RunContext, RustworkxGraph, SCRIPT, build_city, reset_button, snapshot):
    _ = reset_button  # rebuild a fresh city whenever Reset is clicked
    _graph = build_city()
    session = {
        "graph": _graph,
        "pydigraph": RustworkxGraph.from_executable_graph(_graph),
        "ctx": RunContext(graph=_graph, disabled=set(HELD_BACK)),
        "cabin_id": _graph.place_named("Depot").tokens[0].cabin.net_id,
        "script": list(SCRIPT),
        "history": [],
        "log": [],
        "cursor": 0,
    }
    snapshot(session, "Initial state — three passengers waiting at the depot")
    return (session,)


@app.cell
async def _(
    back_button,
    fire_one,
    forward_button,
    mo,
    render,
    repeat_button,
    reset_button,
    session,
    step_button,
    time,
):
    _controls = mo.hstack(
        [step_button, repeat_button, back_button, forward_button, reset_button],
        justify="start",
    )

    def _view():
        log = "\n".join(f"{n}. {line}" for n, line in enumerate(session["log"][:session["cursor"]], 1))
        return mo.vstack([_controls, render(session), mo.md(f"**Firings so far**\n\n{log or '_none_'}")])

    if step_button.value:
        await fire_one(session)
        session["cursor"] = len(session["history"]) - 1
    elif repeat_button.value:
        for _ in range(100):
            if not await fire_one(session):
                break
            session["cursor"] = len(session["history"]) - 1
            mo.output.replace(_view())
            time.sleep(0.6)
        session["cursor"] = len(session["history"]) - 1
    elif back_button.value:
        session["cursor"] = max(0, session["cursor"] - 1)
    elif forward_button.value:
        session["cursor"] = min(len(session["history"]) - 1, session["cursor"] + 1)

    _view()
    return


if __name__ == "__main__":
    app.run()
