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

        The city holds **vehicle tokens**, and each vehicle holds a **cabin**: a Petri net of
        its own (`examples/tokens_as_nets/transit.py`). The city is drawn on the left; on the
        right, the cabin of the bus we follow, found by its stable `net_id` wherever the bus is.

        **Nothing drives this net but its own rules.** Each **Step** is one firing of the
        engine's choosing: the city's first fireable transition, else the first cabin with
        anything to fire. The journey works because the vehicle's state is where its doors are.
        Each door is a token, the door's name, in one of two places of the cabin, `Doors Open`
        or `Doors Closed` (the bus has one door, the ferry two), so the cabin shows its phase:
        a door open and no announcement is boarding, a door open with an announcement is
        alighting, every door closed is under way. The city's guards read it: `Board` only while
        boarding, `Alight` only while alighting, `Depart` only once every door is closed. The
        cabin closes its doors, `Close Doors` one door a firing once everyone at the door has
        settled; `Arrive` opens them itself as it puts up the announcement, and
        `Clear Announcement` takes it down once everyone has left.

        Vehicles follow a loop, Depot, Central, Harbour, Airport. Passengers who alight go to
        `Arrived`, never back onto a platform. Seven passengers wait on three platforms; after
        45 firings all seven have arrived, and the vehicles keep going round, empty.

        - **Step** — one firing, outer or inner.
        - **Repeat Step** — step until everyone has arrived.
        - **◀ Back / Forward ▶** — move through the states already visited.
        - **↺ Reset** — rebuild the city.

        A thick grey line with a dot at each end is a *mutate edge*: the transition reads the
        place's front token and may change it in place. `Board` and `Alight` reach into a cabin
        through one; `Depart` and `Arrive` move the vehicle itself.
        """
    )
    return


@app.cell
def _():
    import io

    from PIL import Image
    from rustworkx.visualization import graphviz_draw

    from petritype.core.executable_graph_components import NestedNets
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from transit import PASSENGERS, build_city, everyone_arrived, step

    return (
        Image,
        NestedNets,
        PASSENGERS,
        RustworkxGraph,
        RustworkxToGraphviz,
        build_city,
        everyone_arrived,
        graphviz_draw,
        io,
        step,
    )


@app.cell
def _(Image, NestedNets, RustworkxGraph, RustworkxToGraphviz, graphviz_draw, io):
    def draw(graph, pydigraph=None):
        """A net as a PIL image, activation colours from its own history."""
        node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
        return graphviz_draw(
            pydigraph or RustworkxGraph.from_executable_graph(graph),
            node_attr_fn=node_attr_fn, edge_attr_fn=edge_attr_fn, method="dot",
        )

    def snapshot(session, caption):
        """The city and, beside it, the followed cabin, appended to the history as
        ``(caption, png_bytes, width)``."""
        city = draw(session["graph"], session["pydigraph"])
        found = NestedNets.find(session["graph"], session["cabin_id"])
        picture = city
        if found is not None:
            cabin = draw(found[1])
            picture = Image.new("RGB", (city.width + 40 + cabin.width, max(city.height, cabin.height)), "white")
            picture.paste(city, (0, 0))
            picture.paste(cabin, (city.width + 40, 0))
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        session["history"].append((caption, buffer.getvalue(), min(picture.width, 1400)))

    return (snapshot,)


@app.cell
def _(PASSENGERS, everyone_arrived, snapshot, step):
    async def fire_one(session, keep_going=False):
        """One firing, the engine's own choice. Returns 1, or 0 once everyone has arrived
        (the vehicles never stop, so the passengers say when the journey is over) unless
        ``keep_going`` asks to watch them go round."""
        graph = session["graph"]
        if everyone_arrived(graph) and not keep_going:
            return 0
        fired = await step(graph)
        if fired is None:
            return 0
        session["log"].append(fired)
        arrived = len(graph.place_named("Arrived").tokens)
        snapshot(session, f"Firing {len(session['log'])} — {fired}  ·  arrived {arrived} of {len(PASSENGERS)}")
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
def _(RustworkxGraph, build_city, reset_button, snapshot):
    _ = reset_button  # rebuild a fresh city whenever Reset is clicked
    _graph = build_city()
    session = {
        "graph": _graph,
        "pydigraph": RustworkxGraph.from_executable_graph(_graph),
        "cabin_id": _graph.place_named("Depot").tokens[0].cabin.net_id,
        "history": [],
        "log": [],
        "cursor": 0,
    }
    snapshot(session, "Initial state — seven passengers waiting, both vehicles boarding")
    return (session,)


@app.cell
async def _(back_button, fire_one, forward_button, mo, repeat_button, reset_button, session, step_button):
    if step_button.value:
        await fire_one(session, keep_going=True)
        session["cursor"] = len(session["history"]) - 1
    elif repeat_button.value:
        for _ in range(200):  # the journey takes 45 firings from the seed
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
