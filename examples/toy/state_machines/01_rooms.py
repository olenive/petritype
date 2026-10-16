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

        A Petri net equivalent to a state machine: a game character moves between rooms. The
        **places are rooms**, the **token is the character**, and each room has **one
        transition, its way out**. Where the character goes is not fixed by the net's shape:
        `Leave Hallway` has an output edge to each neighbouring room, and its **distribution
        function** decides, firing by firing, which one the character takes.

        - The body, `move_from_room_to_room(character, destination)`, hands back the character
          and the room the player chose. The original Jupyter notebook asked with `input()`
          inside the body; here the choice arrives as the net parameter `destination`, which a
          **Go to** button sets (a `SetNetParam` command) just before it steps the net.
        - The distribution function, `direct_to_chosen_room`, turns that pair into
          `{destination: character}`: the room named gets the token, and the transition's other
          output places get nothing.
        - Each move's **guard** is its room's doors: the transition is fireable only when the
          destination is one of that room's exits. Ask for a room with no door from where the
          character stands and no move is fireable, so nothing fires.

        After each move the transition that fired is outlined in magenta, with the one output
        edge its distribution function used. **↺ Reset** puts the character back in the
        Entrance Chamber.

        > This may not be a sensible modelling choice if movement mechanics are identical for
        > every location, but it illustrates the state-machine correspondence.
        """
    )
    return


@app.cell
def _():
    import asyncio
    import io

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
    from petritype.runtime import RunContext, Runner, SetNetParam

    return (
        ArgumentEdgeToTransition,
        BaseModel,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
        RunContext,
        Runner,
        RustworkxGraph,
        RustworkxToGraphviz,
        SetNetParam,
        asyncio,
        graphviz_draw,
        io,
    )


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
):
    # Where the doors lead from each room.
    EXITS = {
        "Entrance Chamber": ["Hallway"],
        "Hallway": ["Entrance Chamber", "Treasure Room"],
        "Treasure Room": ["Hallway"],
    }

    def move_from_room_to_room(character: Physical, destination: str) -> tuple[Physical, str]:
        """The character and the room the player chose (the net parameter ``destination``)."""
        return character, destination

    def direct_to_chosen_room(movement_info: tuple[Physical, str]) -> dict[str, Physical]:
        """Distribution function that routes the character to the chosen destination: a dict
        mapping the destination place name to the character token."""
        character, destination = movement_info
        return {destination: character}

    def doors_of(room: str):
        """A move's guard: fireable only when the destination is one of ``room``'s exits."""
        return lambda graph: graph.params["destination"] in EXITS[room]

    def build_rooms():
        """The map, with the character in the Entrance Chamber and no destination chosen."""
        nodes_and_edges = [
            ListPlaceNode(name="Entrance Chamber", type=Physical, tokens=[Physical(name="character")]),
            ArgumentEdgeToTransition("Entrance Chamber", "Leave Entrance", "character"),
            FunctionTransitionNode(
                name="Leave Entrance",
                function=move_from_room_to_room,
                output_distribution_function=direct_to_chosen_room,
                guard=doors_of("Entrance Chamber"),
            ),
            ReturnedEdgeFromTransition("Leave Entrance", "Hallway"),
            # The hub: one move, two doors, and the distribution function picks the door.
            ListPlaceNode(name="Hallway", type=Physical),
            ArgumentEdgeToTransition("Hallway", "Leave Hallway", "character"),
            FunctionTransitionNode(
                name="Leave Hallway",
                function=move_from_room_to_room,
                output_distribution_function=direct_to_chosen_room,
                guard=doors_of("Hallway"),
            ),
            ReturnedEdgeFromTransition("Leave Hallway", "Entrance Chamber"),
            ReturnedEdgeFromTransition("Leave Hallway", "Treasure Room"),
            ListPlaceNode(name="Treasure Room", type=Physical),
            ArgumentEdgeToTransition("Treasure Room", "Leave Treasure Room", "character"),
            FunctionTransitionNode(
                name="Leave Treasure Room",
                function=move_from_room_to_room,
                output_distribution_function=direct_to_chosen_room,
                guard=doors_of("Treasure Room"),
            ),
            ReturnedEdgeFromTransition("Leave Treasure Room", "Hallway"),
        ]
        return ExecutableGraphOperations.construct_graph(nodes_and_edges, params={"destination": None})

    return EXITS, build_rooms


@app.cell
def _(RustworkxToGraphviz, graphviz_draw, io, mo):
    def draw(session):
        """The map as it stands, the last move and the door it used in magenta."""
        node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(
            session["graph"], lambda token: token.name
        )
        image = graphviz_draw(session["pydigraph"], node_attr_fn=node_attr_fn, edge_attr_fn=edge_attr_fn, method="dot")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return mo.image(src=buffer.getvalue(), width=image.width * 3 // 4)

    return (draw,)


@app.cell
def _(EXITS, mo):
    go_buttons = mo.ui.dictionary({room: mo.ui.run_button(label=f"🚶 Go to {room}") for room in EXITS})
    reset_button = mo.ui.run_button(label="↺ Reset", kind="warn")
    return go_buttons, reset_button


@app.cell
def _(RunContext, RustworkxGraph, asyncio, build_rooms, reset_button):
    _ = reset_button  # back to the start whenever Reset is clicked
    _graph = build_rooms()
    session = {
        "graph": _graph,
        "ctx": RunContext(graph=_graph, inbox=asyncio.Queue()),
        "pydigraph": RustworkxGraph.from_executable_graph(_graph),
        "moves": [],
        "said": "The character is in the Entrance Chamber. Choose where to go.",
    }
    return (session,)


@app.cell
async def _(EXITS, Runner, SetNetParam, draw, go_buttons, mo, reset_button, session):
    def _room():
        return next(place.name for place in session["graph"].places if place.tokens)

    _chosen = next((room for room, clicked in go_buttons.value.items() if clicked), None)
    if _chosen is not None:
        _from = _room()
        # The button only produces a command; the runner applies it, then fires one move.
        session["ctx"].inbox.put_nowait(SetNetParam("destination", _chosen))
        if await Runner.step(session["ctx"]):
            _fired = session["graph"].last_fired
            session["moves"].append(f"{_fired}: {_from} → {_room()}")
            session["said"] = (
                f"**{_fired}** fired, and its distribution function sent the character to the "
                f"**{_room()}**."
            )
        elif _chosen == _from:
            session["said"] = (
                f"The character is already in the {_from}, and no door leads from a room to itself: "
                "no move is fireable, so nothing fired."
            )
        else:
            session["said"] = (
                f"There is no door from the {_from} to the {_chosen}: no move's guard allows it, "
                "so nothing fired."
            )

    _doors = ", ".join(EXITS[_room()])
    _moves = "\n".join(f"{n}. {move}" for n, move in enumerate(session["moves"], 1))
    mo.vstack([
        mo.hstack([*(go_buttons[room] for room in EXITS), reset_button], justify="start"),
        mo.md(f"{session['said']}\n\nThe character is in the **{_room()}**. Doors from here: {_doors}."),
        draw(session),
        mo.md(f"**Moves so far**\n\n{_moves or '_none yet_'}"),
    ])
    return


if __name__ == "__main__":
    app.run()
