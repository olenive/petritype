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
        # Caching: relationship graph → executable graph

        Two views of the same hypothetical caching code:

        1. A **relationship graph** read straight from `hypothetical_caching.py` by AST
           analysis — which types flow into which functions and back out. This is the map
           you reason over (or hand to an LLM) when *designing* a pipeline.
        2. The **executable graph** that design produces: for each key, look in the cache;
           on a miss, fetch from the database and cache the result. It is fired **live**
           at the bottom.
        """
    )
    return


@app.cell
def _():
    import io
    import os
    import time
    from itertools import chain
    from typing import Iterable, Sequence

    import rustworkx as rx
    from rustworkx.visualization import graphviz_draw

    from petritype.core.ast_extraction import FunctionWithAnnotations
    from petritype.core.data_structures import ClassName, NodeIndex, TypeVariableWithAnnotations
    from petritype.core.descriptions import Description
    from petritype.core.executable_graph_components import (
        ArgumentEdgeToTransition,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
    )
    from petritype.core.parse_modules import (
        ExtractClassCode,
        ExtractFunctions,
        ExtractImportStatements,
        ExtractTypes,
        ParseModule,
    )
    from petritype.core.relationship_graph_components import RelationshipEdges
    from petritype.core.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    # Domain types & functions, imported by BARE module name (sibling on marimo's sys.path).
    from hypothetical_caching import (
        CacheOperations,
        DBKey,
        DBKeyValuePair,
        DBOperations,
    )

    return (
        ArgumentEdgeToTransition,
        CacheOperations,
        ClassName,
        DBKey,
        DBKeyValuePair,
        DBOperations,
        Description,
        ExecutableGraphOperations,
        ExtractClassCode,
        ExtractFunctions,
        ExtractImportStatements,
        ExtractTypes,
        FunctionTransitionNode,
        FunctionWithAnnotations,
        ListPlaceNode,
        ParseModule,
        RelationshipEdges,
        ReturnedEdgeFromTransition,
        RustworkxGraph,
        RustworkxToGraphviz,
        SimpleGraphvizVisualization,
        TypeVariableWithAnnotations,
        chain,
        graphviz_draw,
        io,
        mo,
        os,
        rx,
        time,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""## 1. The relationship graph (read from the source by AST analysis)""")
    return


@app.cell
def _(
    ExtractFunctions,
    ExtractTypes,
    ParseModule,
    RelationshipEdges,
    RustworkxToGraphviz,
    graphviz_draw,
    os,
):
    _path_components = ("examples", "caching", "hypothetical_caching.py")
    parsed_module = ParseModule.from_file(
        path_to_file=os.path.join(*_path_components),
        import_path_components=_path_components,
    )
    functions = ExtractFunctions.from_selected_classes_in_parsed_modules(
        parsed_modules=(parsed_module,),
        selected_classes=("DBOperations", "CacheOperations", "Branch"),
    )
    types = ExtractTypes.from_parsed_modules(parsed_modules=(parsed_module,))

    (
        relationship_graph,
        type_names_to_node_indices,
        _function_names_to_node_indices,
        _type_relationship_edges,
    ) = RustworkxToGraphviz.digraph(
        types=types,
        functions=functions,
        edges_type_to_function=RelationshipEdges.type_to_function(types, functions),
        edges_function_to_type=RelationshipEdges.function_to_type(functions, types),
        edges_type_to_type=RelationshipEdges.type_to_type(types),
    )
    graphviz_draw(
        relationship_graph,
        node_attr_fn=RustworkxToGraphviz.node_attr_fn,
        edge_attr_fn=RustworkxToGraphviz.edge_attr_fn,
        method="sfdp",
    )
    return (
        functions,
        parsed_module,
        relationship_graph,
        type_names_to_node_indices,
        types,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        r"""
        ## 2. A design prompt derived from the graph

        The shortest paths between a start and end type pick out the *relevant* types and
        functions; those, plus a description of the Petritype primitives, form a prompt you
        could hand to an LLM to propose the executable graph below. (Collapsed — expand to
        read.)
        """
    )
    return


@app.cell
def _(
    ClassName,
    Description,
    ExtractClassCode,
    ExtractImportStatements,
    FunctionWithAnnotations,
    RelationshipEdges,
    TypeVariableWithAnnotations,
    chain,
    functions,
    mo,
    parsed_module,
    relationship_graph,
    rx,
    type_names_to_node_indices,
    types,
):
    start_type, end_type = "DBKey", "DBKeyValuePair"
    _start, _end = type_names_to_node_indices[start_type], type_names_to_node_indices[end_type]
    _paths = rx.digraph_all_shortest_paths(relationship_graph, _start, _end)
    _path_indices = list(chain.from_iterable(_paths))
    _neighbours = list(chain.from_iterable(relationship_graph.neighbors(i) for i in _path_indices))
    _relevant = set(_path_indices + _neighbours)

    relevant_functions = tuple(
        relationship_graph[i]
        for i in _relevant
        if isinstance(relationship_graph[i], FunctionWithAnnotations)
    )
    relevant_types = tuple(
        relationship_graph[i]
        for i in _relevant
        if isinstance(relationship_graph[i], TypeVariableWithAnnotations)
    )
    relevant_classes: set[ClassName] = {f.class_name for f in functions}
    relevant_classes_code = [
        ExtractClassCode.from_parsed_module(parsed_module, c) for c in relevant_classes
    ]

    _types_block = "\n\n\n".join(t.code for t in relevant_types)
    _imports = ".\n".join(ExtractImportStatements.from_parsed_module(parsed_module))
    _task = (
        "TASK:\n"
        f"Propose a Petritype Executable Graph starting at {start_type} and ending at {end_type}.\n"
        "Retrieve the value from the cache if it exists; otherwise retrieve it from the\n"
        "database and store it in the cache. The db and cache live outside the graph and are\n"
        "passed to transitions via kwargs, so they are not place nodes.\n\n"
    )
    design_prompt = (
        Description.of_petritype_data_structures()
        + Description.of_petritype_relationship_graph_components()
        + _task
        + f'"""The following type declarations are relevant here."""\n\n{_types_block}\n\n\n'
        + f"IMPORTS:\n{_imports}\n\n\n"
        + "\n\n\n".join(relevant_classes_code)
    )

    mo.accordion({"Generated design prompt (click to expand)": mo.plain_text(design_prompt)})
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        r"""
        ## 3. The executable graph, fired live

        Eight keys are seeded with a warm cache holding three of them (`a_0`, `c_0`, `d_0`).
        Those three resolve straight from the cache (hits, ending in *CachedValueFound*); the
        other five miss, get fetched from the DB, cached, and end in *FinalKeyValuePair*.

        - **Step** / **Repeat Step** — fire one / keep firing until quiescent.
        - **◀ Back / Forward ▶** — scrub through visited states.
        - **↺ Reset** — rebuild a fresh graph.
        """
    )
    return


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
    RustworkxGraph,
):
    def build_graph():
        """Construct a fresh caching graph and its rustworkx view.

        CheckCache returns either a DBKey (miss) or a DBKeyValuePair (hit); the engine
        routes by type, so a miss flows to RetrieveFromDB -> CacheKeyValuePair and a hit
        ends immediately in CachedValueFound. Rebuilt on Reset for clean state.
        """
        cache = {"a_0": "A_0", "c_0": "C_1", "d_0": "D_2"}
        db = {
            "a_0": "A_10",
            "b_0": "B_11",
            "c_0": "C_12",
            "d_0": "D_13",
            "e_0": "E_14",
            "f_0": "F_15",
            "g_0": "G_16",
            "h_0": "H_17",
        }
        initial_keys = ["a_0", "b_0", "c_0", "d_0", "e_0", "f_0", "g_0", "h_0"]
        nodes_and_edges = [
            ListPlaceNode("KeyInput", DBKey, initial_keys),
            ListPlaceNode("KeyForDBRetrieval", DBKey),
            ListPlaceNode("DBValueRetrieved", DBKeyValuePair),
            ListPlaceNode("CachedValueFound", DBKeyValuePair),
            ListPlaceNode("FinalKeyValuePair", DBKeyValuePair),
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
            ArgumentEdgeToTransition("KeyForDBRetrieval", "RetrieveFromDB", "key"),
            ReturnedEdgeFromTransition("RetrieveFromDB", "DBValueRetrieved"),
            ArgumentEdgeToTransition("DBValueRetrieved", "CacheKeyValuePair", "key_value_pair"),
            ReturnedEdgeFromTransition("CacheKeyValuePair", "FinalKeyValuePair"),
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
