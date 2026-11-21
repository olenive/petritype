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
        # Levels: a world whose places hold nets

        The world has a **Door**, where three heroes wait, and **Outside**, where they end up.
        Between them stand two **subnet places**, `Level 1` and `Level 2`: each holds a net of
        its own, built by the one template `Level` (`examples/subnet_places/levels.py`). The
        world is drawn on top, each level closed as one box with a cell per **port place**,
        showing the port's name and how many heroes it holds; below it, each level's own net,
        where a port is drawn with a second ring, the border seen from inside.

        The world reaches a level only through its ports. `Enter` puts a hero into level 1's
        `Lobby`, `Climb` takes one from level 1's `Exit Hall` into level 2's `Lobby`, and
        `Leave` takes one from level 2's `Exit Hall` outside; the arrows end at those cells.
        Inside, `Fight` takes a hero from the lobby into the `Arena`, which the world cannot
        see, and gives them the level's reward in experience, and `Win` takes them on to the
        exit hall. A hero resting in a port is one token that both nets see, with one id.

        **Nothing drives this net but its own rules.** Each **Step** is one firing of the
        engine's choosing. Both levels keep the default stepping rule, `inner_first`: a level
        fires until it is at rest before the world fires again, so each hero clears level 1
        before the next one enters. After 21 firings every hero is outside.

        - **Step**: one firing, in the world or in a level.
        - **Repeat Step**: step until nothing can fire.
        - **◀ Back / Forward ▶**: move through the states already visited.
        - **↺ Reset**: rebuild the world.
        """
    )
    return


@app.cell
def _():
    import io

    from PIL import Image
    from rustworkx.visualization import graphviz_draw

    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from levels import build_world, label, step

    return (
        Image,
        RustworkxGraph,
        RustworkxToGraphviz,
        build_world,
        graphviz_draw,
        io,
        label,
        step,
    )


@app.cell
def _(Image, RustworkxGraph, RustworkxToGraphviz, graphviz_draw, io):
    def draw(graph):
        """A net as a PIL image, activation colours from its own history."""
        node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
        return graphviz_draw(
            RustworkxGraph.from_executable_graph(graph),
            node_attr_fn=node_attr_fn, edge_attr_fn=edge_attr_fn, method="dot",
        )

    def snapshot(session, caption):
        """The world on top and its two levels side by side below it, appended to the
        history as ``(caption, png_bytes, width)``."""
        world = session["graph"]
        top = draw(world)
        levels = [draw(world.subnet(name)) for name in ("Level 1", "Level 2")]
        row_width = sum(level.width for level in levels) + 40 * (len(levels) - 1)
        row_height = max(level.height for level in levels)
        picture = Image.new("RGB", (max(top.width, row_width), top.height + 40 + row_height), "white")
        picture.paste(top, ((picture.width - top.width) // 2, 0))
        left = 0
        for level in levels:
            picture.paste(level, (left, top.height + 40))
            left += level.width + 40
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        session["history"].append((caption, buffer.getvalue(), min(picture.width, 1400)))

    return (snapshot,)


@app.cell
def _(label, snapshot, step):
    async def fire_one(session):
        """One firing, the engine's own choice. Returns 1, or 0 once nothing can fire."""
        graph = session["graph"]
        fired = await step(graph)
        if fired is None:
            return 0
        session["log"].append(label(fired))
        outside = len(graph.place_named("Outside").tokens)
        snapshot(session, f"Firing {len(session['log'])}: {label(fired)}  ·  outside {outside} of 3")
        return 1

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
def _(build_world, reset_button, snapshot):
    _ = reset_button  # rebuild a fresh world whenever Reset is clicked
    session = {
        "graph": build_world(),
        "history": [],
        "log": [],
        "cursor": 0,
    }
    snapshot(session, "Initial state: three heroes at the door, both levels empty")
    return (session,)


@app.cell
async def _(back_button, fire_one, forward_button, mo, repeat_button, reset_button, session, step_button):
    if step_button.value:
        await fire_one(session)
        session["cursor"] = len(session["history"]) - 1
    elif repeat_button.value:
        for _ in range(100):  # the journey takes 21 firings from the seed
            if not await fire_one(session):
                break
        session["cursor"] = len(session["history"]) - 1
    elif back_button.value:
        session["cursor"] = max(0, session["cursor"] - 1)
    elif forward_button.value:
        session["cursor"] = min(len(session["history"]) - 1, session["cursor"] + 1)

    _caption, _png, _width = session["history"][session["cursor"]]
    _log = "\n".join(f"{n}. {line}" for n, line in enumerate(session["log"][: session["cursor"]], 1))
    mo.vstack([
        mo.hstack([step_button, repeat_button, back_button, forward_button, reset_button], justify="start"),
        mo.md(f"**{_caption}**  ·  state {session['cursor']} / {len(session['history']) - 1}"),
        mo.image(src=_png, width=_width),
        mo.md(f"**Firings so far**\n\n{_log or '_none_'}"),
    ])
    return


if __name__ == "__main__":
    app.run()
