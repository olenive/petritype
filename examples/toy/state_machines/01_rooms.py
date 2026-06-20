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
        # State machines — moving between rooms

        A Petri net equivalent to a state machine: a game character moves between rooms in
        response to user input. The **places are rooms**, the **token is the character**,
        and the **transitions are the possible moves**.

        The original notebook used Python's blocking `input()` to choose a destination. In
        marimo that becomes genuinely interactive: click a **move** button for one of the
        current room's exits and the character token relocates; **Reset** returns to the
        start.

        > This may not be a sensible modelling choice if movement mechanics are identical
        > for every location — but it illustrates the state-machine correspondence.
        """
    )
    return


@app.cell
def _():
    import io

    from pydantic import BaseModel

    from petritype.core.executable_graph_components import (
        ArgumentEdgeToTransition,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
    )
    from petritype.core.rustworkx_graph import RustworkxGraph
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    return (
        ArgumentEdgeToTransition,
        BaseModel,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
        RustworkxGraph,
        SimpleGraphvizVisualization,
        io,
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
    class Physical(BaseModel):
        name: str

    return (Physical,)


@app.cell
def _(
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    Physical,
    ReturnedEdgeFromTransition,
    RustworkxGraph,
):
    # Where you can go from each room.
    EXITS = {
        "Entrance Chamber": ["Hallway"],
        "Hallway": ["Entrance Chamber", "Treasure Room"],
        "Treasure Room": ["Hallway"],
    }

    def build_map_graph(current_room: str):
        """Build the rustworkx view of the room map, with the character token placed in
        `current_room`. The transition functions are placeholders — moves are driven by the
        UI rather than by executing the net, so they are never called."""

        def leave(character: Physical) -> Physical:
            return character

        def tokens_for(room: str):
            return [Physical(name="character")] if room == current_room else []

        nodes_and_edges = [
            ListPlaceNode(name="Entrance Chamber", type=Physical, tokens=tokens_for("Entrance Chamber")),
            ArgumentEdgeToTransition("Entrance Chamber", "Leave Entrance", "character"),
            FunctionTransitionNode(name="Leave Entrance", function=leave),
            ReturnedEdgeFromTransition("Leave Entrance", "Hallway"),
            ListPlaceNode(name="Hallway", type=Physical, tokens=tokens_for("Hallway")),
            ArgumentEdgeToTransition("Hallway", "Leave Hallway", "character"),
            FunctionTransitionNode(name="Leave Hallway", function=leave),
            ReturnedEdgeFromTransition("Leave Hallway", "Entrance Chamber"),
            ReturnedEdgeFromTransition("Leave Hallway", "Treasure Room"),
            ListPlaceNode(name="Treasure Room", type=Physical, tokens=tokens_for("Treasure Room")),
            ArgumentEdgeToTransition("Treasure Room", "Leave Treasure Room", "character"),
            FunctionTransitionNode(name="Leave Treasure Room", function=leave),
            ReturnedEdgeFromTransition("Leave Treasure Room", "Hallway"),
        ]
        graph = ExecutableGraphOperations.construct_graph(nodes_and_edges)
        return RustworkxGraph.from_executable_graph(graph)

    return EXITS, build_map_graph


@app.cell
def _(mo):
    get_room, set_room = mo.state("Entrance Chamber")
    return get_room, set_room


@app.cell
def _(EXITS, SimpleGraphvizVisualization, build_map_graph, get_room, half_image, mo, set_room):
    _room = get_room()

    # One "move" button per available exit; Reset returns to the start.
    _move_buttons = [
        mo.ui.button(
            label=f"🚶 Go to {destination}",
            on_change=(lambda d: lambda _v: set_room(d))(destination),
        )
        for destination in EXITS[_room]
    ]
    _reset_button = mo.ui.button(
        label="↺ Reset", kind="warn", on_change=lambda _v: set_room("Entrance Chamber")
    )
    _controls = mo.hstack(_move_buttons + [_reset_button], justify="start")

    # Buttons render directly above the map so the controls stay next to the figure.
    mo.vstack(
        [
            _controls,
            mo.md(f"### 🧙 The character is in **{_room}**"),
            half_image(SimpleGraphvizVisualization.graph(build_map_graph(_room))),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
