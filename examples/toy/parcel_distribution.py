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
        # Parcel distribution centre — perpetual simulation

        A long-running Petri net where parcels continuously arrive, get sorted, staged,
        and dispatched. The process never stops on its own.

        **Flow:** Receive Parcel → Receiving → Sort → Sorting → Stage → Staging Area →
        Dispatch Truck (some parcels randomly left behind for the next truck).

        Demonstrates:
        - **Perpetual execution** — `Receive Parcel` has no inputs, so it generates tokens
          from nothing.
        - **Time-based activation** — guards use wall-clock time (`1s` arrivals, `5s`
          dispatch), so timing is controlled by activation functions, not by sleeping
          inside transitions.
        - **Guard-based selection** — a custom selector that respects activation guards.
        - **Probabilistic behaviour** — each parcel has a 1-in-10 chance of being left
          behind.

        > Because this is live and time-driven (rather than finite and deterministic), it
        > uses a **Run** button with a step cap instead of the precompute + scrub slider
        > used by the other examples.
        """
    )
    return


@app.cell
def _():
    import io
    import random
    import time

    from pydantic import BaseModel
    from rustworkx.visualization import graphviz_draw

    from petritype.core.executable_graph_components import (
        ArgumentEdgeToTransition,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
    )
    from petritype.core.rustworkx_graph import RustworkxGraph
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
        graphviz_draw,
        io,
        random,
        time,
    )


@app.cell
def _(io, mo):
    def half_image(pil_image):
        """Render a PIL image at ~three-quarters its native width so it fits on screen."""
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        return mo.image(src=buffer.getvalue(), width=pil_image.width * 3 // 4)

    return (half_image,)


@app.cell
def _(BaseModel):
    class Parcel(BaseModel):
        """A parcel with a tracking ID."""

        id: int

    return (Parcel,)


@app.cell
def _(
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    Parcel,
    ReturnedEdgeFromTransition,
    RustworkxGraph,
    random,
    time,
):
    def make_simulation():
        """Build a fresh simulation: graph, rustworkx view, and encapsulated state.

        Timing/counter state lives in a local dict closed over by the transition
        functions — this avoids module-level globals (which don't play well with
        marimo's reactive, cell-scoped model).
        """
        state = {"counter": 0, "last_arrival": 0.0, "last_dispatch": 0.0}

        def receive_parcel() -> Parcel:
            """Receive a parcel from a delivery truck (timing handled by the guard)."""
            state["counter"] += 1
            state["last_arrival"] = time.time()
            print(f"📦 Receiving parcel #{state['counter']}...")
            return Parcel(id=state["counter"])

        def can_receive_parcel() -> bool:
            """Guard: only allow receiving a parcel every 1 second."""
            return time.time() - state["last_arrival"] >= 1.0

        def sort_parcel(parcel: Parcel) -> Parcel:
            print(f"  Parcel #{parcel.id} → Sorting...")
            return parcel

        def stage_parcel(parcel: Parcel) -> Parcel:
            print(f"  Parcel #{parcel.id} → Staging Area")
            return parcel

        def dispatch_truck(parcels: list[Parcel]) -> list[Parcel]:
            """Dispatch a truck; each parcel has a 1-in-10 chance of being left behind."""
            state["last_dispatch"] = time.time()
            left_behind, dispatched_ids = [], []
            for parcel in parcels:
                if random.randint(1, 10) == 1:
                    left_behind.append(parcel)
                else:
                    dispatched_ids.append(parcel.id)
            if dispatched_ids:
                print(f"🚚 Truck departed with parcels: {dispatched_ids}")
            if left_behind:
                print(f"⏳ Left behind for next truck: {[p.id for p in left_behind]}")
            return left_behind

        def can_dispatch_truck() -> bool:
            """Guard: only allow truck dispatch every 5 seconds."""
            return time.time() - state["last_dispatch"] >= 5.0

        def guard_based_selector(graph, enabled):
            """Select the first enabled transition whose guard (if any) is satisfied."""
            for transition in enabled:
                if transition.activation_function is None:
                    return transition
                if transition.activation_function():
                    return transition
            return None

        nodes_and_edges = [
            FunctionTransitionNode(
                name="Receive Parcel",
                function=receive_parcel,
                activation_function=can_receive_parcel,
            ),
            ReturnedEdgeFromTransition("Receive Parcel", "Receiving"),
            ListPlaceNode(name="Receiving", type=Parcel),
            ArgumentEdgeToTransition("Receiving", "Sort", "parcel"),
            FunctionTransitionNode(name="Sort", function=sort_parcel),
            ReturnedEdgeFromTransition("Sort", "Sorting"),
            ListPlaceNode(name="Sorting", type=Parcel),
            ArgumentEdgeToTransition("Sorting", "Stage", "parcel"),
            FunctionTransitionNode(name="Stage", function=stage_parcel),
            ReturnedEdgeFromTransition("Stage", "Staging Area"),
            ListPlaceNode(name="Staging Area", type=Parcel),
            ArgumentEdgeToTransition("Staging Area", "Dispatch Truck", "parcels"),
            FunctionTransitionNode(
                name="Dispatch Truck",
                function=dispatch_truck,
                activation_function=can_dispatch_truck,
            ),
            ReturnedEdgeFromTransition("Dispatch Truck", "Staging Area"),
        ]
        graph = ExecutableGraphOperations.construct_graph(nodes_and_edges)
        graph.transition_selector = guard_based_selector
        pydigraph = RustworkxGraph.from_executable_graph(graph)
        return graph, pydigraph

    return (make_simulation,)


@app.cell
def _(mo):
    run = mo.ui.run_button(label="▶ Run simulation", kind="success")
    n_steps = mo.ui.slider(start=5, stop=60, value=30, label="Steps", show_value=True)
    return n_steps, run


@app.cell
async def _(
    ExecutableGraphOperations,
    RustworkxToGraphviz,
    SimpleGraphvizVisualization,
    graphviz_draw,
    half_image,
    make_simulation,
    mo,
    n_steps,
    run,
    time,
):
    _graph, _pydigraph = make_simulation()
    _controls = mo.hstack([run, n_steps], justify="start")

    if not run.value:
        mo.output.replace(
            mo.vstack(
                [
                    _controls,
                    mo.md("Click **▶ Run simulation** to start the perpetual process."),
                    half_image(SimpleGraphvizVisualization.graph(_pydigraph)),
                ]
            )
        )
    else:
        # Live loop: unlike the finite examples we do NOT stop on an idle step — an idle
        # step just means a timing guard isn't ready yet, and the process is perpetual.
        for _i in range(n_steps.value):
            _, _fired = await ExecutableGraphOperations.execute_graph(
                executable_graph=_graph,
                max_transitions=1,
                verbose=False,
            )
            _node_attr_fn, _edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(_graph)
            _diagram = graphviz_draw(
                _pydigraph,
                node_attr_fn=_node_attr_fn,
                edge_attr_fn=_edge_attr_fn,
                method="dot",
            )
            mo.output.replace(
                mo.vstack([_controls, mo.md(f"**Step {_i}** — fired {_fired}"), half_image(_diagram)])
            )
            time.sleep(0.5)
        mo.output.append(
            mo.md(
                f"_Stopped after {n_steps.value} steps — this process would otherwise run "
                "indefinitely._"
            )
        )
    return


if __name__ == "__main__":
    app.run()
