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
        # Shop — promotions that run while orders come in

        A shop prices each order as it comes in, with whatever promotions are running at that
        moment (`examples/edges_on_empty_places/priced_shop.py`, the shop of
        `examples/edges_on_empty_places/shop.py` with prices). It shows an input edge that does
        not hold its transition back when the place it reads is empty:

        - `Price Order` takes the next order and **surveys** `Promotions` (a `SurveyEdge`): it is
          handed a copy of every promotion running, zero or more, and they keep running.

        **Try it.**

        1. Place an order with no promotion running: it is priced at full price.
        2. Switch a promotion or two on. Nothing fires: a promotion is a token in `Promotions`,
           and `Price Order` waits for an order however many promotions are running.
        3. Place another order: its receipt shows what each running promotion took off. Orders
           already priced keep their price.
        4. Switch a promotion off: it leaves `Promotions`, and the next order is priced without
           it.

        **▶ Play the example day** tells the same story by itself, one firing at a time.
        **↺ Reset** opens a fresh shop.

        **Reading the picture.** The last firing is outlined in magenta. A solid arrow consumes
        tokens; a dashed arrow reads a copy and leaves them in place. The survey's hollow
        arrowhead marks an edge that also accepts an empty place.

        The edge that *takes* every token of a place, zero or more, has a notebook of its own
        beside this one: the traffic light (`traffic_light_junction.py`), where every car
        waiting goes when the light turns green.
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
    from petritype.runtime import Extend, RunContext, Runner, SetTokens
    from priced_shop import (
        BASKETS,
        PROMOTIONS,
        SCRIPT,
        Order,
        Promotion,
        Receipt,
        build_priced_shop,
        fireable,
        money,
        order,
    )

    return (
        BASKETS,
        Extend,
        Order,
        PROMOTIONS,
        Promotion,
        Receipt,
        RunContext,
        Runner,
        RustworkxGraph,
        RustworkxToGraphviz,
        SCRIPT,
        SetTokens,
        asyncio,
        build_priced_shop,
        fireable,
        graphviz_draw,
        io,
        money,
        order,
    )


@app.cell
def _(Order, Promotion, Receipt, RustworkxToGraphviz, graphviz_draw, io, money):
    def label(token):
        """A token as the picture shows it."""
        if isinstance(token, Order):
            return f"#{token.number} {token.basket} {money(token.subtotal)}"
        if isinstance(token, Promotion):
            return token.name
        if isinstance(token, Receipt):
            saved = f", saved {money(token.saved)}" if token.saved else ""
            return f"#{token.order.number} {token.order.basket}: {money(token.total)}{saved}"
        return str(token)

    def picture(session):
        """The shop as it stands, as ``(png_bytes, width)``."""
        graph = session["graph"]
        node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph, label)
        image = graphviz_draw(session["pydigraph"], node_attr_fn=node_attr_fn, edge_attr_fn=edge_attr_fn, method="dot")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), min(image.width, 900)

    def snapshot(session, caption):
        """Record the shop as it stands, ``(caption, png_bytes, width)``: the frames the
        notebook tests and the gallery replay."""
        png, width = picture(session)
        session["history"].append((caption, png, width))

    return picture, snapshot


@app.cell
def _(Extend, Runner, SCRIPT, SetTokens, fireable, money, snapshot):
    def running(session):
        """The names of the promotions running, oldest first."""
        return [p.name for p in session["graph"].place_named("Promotions").tokens]

    def put(session, tokens):
        """Put tokens in, as the protocol's ``inject`` does, through the runner's inbox.
        Orders are numbered as they come in, and a promotion already running is not started
        twice. Returns what came in, in words."""
        ctx = session["ctx"]
        said = []
        for place, values in tokens.items():
            if place == "Orders":
                values = [o.model_copy(update={"number": session["orders"] + i}) for i, o in enumerate(values, 1)]
                session["orders"] += len(values)
                said += [f"order {o.number} ({o.basket}, {money(o.subtotal)})" for o in values]
            else:  # Promotions
                values = [p for p in values if p.name not in running(session)]
                said += [f"{p.name} starts" for p in values]
            if values:
                ctx.inbox.put_nowait(Extend(place, list(values)))
        Runner.drain_inbox(ctx)
        return ", ".join(said)

    def withdraw(session, name):
        """Take a promotion off: the switch, not the net, ends it."""
        ctx = session["ctx"]
        keep = [p for p in ctx.graph.place_named("Promotions").tokens if p.name != name]
        ctx.inbox.put_nowait(SetTokens("Promotions", keep))
        Runner.drain_inbox(ctx)

    def explain(graph, fired, before):
        """What a firing did, read off the net after it; ``before`` is what was running."""
        if fired == "Price Order":
            receipt = graph.place_named("Priced").tokens[-1]
            o = receipt.order
            applied = [s.promotion for s in receipt.savings]
            if not before:
                return (
                    f"Price Order priced order {o.number} at full price, {money(receipt.total)}: "
                    "no promotion is running, so the survey handed it none"
                )
            idle = [name for name in before if name not in applied]
            if not receipt.savings:
                return (
                    f"Price Order priced order {o.number} at full price, {money(receipt.total)}: "
                    f"{', '.join(idle)} {'is' if len(idle) == 1 else 'are'} running but "
                    f"{'does' if len(idle) == 1 else 'do'} not apply to it; the survey left every promotion running"
                )
            took = ", ".join(f"{s.promotion} −{money(s.amount)}" for s in receipt.savings)
            idle_said = f"; {', '.join(idle)} {'does' if len(idle) == 1 else 'do'} not apply to it" if idle else ""
            return (
                f"Price Order priced order {o.number} at {money(receipt.total)} after {took}{idle_said}; "
                "the survey left every promotion running"
            )
        return f"{fired} fired"

    async def fire(session):
        """One firing; what it did in words, or None when nothing is fireable."""
        ctx = session["ctx"]
        before = running(session)
        if not await Runner.step(ctx):
            return None
        session["firings"] += 1
        return f"Firing {session['firings']} — {explain(ctx.graph, ctx.graph.last_fired, before)}"

    async def fire_one(session, show=None):
        """One firing of the example day: when nothing is fireable, the day's next delivery
        comes in first, and ``show`` (the notebook's animation; the tests pass none) is
        awaited with it. Returns 1, or 0 once the day is over."""
        if not fireable(session["graph"]) and session["stage"] < len(SCRIPT):
            came_in = put(session, SCRIPT[session["stage"]])
            session["stage"] += 1
            if show is not None:
                await show(f"In: {came_in}")
        what = await fire(session)
        if what is None:
            return 0
        session["caption"] = what
        snapshot(session, what)
        return 1

    async def settle(session, show):
        """Fire until nothing is fireable, showing each firing."""
        while (what := await fire(session)) is not None:
            await show(what)

    return fire_one, put, running, settle, withdraw


@app.cell
def _(BASKETS, mo):
    basket = mo.ui.dropdown(options=list(BASKETS), value="Hamper", label="Basket")
    # The action buttons count their clicks, and the cell below acts on a count it has not
    # handled yet: it also reruns when the switches are rebuilt after the example day starts
    # promotions, and that rerun must not repeat the action.
    order_button = mo.ui.button(label="＋ Place order", kind="success", value=0, on_click=lambda count: count + 1)
    play_button = mo.ui.button(label="▶ Play the example day", value=0, on_click=lambda count: count + 1)
    handled = {"order": 0, "play": 0}
    reset_button = mo.ui.run_button(label="↺ Reset", kind="warn")
    get_sync, set_sync = mo.state(0)
    return basket, get_sync, handled, order_button, play_button, reset_button, set_sync


@app.cell
def _(RunContext, RustworkxGraph, asyncio, build_priced_shop, reset_button, snapshot):
    _ = reset_button  # open a fresh shop whenever Reset is clicked
    _graph = build_priced_shop()
    session = {
        "graph": _graph,
        "ctx": RunContext(graph=_graph, inbox=asyncio.Queue()),
        "pydigraph": RustworkxGraph.from_executable_graph(_graph),
        "history": [],
        "stage": 0,  # the next delivery of the example day
        "firings": 0,
        "orders": 0,
        "caption": "The shop opens: no order in, no promotion running.",
    }
    snapshot(session, session["caption"])
    return (session,)


@app.cell
def _(PROMOTIONS, get_sync, mo, running, session):
    get_sync()  # rebuilt, showing the net, whenever the net's promotions change other than by a switch
    _on = running(session)
    switches = mo.ui.dictionary(
        {p.name: mo.ui.switch(value=p.name in _on, label=p.name) for p in PROMOTIONS}
    )
    return (switches,)


@app.cell
async def _(
    PROMOTIONS,
    asyncio,
    basket,
    fire_one,
    handled,
    mo,
    money,
    order,
    order_button,
    picture,
    play_button,
    put,
    reset_button,
    running,
    session,
    set_sync,
    settle,
    switches,
    withdraw,
):
    def _view(playing=False):
        _png, _width = picture(session)
        _graph = session["graph"]
        _rows = [
            {
                "order": r.order.number,
                "basket": r.order.basket,
                "subtotal": money(r.order.subtotal),
                "shipping": money(r.order.shipping),
                "promotions applied": ", ".join(f"{s.promotion} −{money(s.amount)}" for s in r.savings) or "—",
                "total": money(r.total),
            }
            for r in _graph.place_named("Priced").tokens
        ]
        # While a firing plays, the switches cannot follow the net (they are rebuilt once the
        # cell is done), so the promotions running are written out instead.
        _promotions = (
            mo.md(f"**Promotions running:** {', '.join(running(session)) or 'none'}")
            if playing
            else mo.hstack([mo.md("**Promotions running:**"), *[switches[p.name] for p in PROMOTIONS]], justify="start", gap=2)
        )
        return mo.vstack([
            _promotions,
            mo.hstack([basket, order_button, play_button, reset_button], justify="start"),
            mo.md(f"**{session['caption']}**"),
            mo.hstack(
                [
                    mo.image(src=_png, width=_width),
                    mo.vstack([
                        mo.md("**Receipts**"),
                        mo.ui.table(_rows, selection=None) if _rows else mo.md("_no order priced yet_"),
                    ]),
                ],
                justify="start",
                align="start",
                gap=2,
            ),
        ])

    async def _show(caption):
        session["caption"] = caption
        mo.output.replace(_view(playing=True))
        await asyncio.sleep(1.0)  # slow the animation for watchability (not the firing)

    # A switch that disagrees with the net was flipped: start or end that promotion.
    _on = running(session)
    for _name, _wanted in switches.value.items():
        if _wanted and _name not in _on:
            session["caption"] = f"{put(session, {'Promotions': [p for p in PROMOTIONS if p.name == _name]})}: nothing fires, Price Order waits for an order"
        elif not _wanted and _name in _on:
            withdraw(session, _name)
            session["caption"] = f"{_name} ends: the next order is priced without it"

    def _clicked(name, button):
        """Whether ``button`` has been clicked since this cell last acted on it."""
        if button.value == handled[name]:
            return False
        handled[name] = button.value
        return True

    _order, _play = _clicked("order", order_button), _clicked("play", play_button)
    if _order:
        await _show(f"In: {put(session, {'Orders': [order(0, basket.value)]})}")
        await settle(session, _show)
    elif _play:
        _played = 0
        while await fire_one(session, show=_show):
            _played += 1
            await _show(session["caption"])
        if not _played:
            session["caption"] = "The example day is over: ↺ Reset to play it again"

    # The promotions changed other than by a switch (the example day started some): rebuild
    # the switches so they show it.
    if dict(switches.value) != {p.name: p.name in running(session) for p in PROMOTIONS}:
        set_sync(lambda n: n + 1)

    _view()
    return


if __name__ == "__main__":
    app.run()
