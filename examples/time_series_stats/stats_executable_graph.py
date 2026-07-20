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
        # Time-series statistics: relationship graph → executable graph

        Two views of the `hypothetical_time_series.py` code:

        1. A **relationship graph** read from the source by AST analysis — how
           `TimeSeriesGeneratingParameters` flow through the simulation and statistics
           functions to an `ExponentialMovingAverageOfInterval`.
        2. The **executable graph** it motivates: one set of parameters is copied into two
           branches that generate the same series and compute an EMA at two different decay
           rates. It is fired **live** at the bottom.
        """
    )
    return


@app.cell
def _():
    import io
    import os
    import time
    from datetime import datetime, timedelta
    from itertools import chain

    import rustworkx as rx
    from rustworkx.visualization import graphviz_draw

    from petritype.core.ast_extraction import FunctionWithAnnotations
    from petritype.core.data_structures import TypeVariableWithAnnotations
    from petritype.core.descriptions import Description
    from petritype.core.executable_graph_components import (
        ArgumentEdgeToTransition,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
    )
    from petritype.core.parse_modules import (
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
    from hypothetical_time_series import (
        ExponentialMovingAverageOfInterval,
        SeriesStatistics,
        SimulateData,
        TimeSeriesGeneratingParameters,
        TimeSeriesInterval,
    )

    return (
        ArgumentEdgeToTransition,
        Description,
        ExecutableGraphOperations,
        ExponentialMovingAverageOfInterval,
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
        SeriesStatistics,
        SimpleGraphvizVisualization,
        SimulateData,
        TimeSeriesGeneratingParameters,
        TimeSeriesInterval,
        TypeVariableWithAnnotations,
        chain,
        datetime,
        graphviz_draw,
        io,
        mo,
        os,
        rx,
        time,
        timedelta,
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
    _path_components = ("examples", "time_series_stats", "hypothetical_time_series.py")
    parsed_module = ParseModule.from_file(
        path_to_file=os.path.join(*_path_components),
        import_path_components=_path_components,
    )
    functions = ExtractFunctions.from_selected_classes_in_parsed_modules(
        parsed_modules=(parsed_module,),
        selected_classes=("SimulateData", "SeriesStatistics"),
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
    return parsed_module, relationship_graph, type_names_to_node_indices


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        r"""
        ## 2. A design prompt derived from the graph

        The shortest paths between the start and end types pick out the *relevant* types;
        those, plus a description of the Petritype primitives, form a prompt you could hand
        to an LLM to propose the executable graph below. (Collapsed — expand to read.)
        """
    )
    return


@app.cell
def _(
    Description,
    ExtractImportStatements,
    TypeVariableWithAnnotations,
    chain,
    mo,
    parsed_module,
    relationship_graph,
    rx,
    type_names_to_node_indices,
):
    start_type, end_type = "TimeSeriesGeneratingParameters", "ExponentialMovingAverageOfInterval"
    _start, _end = type_names_to_node_indices[start_type], type_names_to_node_indices[end_type]
    _paths = rx.digraph_all_shortest_paths(relationship_graph, _start, _end)
    _path_indices = list(chain.from_iterable(_paths))
    _neighbours = list(chain.from_iterable(relationship_graph.neighbors(i) for i in _path_indices))
    _relevant = set(_path_indices + _neighbours)

    relevant_types = tuple(
        relationship_graph[i]
        for i in _relevant
        if isinstance(relationship_graph[i], TypeVariableWithAnnotations)
    )

    _types_block = "\n\n\n".join(t.code for t in relevant_types)
    _imports = ".\n".join(ExtractImportStatements.from_parsed_module(parsed_module))
    _task = (
        "TASK:\n"
        f"Propose a Petritype Executable Graph starting at {start_type} and ending at {end_type}.\n"
        "Copy the parameters into two branches; each branch generates the series and computes\n"
        "an exponential moving average, one with decay 0.0002 and the other with 0.0001.\n\n"
    )
    design_prompt = (
        Description.of_petritype_data_structures()
        + Description.of_petritype_relationship_graph_components()
        + _task
        + f'"""The following type declarations are relevant here."""\n\n{_types_block}\n\n\n'
        + f"IMPORTS:\n{_imports}\n"
    )

    mo.accordion({"Generated design prompt (click to expand)": mo.plain_text(design_prompt)})
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        r"""
        ## 3. The executable graph, fired live

        *Copy Parameters* fans the single input token out to both branches (token copying is
        enabled), each branch generates the series and computes its EMA, and the two results
        land in *ema_interval_branch1* (decay 0.0002) and *ema_interval_branch2* (decay 0.0001).

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
    ExecutableGraphOperations,
    ExponentialMovingAverageOfInterval,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    RustworkxGraph,
    SeriesStatistics,
    SimulateData,
    TimeSeriesGeneratingParameters,
    TimeSeriesInterval,
    datetime,
    timedelta,
):
    def build_graph():
        """Construct a fresh two-branch EMA graph and its rustworkx view.

        One TimeSeriesGeneratingParameters token is copied into two branches; each generates
        the series and computes an EMA at its own decay rate. Rebuilt on Reset for clean state.
        """
        parameters = TimeSeriesGeneratingParameters(
            start=datetime(2020, 1, 1),
            end=datetime(2020, 1, 2),
            n=100,
            amplitude=10.0,
            shift=15.0,
            noise_std=0.1,
            seed=42,
        )
        nodes_and_edges = [
            ListPlaceNode("initial_parameters", TimeSeriesGeneratingParameters, [parameters]),
            ListPlaceNode("Branch 1 Parameters", TimeSeriesGeneratingParameters),
            ListPlaceNode("Branch 2 Parameters", TimeSeriesGeneratingParameters),
            ListPlaceNode("time_series_interval-1", TimeSeriesInterval),
            ListPlaceNode("time_series_interval-2", TimeSeriesInterval),
            ListPlaceNode("ema_interval_branch1", ExponentialMovingAverageOfInterval),
            ListPlaceNode("ema_interval_branch2", ExponentialMovingAverageOfInterval),
            FunctionTransitionNode("Copy Parameters", function=SimulateData.copy_parameters),
            FunctionTransitionNode(
                "generate_time_series-1",
                function=SimulateData.generate_sine_wave_with_noise_from_parameters,
            ),
            FunctionTransitionNode(
                "generate_time_series-2",
                function=SimulateData.generate_sine_wave_with_noise_from_parameters,
            ),
            FunctionTransitionNode(
                "calculate_ema_branch1",
                function=SeriesStatistics.datetime_interval_ema,
                kwargs={"decay_parameter": 0.0002},
            ),
            FunctionTransitionNode(
                "calculate_ema_branch2",
                function=SeriesStatistics.datetime_interval_ema,
                kwargs={"decay_parameter": 0.0001},
            ),
            ArgumentEdgeToTransition("initial_parameters", "Copy Parameters", "parameters"),
            ReturnedEdgeFromTransition("Copy Parameters", "Branch 1 Parameters"),
            ReturnedEdgeFromTransition("Copy Parameters", "Branch 2 Parameters"),
            ArgumentEdgeToTransition("Branch 1 Parameters", "generate_time_series-1", "parameters"),
            ArgumentEdgeToTransition("Branch 2 Parameters", "generate_time_series-2", "parameters"),
            ReturnedEdgeFromTransition("generate_time_series-1", "time_series_interval-1"),
            ReturnedEdgeFromTransition("generate_time_series-2", "time_series_interval-2"),
            ArgumentEdgeToTransition("time_series_interval-1", "calculate_ema_branch1", "interval"),
            ArgumentEdgeToTransition("time_series_interval-2", "calculate_ema_branch2", "interval"),
            ReturnedEdgeFromTransition("calculate_ema_branch1", "ema_interval_branch1"),
            ReturnedEdgeFromTransition("calculate_ema_branch2", "ema_interval_branch2"),
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
        auto-stepping. Token copying is enabled so Copy Parameters can fan out to both
        same-typed branch places.
        """
        graph = session["graph"]
        pydigraph = session["pydigraph"]
        _, fired = await ExecutableGraphOperations.execute_graph(
            executable_graph=graph,
            stop_after_n_firings=1,
            allow_token_copying=True,
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
