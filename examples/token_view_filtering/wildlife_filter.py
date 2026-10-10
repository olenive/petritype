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
        # Wildlife — token keys, queries and a filtered view

        Habitats are places; every token is one species in one habitat and carries its own
        category as a **key** (`species`, `class`, `diet`; `examples/token_view_filtering/wildlife.py`, the example
        of the token-keys design). The tabs below are built from `Query.distinct`, one per value
        of `class`, plus **all**; pick one and the net is drawn through that filter: a place
        lists only the tokens it keeps, its header reads `matching / count`, and it says how
        many tokens the filter hides, so a hidden token is never mistaken for an absent one. The
        table under the picture is the same question asked of the net directly,
        `Query.matches(graph, filter)`.

        - **Season: Autumn** lets the birds migrate; `Migrate Forest Wetland` is a **keyed
          edge** that takes the oldest bird of the forest and leaves the fox where it is.
        - **Step** fires one transition (`Spread` splits the zebra first, then migration and
          settling once it is autumn, then `Decline` thins the forest).
        - **↺ Reset** rebuilds the net.

        **Reading the edges.** An arrow moves tokens (consumed from a place, produced into
        one); a thick grey line with a dot at each end is a *mutate edge*, which hands the
        transition the place's front token live and leaves it there. `Spread` and `Decline`
        change a species' population in place through one; `Migrate` and `Settle` move the
        token.
        """
    )
    return


@app.cell
def _():
    import asyncio
    import io

    from rustworkx.visualization import graphviz_draw

    from petritype.core.keys import Filter, Query
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.runtime import RunContext, Runner, SetNetParam
    from wildlife import build_wildlife

    return (
        Filter,
        Query,
        RunContext,
        Runner,
        RustworkxGraph,
        RustworkxToGraphviz,
        SetNetParam,
        asyncio,
        build_wildlife,
        graphviz_draw,
        io,
    )


@app.cell
def _(mo):
    step_button = mo.ui.run_button(label="▶ Step", kind="success")
    reset_button = mo.ui.run_button(label="↺ Reset", kind="warn")
    autumn = mo.ui.switch(label="Season: Autumn", value=False)
    return autumn, reset_button, step_button


@app.cell
def _(Query, RunContext, RustworkxGraph, asyncio, build_wildlife, reset_button):
    _ = reset_button  # rebuild whenever Reset is clicked
    _graph = build_wildlife()
    session = {
        "graph": _graph,
        "pydigraph": RustworkxGraph.from_executable_graph(_graph),
        "ctx": RunContext(graph=_graph, inbox=asyncio.Queue()),
        "last_autumn": None,
    }
    # One tab per class the net holds, in order of first appearance, plus "all".
    class_values = ["all", *Query.distinct(_graph, "class")]
    diet_values = ["all", *Query.distinct(_graph, "diet")]
    return class_values, diet_values, session


@app.cell
def _(class_values, diet_values, mo):
    class_tab = mo.ui.radio(options=class_values, value="all", inline=True, label="class")
    diet_tab = mo.ui.radio(options=diet_values, value="all", inline=True, label="diet")
    return class_tab, diet_tab


@app.cell
def _(Filter, class_tab, diet_tab):
    def chosen_filter():
        """The filter the tabs describe: AND across keys, nothing for ``all``."""
        accepted = {}
        if class_tab.value != "all":
            accepted["class_"] = class_tab.value
        if diet_tab.value != "all":
            accepted["diet"] = diet_tab.value
        return Filter.where(**accepted) if accepted else None

    return (chosen_filter,)


@app.cell
def _(RustworkxToGraphviz, graphviz_draw, io, mo):
    def draw(session, flt):
        """The net through the filter: kept tokens listed, hidden ones counted."""
        graph = session["graph"]

        def summarise(species):
            return f"{species.name} ({species.population})"

        node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(
            graph, summarise, filter=flt,
        )
        image = graphviz_draw(session["pydigraph"], node_attr_fn=node_attr_fn, edge_attr_fn=edge_attr_fn, method="dot")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return mo.image(src=buffer.getvalue(), width=min(image.width, 1300))

    return (draw,)


@app.cell
async def _(
    Query,
    Runner,
    SetNetParam,
    autumn,
    chosen_filter,
    class_tab,
    diet_tab,
    draw,
    mo,
    reset_button,
    session,
    step_button,
):
    _ctx = session["ctx"]
    _graph = _ctx.graph
    # The switch only produces a command; the runner applies it between steps.
    if autumn.value != session["last_autumn"]:
        _ctx.inbox.put_nowait(SetNetParam("season", "Autumn" if autumn.value else "Summer"))
        session["last_autumn"] = autumn.value
    if step_button.value:
        await Runner.step(_ctx)
    else:
        Runner.drain_inbox(_ctx)

    _filter = chosen_filter()
    _matches = Query.matches(_graph, _filter) if _filter is not None else Query.matches(_graph)
    _total = sum(len(place.tokens) for place in _graph.places)
    _banner = (
        f"**{_total - len(_matches)} tokens hidden by the filter** · {len(_matches)} shown"
        if _filter is not None else f"**No filter** · {_total} tokens"
    )
    _rows = [
        {"place": m.place, "id": m.token_id, "species": m.token.name, "class": m.token.class_,
         "diet": m.token.diet, "population": m.token.population}
        for m in _matches
    ]
    mo.vstack([
        mo.hstack([class_tab, diet_tab, autumn, step_button, reset_button], justify="start"),
        mo.md(f"{_banner} · season **{_graph.params['season']}** · last fired: `{_graph.last_fired}`"),
        draw(session, _filter),
        mo.md("**`Query.matches`**"),
        mo.ui.table(_rows, selection=None) if _rows else mo.md("_nothing matches_"),
    ])
    return


if __name__ == "__main__":
    app.run()
