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
        # A hypothetical web-scraping pipeline

        A small, self-contained data-processing pipeline used in the documentation. It
        represents a hypothetical web scrape over dummy data, and shows how a token can
        **branch by type**: a transition returns one of several types, and the engine
        routes each result to the place whose type it matches.

        **Flow:** Input Parameters → *Scrape* → Response → *Classify* →
        {Successes, Uncertain}; Uncertain → *Special Cases* → {Successes, Failures}.

        The net is fired **live**, one transition at a time:

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
    from typing import Union

    from pydantic import BaseModel
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

    return (
        ArgumentEdgeToTransition,
        BaseModel,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
        RustworkxGraph,
        RustworkxToGraphviz,
        SimpleGraphvizVisualization,
        Union,
        graphviz_draw,
        io,
        time,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""## Domain types and the (simulated) scraping functions""")
    return


@app.cell
def _(BaseModel, Union):
    class ScrapeParameters(BaseModel):
        url: str

        def __str__(self):
            return f"[{self.url}]"

    class ScrapedData(BaseModel):
        parameters: ScrapeParameters
        data: str

        def __str__(self):
            return f"[{self.parameters.url}]"

    class SuccessfulScrape(BaseModel):
        parameters: ScrapeParameters
        result: ScrapedData

        def __str__(self):
            return f"[{self.parameters.url}]"

    class UncertainScrapeResult(BaseModel):
        parameters: ScrapeParameters
        result: ScrapedData

        def __str__(self):
            return f"[{self.parameters.url}]"

    class FailedScrape(BaseModel):
        parameters: ScrapeParameters
        result: ScrapedData
        error: str

        def __str__(self):
            return f"[{self.parameters.url}]"

    def simulated_scrape_attempt(parameters: ScrapeParameters) -> ScrapedData:
        if parameters.url.startswith("wrong/page"):
            return ScrapedData(parameters=parameters, data="Encountered a wrong error")
        elif parameters.url.startswith("difficult/page"):
            return ScrapedData(parameters=parameters, data="Maybe difficult valid data")
        else:
            return ScrapedData(parameters=parameters, data="Valid data")

    def classify_scrape_data(
        data: ScrapedData,
    ) -> Union[SuccessfulScrape, UncertainScrapeResult]:
        if ("difficult" in data.data.lower()) or ("wrong" in data.data.lower()):
            return UncertainScrapeResult(parameters=data.parameters, result=data)
        return SuccessfulScrape(parameters=data.parameters, result=data)

    def handle_special_cases(
        data: UncertainScrapeResult,
    ) -> Union[SuccessfulScrape, FailedScrape]:
        if "maybe" in data.result.data.lower():
            return SuccessfulScrape(parameters=data.parameters, result=data.result)
        return FailedScrape(
            parameters=data.parameters, result=data.result, error="Definite error encountered"
        )

    return (
        FailedScrape,
        ScrapeParameters,
        ScrapedData,
        SuccessfulScrape,
        UncertainScrapeResult,
        classify_scrape_data,
        handle_special_cases,
        simulated_scrape_attempt,
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
    ExecutableGraphOperations,
    FailedScrape,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    RustworkxGraph,
    ScrapeParameters,
    ScrapedData,
    SuccessfulScrape,
    UncertainScrapeResult,
    classify_scrape_data,
    handle_special_cases,
    simulated_scrape_attempt,
):
    def build_graph():
        """Construct a fresh scraping graph and its rustworkx view.

        Four URLs are seeded: two clean ("valid/..."), one that scrapes an error
        ("wrong/..."), and one that is ambiguous ("difficult/..."). Classify routes the
        clean ones straight to Successes and the other two to Uncertain; Special Cases
        then rescues the ambiguous one and fails the erroneous one.
        """
        initial_parameters = [
            ScrapeParameters(url="valid/page_01"),
            ScrapeParameters(url="wrong/page_01"),
            ScrapeParameters(url="difficult/page_01"),
            ScrapeParameters(url="valid/page_02"),
        ]
        nodes_and_edges = [
            ListPlaceNode("Input Parameters", ScrapeParameters, initial_parameters),
            ListPlaceNode("Response", ScrapedData),
            ListPlaceNode("Uncertain", UncertainScrapeResult),
            ListPlaceNode("Failures", FailedScrape),
            ListPlaceNode("Successes", SuccessfulScrape),
            FunctionTransitionNode("Scrape", function=simulated_scrape_attempt),
            FunctionTransitionNode("Classify", function=classify_scrape_data),
            FunctionTransitionNode("Special Cases", function=handle_special_cases),
            ArgumentEdgeToTransition("Input Parameters", "Scrape", "parameters"),
            ReturnedEdgeFromTransition("Scrape", "Response"),
            ArgumentEdgeToTransition("Response", "Classify", "data"),
            ReturnedEdgeFromTransition("Classify", "Uncertain"),
            ReturnedEdgeFromTransition("Classify", "Successes"),
            ArgumentEdgeToTransition("Uncertain", "Special Cases", "data"),
            ReturnedEdgeFromTransition("Special Cases", "Successes"),
            ReturnedEdgeFromTransition("Special Cases", "Failures"),
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
