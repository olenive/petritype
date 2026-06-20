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
        # Match up tokens — match lengths

        ### Motivations
        Sometimes we may want to operate on all the tokens in a given place node or nodes.
        Here we have a simple example of using a transition function to pair up tokens from
        two different place nodes.

        ### This example
        String tokens are matched against integers corresponding to the number of characters
        in the string. There may not be a unique set of matches, and not every token may
        match up.

        ### Petri net mechanics
        We use `list[...]` type annotations to indicate that, rather than being passed a
        single token from a place node, *all* the tokens should be passed in. The
        `output_distribution_function` specifies how to handle the returned tokens — here
        unmatched tokens are returned to their original places.

        > **TODO:** find a more elegant way to avoid futile cycles, rather than just raising
        > an error. When no further match is possible the transition raises `RuntimeError`,
        > which simply ends the run (auto-stepping stops).

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
    from typing import Optional, Union

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
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        Optional,
        ReturnedEdgeFromTransition,
        RustworkxGraph,
        RustworkxToGraphviz,
        SimpleGraphvizVisualization,
        Union,
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
def _(Optional, Union):
    def match_one_string_to_one_length(
        strings: list[str], lengths: list[int]
    ) -> tuple[list[str], list[int], Optional[tuple[str, int]]]:
        """An impure function that pulls matching pairs out of the input lists."""
        for s in strings:
            for length in lengths:
                if len(s) == length:
                    strings.remove(s)
                    lengths.remove(length)
                    return strings, lengths, (s, length)
        raise RuntimeError("No more matches found.")

    def distribute_result_tokens(
        result: tuple[list[str], list[int], Optional[tuple[str, int]]],
    ) -> dict[str, Union[list[str], list[int], tuple[str, int]]]:
        strings, lengths, matched_pair = result
        print("Distributing result tokens:")
        print(f"  Some Strings: {strings}")
        print(f"  Some Lengths: {lengths}")
        print(f"  Matched Pair: {matched_pair}")
        return {
            "Some Strings": strings,
            "Some Lengths": lengths,
            "Matched Pair": matched_pair,
        }

    return distribute_result_tokens, match_one_string_to_one_length


@app.cell
def _(
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    Optional,
    ReturnedEdgeFromTransition,
    RustworkxGraph,
    distribute_result_tokens,
    match_one_string_to_one_length,
):
    def build_graph():
        """Construct a fresh graph and its rustworkx view."""
        nodes_and_edges = [
            ListPlaceNode("Some Strings", str, ["a", "ab", "abc", "abcd"]),
            ListPlaceNode("Some Lengths", int, [2, 3, 4, 5, 99]),
            ArgumentEdgeToTransition("Some Strings", "Match Lengths", "strings"),
            ArgumentEdgeToTransition("Some Lengths", "Match Lengths", "lengths"),
            FunctionTransitionNode(
                "Match Lengths",
                match_one_string_to_one_length,
                output_distribution_function=distribute_result_tokens,
            ),
            ReturnedEdgeFromTransition("Match Lengths", "Some Strings"),
            ReturnedEdgeFromTransition("Match Lengths", "Some Lengths"),
            ReturnedEdgeFromTransition("Match Lengths", "Matched Pair"),
            ListPlaceNode("Matched Pair", Optional[tuple[str, int]], []),
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
        transitions fired — 0 means nothing was enabled (or the firing raised), which is
        the signal to stop auto-stepping.
        """
        graph = session["graph"]
        pydigraph = session["pydigraph"]
        try:
            _, fired = await ExecutableGraphOperations.execute_graph(
                executable_graph=graph,
                max_transitions=1,
                verbose=False,
            )
        except Exception:
            return 0
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
