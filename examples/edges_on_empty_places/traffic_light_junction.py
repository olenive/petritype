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
        # Traffic light — every car waiting goes when the light turns green

        A light cycles red, green, amber and red again, and cars wait at it
        (`examples/edges_on_empty_places/traffic_light.py`, the conformance net
        `traffic-light`). The light is one place, `Light`, and its one token is its colour:
        `red`, `green` or `amber`, and the picture fills the place in that colour. Cars wait in
        `Waiting`, oldest first, and go to `Passed`. Two transitions, one that changes the
        light and one that moves the cars:

        - `Change` takes the light and puts the next colour back: red to green, green to amber,
          amber to red. It is unguarded, so the light cycles by itself.
        - `Go` **drains** `Waiting` (a `DrainEdge`: every car waiting, oldest first) and
          **reads** `Light` (a plain `SnapshotEdge`: the colour is read, not taken, so the light
          stays green), and is guarded on the light being green and someone waiting. Everyone
          who waited at red goes at once when the light is green; a car arriving while it is
          green goes straight through; a car arriving at amber or red waits.

        Why the guard needs someone waiting: a drain accepts an empty place, so without it `Go`
        could fire at every green taking nothing, and, declared first, it would win every step
        and the light would stick on green. With it, the step goes to `Change` whenever nobody
        is waiting.

        **Nothing drives this net but its own rules**, and cars arriving from outside. The light
        never stops, so the example follows a schedule of arrivals: the light goes green with
        nobody waiting, then amber; Car 1 and Car 2 arrive at amber and wait through red; the
        light goes green and `Go` drains both at once; Car 3 arrives while the light is green
        and goes straight through. After six firings every car has passed, and the light would
        go on cycling.

        - **Step** — one firing, the cars the schedule has due arriving first. Once the
          schedule is over it keeps the light going round.
        - **Repeat Step** — play the schedule to its end, one firing at a time.
        - **◀ Back / Forward ▶** — move through the states already visited.
        - **＋ Car** — a car arrives now: at green `Go` can take it straight through, at amber or
          red it waits for green.
        - **↺ Reset** — the light back to red, nobody waiting.

        **Reading the picture.** The last firing is outlined in magenta. A solid arrow consumes
        tokens; a dashed arrow reads a copy and leaves it in place. A hollow arrowhead marks an
        edge that accepts an empty place: the drain is solid and hollow. The plain read is
        dashed with a normal head.
        """
    )
    return


@app.cell
def _():
    import asyncio
    import io

    from rustworkx.visualization import graphviz_draw

    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from traffic_light import build_traffic_light, cars, colour, fire, fireable, put

    return (
        RustworkxGraph,
        RustworkxToGraphviz,
        asyncio,
        build_traffic_light,
        cars,
        colour,
        fire,
        fireable,
        graphviz_draw,
        io,
        put,
    )


@app.cell
def _(RustworkxToGraphviz, fireable, graphviz_draw, io):
    # The light's place is filled with the colour its token names.
    LAMPS = {"red": "tomato", "green": "limegreen", "amber": "orange"}

    def picture(session):
        """The junction as it stands, as ``(png_bytes, width)``."""
        graph = session["graph"]
        node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)

        def lit(node):
            attrs = node_attr_fn(node)
            if node.name == "Light" and hasattr(node, "tokens"):
                attrs["fillcolor"] = LAMPS.get(node.tokens[0], "gray80") if node.tokens else "gray80"
            return attrs

        # Left to right, like the road: the cars flow from Waiting to Passed through Go, and
        # the light sits beside them with its own loop through Change.
        image = graphviz_draw(
            session["pydigraph"], node_attr_fn=lit, edge_attr_fn=edge_attr_fn, method="dot",
            graph_attr={"rankdir": "LR"},
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), min(image.width, 900)

    def snapshot(session, caption):
        """Record the junction as it stands, ``(caption, png_bytes, width)``: the frames the
        notebook tests and the gallery replay. What is fireable is kept beside each."""
        png, width = picture(session)
        session["history"].append((caption, png, width))
        session["fireable"].append(fireable(session["graph"]))

    return picture, snapshot


@app.cell
def _(cars, colour, fire, put, snapshot):
    # The example's schedule: how many cars arrive before each firing, by firing number, as
    # in the transcript's script. Car 1 and Car 2 arrive at amber, before the third firing,
    # and wait through red; Car 3 arrives while the light is green, before the sixth.
    ARRIVALS = {3: 2, 6: 1}

    def arrive(session, count):
        """``count`` cars arrive, named in the order cars come in, as an ``inject`` puts them
        in. Returns their names and the light's colour when they came."""
        names = [f"Car {session['cars'] + n}" for n in range(1, count + 1)]
        session["cars"] += count
        light = colour(session["graph"])
        put(session["graph"], {"Waiting": names})
        return names, light

    def by_hand(session):
        """A car arrives now, outside the schedule; recorded as a state of its own."""
        (name,), light = arrive(session, 1)
        then = "Go can take it straight through" if light == "green" else "it waits for green"
        snapshot(session, f"{name} arrives at {light}: {then}")

    def over(session):
        """Whether the example is over: every car on the schedule has arrived, and nobody is
        waiting (a car put in by hand included)."""
        return session["firings"] >= max(ARRIVALS) and not session["graph"].place_named("Waiting").tokens

    async def fire_one(session, show=None, keep_going=False):
        """One firing of the example, the engine's own choice: the cars the schedule has due
        arrive first, and ``show`` (the notebook's animation; the tests pass none) is awaited
        with them. Returns 1, or 0 once every car has passed (the light never stops, so the
        cars say when the example is over) unless ``keep_going`` asks to watch the light go
        round."""
        if over(session) and not keep_going:
            return 0
        came = ""
        due = ARRIVALS.get(session["firings"] + 1, 0)
        if due:
            names, light = arrive(session, due)
            if show is not None:
                await show(f"{cars(names)} {'arrives' if due == 1 else 'arrive'} at {light}")
            came = f"{cars(names)} arrived at {light}. "
        what = await fire(session["graph"])
        if what is None:
            return 0
        session["firings"] += 1
        snapshot(session, f"Firing {session['firings']} — {came}{what}")
        return 1

    return by_hand, fire_one, over


@app.cell
def _(mo):
    step_button = mo.ui.run_button(label="Step", kind="neutral")
    repeat_button = mo.ui.run_button(label="Repeat Step", kind="success")
    back_button = mo.ui.run_button(label="◀ Back")
    forward_button = mo.ui.run_button(label="Forward ▶")
    car_button = mo.ui.run_button(label="＋ Car")
    reset_button = mo.ui.run_button(label="↺ Reset", kind="warn")
    return back_button, car_button, forward_button, repeat_button, reset_button, step_button


@app.cell
def _(RustworkxGraph, build_traffic_light, reset_button, snapshot):
    _ = reset_button  # the light back to red, nobody waiting, whenever Reset is clicked
    _graph = build_traffic_light(record_events=False)
    session = {
        "graph": _graph,
        "pydigraph": RustworkxGraph.from_executable_graph(_graph),
        "history": [],
        "fireable": [],
        "cursor": 0,
        "firings": 0,
        "cars": 0,
    }
    snapshot(session, "The light is red and nobody is waiting")
    return (session,)


@app.cell
async def _(
    asyncio,
    back_button,
    by_hand,
    car_button,
    fire_one,
    fireable,
    forward_button,
    mo,
    over,
    picture,
    repeat_button,
    reset_button,
    session,
    step_button,
):
    def _view(live=None, note=None):
        """The state at the cursor; or, while a firing plays, the junction as it stands, with
        ``live`` for its caption."""
        if live is None:
            _caption, _png, _width = session["history"][session["cursor"]]
            _now = session["fireable"][session["cursor"]]
            _where = f"state {session['cursor']} / {len(session['history']) - 1}"
        else:
            (_png, _width), _caption = picture(session), live
            _now, _where = fireable(session["graph"]), "arriving"
        _so_far = "\n".join(f"- {caption}" for caption, _p, _w in session["history"][1 : session["cursor"] + 1])
        return mo.vstack([
            mo.hstack(
                [step_button, repeat_button, back_button, forward_button, car_button, reset_button],
                justify="start",
            ),
            mo.md(
                f"**{_caption}**\n\n"
                f"Fireable now: **{', '.join(_now) or 'nothing'}**  ·  {_where}"
                + (f"\n\n_{note}_" if note else "")
            ),
            mo.hstack(
                [
                    mo.image(src=_png, width=_width),
                    mo.vstack([mo.md("**What has happened**"), mo.md(_so_far or "_nothing yet_")]),
                ],
                justify="start",
                align="start",
                gap=2,
            ),
        ])

    async def _show(caption):
        mo.output.replace(_view(live=caption))
        await asyncio.sleep(1.0)  # slow the animation for watchability (not the firing)

    def _latest():
        session["cursor"] = len(session["history"]) - 1

    _note = None
    if step_button.value:
        await fire_one(session, show=_show, keep_going=True)
        _latest()
    elif repeat_button.value:
        _latest()
        if over(session):
            _note = (
                "The example is over: every car has passed. Step keeps the light going round, "
                "＋ Car sends another car, ↺ Reset plays it again."
            )
        while await fire_one(session, show=_show):
            _latest()
            mo.output.replace(_view())
            await asyncio.sleep(1.0)  # slow the animation for watchability (not the firing)
    elif back_button.value:
        session["cursor"] = max(0, session["cursor"] - 1)
    elif forward_button.value:
        session["cursor"] = min(len(session["history"]) - 1, session["cursor"] + 1)
    elif car_button.value:
        by_hand(session)
        _latest()

    _view(note=_note)
    return


if __name__ == "__main__":
    app.run()
