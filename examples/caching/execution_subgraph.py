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
        # An executable graph as a subgraph

        A minimal example of how a whole executable graph can be wrapped in a single
        transition function and nested inside a bigger graph.

        The **inner** graph is the caching net: for each key it either finds a cached
        value or retrieves it from the database and caches it. The **outer** graph has
        one transition — *Subgraph Transition* — whose function instantiates a fresh
        inner graph, drives it to quiescence, and returns the resolved key/value pair.

        The outer net is fired **live**, one transition at a time:

        - **Step** — fire the next enabled transition and redraw the real (mutated) net.
        - **Repeat Step** — keep firing until no transition can be fired successfully.
        - **◀ Back / Forward ▶** — move through the states you have already visited.
        - **↺ Reset** — rebuild a fresh graph.
        """
    )
    return


@app.cell
def _():
    import io
    import time

    from rustworkx.visualization import graphviz_draw

    from petritype.core.executable_graph_components import (
        ArgumentEdgeToTransition,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
    )
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    # Domain types & functions, imported by BARE module name — the sibling file is on
    # marimo's sys.path. (Original used `from examples.caching.hypothetical_caching import *`.)
    from hypothetical_caching import (
        CacheOperations,
        DBKey,
        DBKeyValuePair,
        DBOperations,
    )

    return (
        ArgumentEdgeToTransition,
        CacheOperations,
        DBKey,
        DBKeyValuePair,
        DBOperations,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
        RustworkxGraph,
        RustworkxToGraphviz,
        SimpleGraphvizVisualization,
        graphviz_draw,
        io,
        time,
    )


@app.cell
def _(io, mo):
    def to_frame(caption, pil_image):
        """Snapshot a PIL diagram as (caption, png_bytes, display_width_px)."""
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        return (caption, buffer.getvalue(), pil_image.width * 3 // 4)

    def render(session):
        """Render the state at the session's current history cursor."""
        caption, png, width = session["history"][session["cursor"]]
        total = len(session["history"]) - 1
        return mo.vstack(
            [
                mo.md(f"**{caption}**  ·  state {session['cursor']} / {total}"),
                mo.image(src=png, width=width),
            ]
        )

    return render, to_frame


@app.cell
def _(
    ArgumentEdgeToTransition,
    CacheOperations,
    DBKey,
    DBKeyValuePair,
    DBOperations,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
):
    def build_inner_graph(db, cache, key):
        """The caching net, seeded with a single key.

        CheckCache returns either a DBKey (miss) or a DBKeyValuePair (hit); the engine
        routes by type, so a miss flows to RetrieveFromDB -> CacheKeyValuePair and a hit
        flows straight through. Either way the resolved pair is copied into
        CacheRetrievalOutput, the graph's single result place.
        """
        nodes_and_edges = [
            ListPlaceNode("KeyInput", DBKey, [key]),
            ListPlaceNode("KeyForDBRetrieval", DBKey),
            ListPlaceNode("DBValueRetrieved", DBKeyValuePair),
            ListPlaceNode("CachedValueFound", DBKeyValuePair),
            ListPlaceNode("FinalKeyValuePair", DBKeyValuePair),
            ListPlaceNode("CacheRetrievalOutput", DBKeyValuePair, []),
            FunctionTransitionNode(
                "CheckCache",
                function=CacheOperations.retrieve_key_value_pair,
                kwargs={"cache": cache},
            ),
            FunctionTransitionNode(
                "RetrieveFromDB",
                function=DBOperations.retrieve_key_value_pair,
                kwargs={"db": db},
            ),
            FunctionTransitionNode(
                "CacheKeyValuePair",
                function=CacheOperations.cache_key_value_pair,
                kwargs={"cache": cache, "expected_size": 100},
            ),
            ArgumentEdgeToTransition("KeyInput", "CheckCache", "key"),
            ReturnedEdgeFromTransition("CheckCache", "CachedValueFound"),
            ReturnedEdgeFromTransition("CheckCache", "KeyForDBRetrieval"),
            ReturnedEdgeFromTransition("CheckCache", "CacheRetrievalOutput"),
            ArgumentEdgeToTransition("KeyForDBRetrieval", "RetrieveFromDB", "key"),
            ReturnedEdgeFromTransition("RetrieveFromDB", "DBValueRetrieved"),
            ArgumentEdgeToTransition("DBValueRetrieved", "CacheKeyValuePair", "key_value_pair"),
            ReturnedEdgeFromTransition("CacheKeyValuePair", "FinalKeyValuePair"),
            ReturnedEdgeFromTransition("CacheKeyValuePair", "CacheRetrievalOutput"),
        ]
        return ExecutableGraphOperations.construct_graph(nodes_and_edges)

    return (build_inner_graph,)


@app.cell
def _(ExecutableGraphOperations, build_inner_graph):
    async def cache_lookup_via_subgraph(key, db, cache):
        """Run the caching net as a nested subgraph and return the resolved pair.

        Each call instantiates a fresh inner graph seeded with one key, drives it to
        quiescence, and returns the single token deposited in CacheRetrievalOutput. The
        outer graph sees this whole run as one transition firing.
        """
        inner_graph = build_inner_graph(db, cache, key)
        for _ in range(100):
            _, fired = await ExecutableGraphOperations.execute_graph(
                executable_graph=inner_graph,
                stop_after_n_firings=1,
                allow_token_copying=True,
                verbose=False,
            )
            if not fired:
                break
        for place in inner_graph.places:
            if place.name == "CacheRetrievalOutput":
                return place.tokens[0]
        raise ValueError("Subgraph produced no CacheRetrievalOutput token.")

    return (cache_lookup_via_subgraph,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""## The inner caching net (what one *Subgraph Transition* firing runs)""")
    return


@app.cell
def _(RustworkxGraph, SimpleGraphvizVisualization, build_inner_graph, mo):
    _sample = build_inner_graph({"a_0": "A_10"}, {}, "a_0")
    _sample_pydigraph = RustworkxGraph.from_executable_graph(_sample)
    mo.image(src=SimpleGraphvizVisualization.graph(_sample_pydigraph)._repr_png_())
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""## The outer net (the caching graph nested as a single transition)""")
    return


@app.cell
def _(
    ArgumentEdgeToTransition,
    DBKey,
    DBKeyValuePair,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    RustworkxGraph,
    cache_lookup_via_subgraph,
):
    def build_graph():
        """Construct a fresh outer graph and its rustworkx view.

        The database is fixed; the cache starts warm for one key ("c_0") so the run
        exercises both a cache hit and misses. Rebuilt on Reset for clean state.
        """
        db = {
            "a_0": "A_10",
            "b_0": "B_11",
            "c_0": "C_12",
            "e_0": "E_13",
            "f_0": "F_14",
            "g_0": "G_15",
            "h_0": "H_16",
        }
        cache = {"c_0": "C_1"}
        nodes_and_edges = [
            ListPlaceNode("KeyInput", DBKey, ["a_0", "c_0", "e_0"]),
            ListPlaceNode("FinalOutput", DBKeyValuePair, []),
            FunctionTransitionNode(
                "Subgraph Transition",
                function=cache_lookup_via_subgraph,
                kwargs={"db": db, "cache": cache},
            ),
            ArgumentEdgeToTransition("KeyInput", "Subgraph Transition", "key"),
            ReturnedEdgeFromTransition("Subgraph Transition", "FinalOutput"),
        ]
        graph = ExecutableGraphOperations.construct_graph(nodes_and_edges)
        pydigraph = RustworkxGraph.from_executable_graph(graph)
        return graph, pydigraph

    return (build_graph,)


@app.cell
def _(ExecutableGraphOperations, RustworkxToGraphviz, graphviz_draw, to_frame):
    async def fire_one(session):
        """Fire one transition on the live graph.

        Records a snapshot only if a transition actually fired. Returns the number of
        transitions fired — 0 means nothing was enabled, which is the signal to stop
        auto-stepping.
        """
        graph = session["graph"]
        pydigraph = session["pydigraph"]
        _, fired = await ExecutableGraphOperations.execute_graph(
            executable_graph=graph,
            stop_after_n_firings=1,
            verbose=False,
        )
        if not fired:
            return 0
        index = len(session["history"])
        node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
        image = graphviz_draw(
            pydigraph, node_attr_fn=node_attr_fn, edge_attr_fn=edge_attr_fn, method="dot"
        )
        session["history"].append(to_frame(f"Transition {index} — fired {fired}", image))
        return fired

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
def _(SimpleGraphvizVisualization, build_graph, reset_button, to_frame):
    _ = reset_button  # rebuild a fresh graph whenever Reset is clicked
    _graph, _pydigraph = build_graph()
    session = {
        "graph": _graph,
        "pydigraph": _pydigraph,
        "history": [
            to_frame("Initial state — nothing fired yet", SimpleGraphvizVisualization.graph(_pydigraph))
        ],
        "cursor": 0,
    }
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

    if step_button.value:
        await fire_one(session)
        session["cursor"] = len(session["history"]) - 1
    elif repeat_button.value:
        for _ in range(1000):
            if not await fire_one(session):
                break
            session["cursor"] = len(session["history"]) - 1
            mo.output.replace(mo.vstack([_controls, render(session)]))
            time.sleep(0.8)
        session["cursor"] = len(session["history"]) - 1
    elif back_button.value:
        session["cursor"] = max(0, session["cursor"] - 1)
    elif forward_button.value:
        session["cursor"] = min(len(session["history"]) - 1, session["cursor"] + 1)

    mo.vstack([_controls, render(session)])
    return


if __name__ == "__main__":
    app.run()
