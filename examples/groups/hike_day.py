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
        # Hike: a flat net drawn in groups

        A hiker has one store of energy, the place **Energy**, and three concerns that all
        read it and change it (`examples/groups/hike.py`). Each concern is a **group**, drawn
        as a dashed frame titled with its name: **Walk** spends two energy on each leg of the
        trail, **Eat** gains three from a snack when the hiker is too tired to walk, and
        **Rest** gains one on a bench when there is nothing left to eat. `Energy` belongs to
        every concern at once, so it is in no group and stands outside the frames.

        A group is a label that only drawings read. The net runs exactly as it would with no
        groups: the frames gather its parts under names, and that is all. A part that is
        really separate, with its own tokens and its own turns, is a subnet place instead.

        **Nothing drives this net but its own rules.** Each **Step** is one firing of the
        engine's choosing. The guards leave one transition fireable at a time, so the day is
        always the same: walk, eat the apple, walk twice, rest on both benches, walk the last
        leg. After 7 firings the trail is walked and the hiker has no energy left.

        - **Step**: one firing.
        - **Repeat Step**: step until nothing can fire.
        - **◀ Back / Forward ▶**: move through the states already visited.
        - **↺ Reset**: start the day again.
        """
    )
    return


@app.cell
def _():
    import io

    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from hike import build_hike, energy_of, step

    return RustworkxGraph, RustworkxToGraphviz, build_hike, energy_of, io, step


@app.cell
def _(RustworkxGraph, RustworkxToGraphviz, io):
    def snapshot(session, caption):
        """The net with its groups framed and activation colours from its own history,
        appended to the history as ``(caption, png_bytes, width)``."""
        graph = session["graph"]
        node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
        picture = RustworkxToGraphviz.draw(
            RustworkxGraph.from_executable_graph(graph),
            node_attr_fn=node_attr_fn, edge_attr_fn=edge_attr_fn, method="dot",
        )
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        session["history"].append((caption, buffer.getvalue(), min(picture.width, 1400)))

    return (snapshot,)


@app.cell
def _(energy_of, snapshot, step):
    async def fire_one(session):
        """One firing, the engine's own choice. Returns 1, or 0 once nothing can fire."""
        graph = session["graph"]
        fired = await step(graph)
        if fired is None:
            return 0
        session["log"].append(fired)
        snapshot(session, f"Firing {len(session['log'])}: {fired}  ·  energy {energy_of(graph)}")
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
def _(build_hike, reset_button, snapshot):
    _ = reset_button  # start a fresh day whenever Reset is clicked
    session = {
        "graph": build_hike(),
        "history": [],
        "log": [],
        "cursor": 0,
    }
    snapshot(session, "Initial state: three energy, four legs to walk, an apple and two benches")
    return (session,)


@app.cell
async def _(back_button, fire_one, forward_button, mo, repeat_button, reset_button, session, step_button):
    if step_button.value:
        await fire_one(session)
        session["cursor"] = len(session["history"]) - 1
    elif repeat_button.value:
        for _ in range(100):  # the day takes 7 firings from the seed
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
