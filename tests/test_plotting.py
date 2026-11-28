"""``petritype.plotting``: the labels the drawings give places and transitions.

A place's label names its type as ``TypeNames`` spells it (``int | str``,
``int | None``, ``Literal['ok']``) and a transition's names its callable, a
partial and a callable instance included. Quotes and backslashes in a name or a
token are escaped, so a label cannot inject Graphviz attributes; Graphviz's own
``dot`` parses the result. The callables drawn are ``test_type_names``'s samples.
"""

from __future__ import annotations

import asyncio
import functools
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, Optional, Union

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from petritype.core.executable_graph_components import (  # noqa: E402
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.core.keys import Filter  # noqa: E402
from tests.test_type_names import CallableProcessor, add_offset, top_level_step  # noqa: E402


# --- rustworkx_to_graphviz: NodeLabel ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("place_type", "expected"),
    [
        (int | str, "int | str"),
        (Optional[int], "int | None"),
        (Union[int, str], "int | str"),
        (list[int], "list[int]"),
        (Any, "Any"),
        (Literal["ok"], "Literal['ok']"),
    ],
)
def test_default_place_renders_complex_types(place_type, expected):
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_to_graphviz import NodeLabel

    place = ListPlaceNode("Inbox", place_type, tokens=[])
    assert NodeLabel.default_place(place).splitlines()[:2] == ["Inbox", f"({expected})"]
    filtered = NodeLabel.default_place(place, filter=Filter())
    assert filtered.splitlines()[:2] == ["Inbox", f"({expected}) 0 / 0"]


def test_default_transition_renders_partials_and_callable_instances():
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_to_graphviz import NodeLabel

    partial_node = FunctionTransitionNode("Add", functools.partial(add_offset, 10))
    callable_node = FunctionTransitionNode("Run", CallableProcessor())
    method_node = FunctionTransitionNode("Method", CallableProcessor().process)

    assert NodeLabel.default_transition(partial_node) == "Add\n(partial(add_offset))"
    assert NodeLabel.default_transition(callable_node) == "Run\n(CallableProcessor)"
    assert NodeLabel.default_transition(method_node) == "Method\n(CallableProcessor.process)"


def test_default_place_and_transition_escape_quotes_and_backslashes():
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_to_graphviz import NodeLabel

    place = ListPlaceNode(
        'Place "A" \\ raw',
        str,
        tokens=['say "hi"', "path\\to\\file", 'inject", fillcolor="red', "trailing\\"],
    )
    label = NodeLabel.default_place(place)
    assert label.splitlines() == [
        'Place \\"A\\" \\\\ raw',
        "(str)",
        'say \\"hi\\"',
        "path\\\\to\\\\file",
        'inject\\", fillcolor=\\"red',
        "trailing\\\\",
    ]

    transition = FunctionTransitionNode('Step "1" \\ "x", shape="diamond', top_level_step)
    t_label = NodeLabel.default_transition(transition)
    assert t_label == 'Step \\"1\\" \\\\ \\"x\\", shape=\\"diamond\n(top_level_step)'


def test_default_place_preserves_plain_multiline_format():
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_to_graphviz import NodeLabel

    place = ListPlaceNode("Queue", int, tokens=[1, 2])
    assert NodeLabel.default_place(place) == "Queue\n(int)\n1\n2"


# --- simple_graphviz: SimpleGraphvizVisualization ----------------------------------------------------------


def test_simple_graphviz_labels_handle_complex_types_callables_and_escaping():
    pytest.importorskip("rustworkx")
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    place = ListPlaceNode('In "box" \\', int | str, tokens=['a"b', "c\\d", 42])
    assert SimpleGraphvizVisualization.place_node_label(place).splitlines() == [
        'In \\"box\\" \\\\',
        "(int | str)",
        'a\\"b',
        "c\\\\d",
        "42",
    ]

    optional_place = ListPlaceNode("Opt", Optional[int], tokens=[None])
    assert SimpleGraphvizVisualization.place_node_label(optional_place).splitlines()[:2] == [
        "Opt",
        "(int | None)",
    ]

    any_place = ListPlaceNode("AnyPlace", Any, tokens=[])
    assert SimpleGraphvizVisualization.place_node_label(any_place).splitlines()[:2] == [
        "AnyPlace",
        "(Any)",
    ]

    partial_transition = FunctionTransitionNode('T "1" \\', functools.partial(add_offset, 5))
    assert SimpleGraphvizVisualization.transition_node_label(partial_transition) == (
        'T \\"1\\" \\\\\n(partial(add_offset))'
    )

    callable_transition = FunctionTransitionNode("T2", CallableProcessor())
    assert SimpleGraphvizVisualization.transition_node_label(callable_transition) == "T2\n(CallableProcessor)"

    # flow_node_attr_fn delegates cleanly without raising
    assert SimpleGraphvizVisualization.flow_node_attr_fn(place)["label"].startswith('In \\"box\\" \\\\\n(int | str)')
    assert SimpleGraphvizVisualization.flow_node_attr_fn(callable_transition)["label"] == "T2\n(CallableProcessor)"


# --- the labels through Graphviz's own parser ---------------------------------------------------------------


@pytest.mark.skipif(shutil.which("dot") is None, reason="needs the Graphviz dot program")
def test_graphviz_parses_tricky_types_callables_and_escaped_labels():
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode(
            'Input "1" \\',
            int | str,
            tokens=['hello "world"', 'inject", shape="diamond', "end\\"],
        ),
        ListPlaceNode("Out", Optional[list[int | str]], tokens=[]),
        FunctionTransitionNode('Work "A" \\, fillcolor="red', functools.partial(add_offset, 1)),
        FunctionTransitionNode("Work B", CallableProcessor()),
        ArgumentEdgeToTransition('Input "1" \\', 'Work "A" \\, fillcolor="red', "value"),
        ReturnedEdgeFromTransition('Work "A" \\, fillcolor="red', "Out"),
        ArgumentEdgeToTransition('Input "1" \\', "Work B", "x"),
        ReturnedEdgeFromTransition("Work B", "Out"),
    ])
    pydigraph = RustworkxGraph.from_executable_graph(graph)
    node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
    source = RustworkxToGraphviz.dot_source(pydigraph, node_attr_fn, edge_attr_fn)

    result = subprocess.run(["dot", "-Tjson"], input=source.encode("utf-8"), capture_output=True, check=True)
    objects = json.loads(result.stdout)["objects"]
    assert len(objects) == 4
    # Confirm no attribute injection occurred: shapes remain oval/box and fillcolors remain deepskyblue/lightgreen.
    assert {obj["shape"] for obj in objects} == {"oval", "box"}
    assert {obj["fillcolor"] for obj in objects} == {"deepskyblue", "lightgreen"}
    labels = [obj["label"] for obj in objects]
    assert any("(int | str)" in lbl and 'hello "world"' in lbl for lbl in labels)
    assert any("(list[int | str] | None)" in lbl for lbl in labels)
    assert any("(partial(add_offset))" in lbl for lbl in labels)
    assert any("(CallableProcessor)" in lbl for lbl in labels)

    # Also verify SimpleGraphvizVisualization.graph renders cleanly end-to-end.
    image = SimpleGraphvizVisualization.graph(pydigraph)
    assert image.width > 0 and image.height > 0


# --- tab, carriage return and NUL (since 2026-11-28) --------------------------------------------------------


ODD = {"tab": "\t", "carriage return": "\r", "carriage return and line feed": "\r\n", "NUL": "\0"}
DRAWN = {"tab": "    ", "carriage return": "\n", "carriage return and line feed": "\n", "NUL": ""}


def odd_net(odd: str):
    """Every kind of name a drawing writes holding ``odd``: a place, its token and
    its group, a transition, a subnet place, its port and its template, and a
    keyed edge's filter and ``reaches``."""
    from petritype.core.events import Port
    from petritype.core.executable_graph_components import SubnetPlaceNode, TakeWhereEdge

    inner = ExecutableGraphOperations.construct_graph([
        ListPlaceNode(f"In{odd}", str, port=True), ListPlaceNode("Out", str, port=True),
    ], template=f"Odd{odd}Template")
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode(f"A{odd}B", int | str, [f"to{odd}ken"], group=f"G{odd}", keys={f"k{odd}": lambda token: token}),
        SubnetPlaceNode(f"S{odd}", inner),
        FunctionTransitionNode(f"T{odd}", top_level_step),
        TakeWhereEdge(f"A{odd}B", f"T{odd}", "x", where=Filter.where(**{f"k{odd}": f"v{odd}"})),
        ReturnedEdgeFromTransition(f"T{odd}", Port(f"S{odd}", f"In{odd}")),
    ], open=True)


@pytest.mark.parametrize("odd", ODD.values(), ids=ODD)
def test_the_labels_make_a_tab_a_carriage_return_and_a_nul_drawable(odd):
    """A tab becomes four spaces, a carriage return a line break (with its line
    feed, one), and a NUL is left out, in every label: Graphviz draws the first two
    in ways of its own, and ``dot`` failed on the third. The names in the net are
    untouched."""
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import NodeLabel, RustworkxToGraphviz

    drawn = DRAWN[next(name for name, value in ODD.items() if value == odd)]
    assert NodeLabel.drawable(f"a{odd}b") == f"a{drawn}b"
    assert NodeLabel.escape_quoted(f'"{odd}"') == f'\\"{drawn}\\"'
    assert NodeLabel.record_text(f"{{{odd}}}") == "\\{" + drawn.replace("\n", "\\n") + "\\}"
    assert NodeLabel.quoted_text(f"x{odd}") == "x" + drawn.replace("\n", "\\n")
    graph = odd_net(odd)
    pydigraph = RustworkxGraph.from_executable_graph(graph)
    node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
    source = RustworkxToGraphviz.dot_source(pydigraph, node_attr_fn, edge_attr_fn)
    assert not {"\t", "\r", "\0"} & set(source)
    assert graph.places[0].name == f"A{odd}B" and graph.places[0].tokens == [f"to{odd}ken"]
    assert graph.groups == (f"G{odd}",)


@pytest.mark.skipif(shutil.which("dot") is None, reason="needs the Graphviz dot program")
@pytest.mark.parametrize("odd", ODD.values(), ids=ODD)
def test_graphviz_draws_a_net_whose_names_hold_them(odd):
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    graph = odd_net(odd)
    pydigraph = RustworkxGraph.from_executable_graph(graph)
    node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
    source = RustworkxToGraphviz.dot_source(pydigraph, node_attr_fn, edge_attr_fn)
    result = subprocess.run(["dot", "-Tjson"], input=source.encode("utf-8"), capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    labels = [obj.get("label", "") for obj in json.loads(result.stdout)["objects"]]
    drawn = DRAWN[next(name for name, value in ODD.items() if value == odd)]
    assert f"A{drawn}B\n(int | str)\nto{drawn}ken" in labels, "the place, a quoted label with its line breaks"
    assert "G" + drawn.replace("\n", "\\n") in labels, "the group's frame, a title with Graphviz's line break"
    image = SimpleGraphvizVisualization.graph(pydigraph)
    assert image.width > 0 and image.height > 0


# --- an open net's places, and the groups' frames (since 2026-11-28) -----------------------------------------


def upper(x: str) -> str:
    return x.upper()


def depot(open: bool, inner_open: bool = False):
    """``Inbox``, a port, and ``Sorted``, in the group ``Sorting``, ``Sort`` between
    them; a subnet place ``Locker`` and a place ``Carts`` whose token is a net, each
    net ``inner_open`` or not; the whole declared ``open`` or not."""
    from petritype.core.executable_graph_components import ExecutableGraph, SubnetPlaceNode

    def inner():
        return ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Slot", str, port=True), ListPlaceNode("Kept", str),
        ], open=inner_open)

    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Inbox", str, ["parcel"], port=True),
        ListPlaceNode("Sorted", str, group="Sorting"),
        SubnetPlaceNode("Locker", inner()),
        ListPlaceNode("Carts", ExecutableGraph, [inner()], nested=True),
        FunctionTransitionNode("Sort", upper, group="Sorting"),
        ArgumentEdgeToTransition("Inbox", "Sort", "x"),
        ReturnedEdgeFromTransition("Sort", "Sorted"),
    ], open=open)


DASHED = '"filled,dashed"'
DOTTED = '"filled,dotted"'


@pytest.mark.parametrize("open", [True, False], ids=["open", "closed"])
def test_a_port_is_dotted_and_an_open_nets_other_places_are_dashed(open):
    """A port has ``dotted`` added to its style, one ring and no ``peripheries``,
    in a closed net and an open one alike, and never ``dashed`` beside it; every
    other ordinary place of a net declared open has ``dashed`` added. A subnet
    place's box, a transition and every other place of a closed net are drawn as
    before, and every place stays filled."""
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz

    graph = depot(open)
    node_attrs, _ = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
    place = DASHED if open else "filled"
    inbox, sorted_ = node_attrs(graph.place_named("Inbox")), node_attrs(graph.place_named("Sorted"))
    assert (inbox["style"], inbox["fillcolor"]) == (DOTTED, "deepskyblue") and "peripheries" not in inbox
    assert "dashed" not in inbox["style"], "the port's mark wins"
    assert (sorted_["style"], sorted_["fillcolor"]) == (place, "deepskyblue") and "peripheries" not in sorted_
    assert node_attrs(graph.place_named("Carts"))["style"] == place
    assert node_attrs(graph.place_named("Locker"))["style"] == "filled", "a subnet place's box is unchanged"
    assert node_attrs(graph.transition_named("Sort"))["style"] == "filled"


def test_the_outline_of_the_last_firing_keeps_the_dots_and_the_dashes():
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz

    graph = depot(open=True)
    asyncio.run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
    node_attrs, _ = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
    inbox, sorted_ = node_attrs(graph.place_named("Inbox")), node_attrs(graph.place_named("Sorted"))
    assert (inbox["style"], inbox["color"], inbox["penwidth"]) == (DOTTED, "magenta", "2")
    assert (sorted_["style"], sorted_["color"], sorted_["penwidth"]) == (DASHED, "magenta", "2")


@pytest.mark.parametrize("inner_open", [True, False], ids=["open", "closed"])
def test_a_subnet_and_a_net_a_token_carries_are_drawn_by_their_own_openness(inner_open):
    """Whichever net is drawn: a subnet or a token's net is dashed when it is open
    itself, whether or not the net around it is, and its port is dotted either way."""
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    graph = depot(open=not inner_open, inner_open=inner_open)
    place = DASHED if inner_open else "filled"
    for net in (graph.subnet("Locker"), graph.place("Carts").tokens[0]):
        node_attrs, _ = RustworkxToGraphviz.activation_coloured_attr_functions(net)
        assert [node_attrs(p)["style"] for p in net.places] == [DOTTED, place], "Slot, the port, and Kept"
        assert RustworkxGraph.is_open(RustworkxGraph.from_executable_graph(net)) is inner_open
        slot = SimpleGraphvizVisualization.flow_node_attr_fn(net.place_named("Slot"), open=inner_open)
        assert slot["style"] == DOTTED and "peripheries" not in slot
        assert SimpleGraphvizVisualization.flow_node_attr_fn(net.place_named("Kept"), open=inner_open)["style"] == place


def test_the_simple_drawing_reads_the_openness_off_the_rustworkx_graph():
    pytest.importorskip("rustworkx")
    from rustworkx import PyDiGraph

    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    assert RustworkxGraph.is_open(RustworkxGraph.from_executable_graph(depot(open=True)))
    assert not RustworkxGraph.is_open(RustworkxGraph.from_executable_graph(depot(open=False)))
    assert not RustworkxGraph.is_open(PyDiGraph()), "a graph built otherwise is drawn closed"
    sorted_, inbox = depot(open=True).place_named("Sorted"), depot(open=True).place_named("Inbox")
    assert SimpleGraphvizVisualization.flow_node_attr_fn(sorted_)["style"] == "filled", "closed unless told"
    assert SimpleGraphvizVisualization.flow_node_attr_fn(sorted_, open=True)["style"] == DASHED
    assert SimpleGraphvizVisualization.flow_node_attr_fn(inbox)["style"] == DOTTED
    assert SimpleGraphvizVisualization.flow_node_attr_fn(inbox, open=True)["style"] == DOTTED


def test_a_style_of_your_own_joins_the_dots_and_the_dashes():
    """``NodeLabel.add_style`` adds a keyword to a style, quoted once it holds two,
    since rustworkx writes the value as given; a keyword already there is kept once."""
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_to_graphviz import NodeLabel

    assert NodeLabel.add_style({}, "dashed") == {"style": "dashed"}
    assert NodeLabel.add_style({"style": "filled"}, "dashed") == {"style": DASHED}
    assert NodeLabel.add_style({"style": "filled"}, "dotted") == {"style": DOTTED}
    assert NodeLabel.add_style({"style": DASHED}, "bold") == {"style": '"filled,dashed,bold"'}
    assert NodeLabel.add_style({"style": DOTTED}, "bold") == {"style": '"filled,dotted,bold"'}
    assert NodeLabel.add_style({"style": DASHED}, "dashed") == {"style": DASHED}
    assert NodeLabel.add_style({"style": '"filled, bold"'}, "dashed") == {"style": '"filled,bold,dashed"'}


def svg_node(svg: str, first_line: str) -> str:
    """The SVG group Graphviz draws for the node whose label's first line is
    ``first_line``: its outline, one ``<ellipse>`` per ring, and its text."""
    groups = [group.split("</g>")[0] for group in svg.split('<g id="node')[1:]]
    found = [group for group in groups if f">{first_line}</text>" in group]
    assert len(found) == 1, f"one node drawn for {first_line}"
    return found[0]


@pytest.mark.skipif(shutil.which("dot") is None, reason="needs the Graphviz dot program")
def test_graphviz_reads_dotted_ports_dashed_places_and_dotted_frames():
    """Read back from Graphviz itself: an open net's port is filled and dotted,
    Graphviz's 1,5 pattern, with one ring; its other places are filled and dashed,
    the 5,2 pattern; and a group's frame is dotted, the shorter of Graphviz's two
    patterns, so it is not taken for an open net's place."""
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import NodeLabel, RustworkxToGraphviz
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    graph = depot(open=True)
    pydigraph = RustworkxGraph.from_executable_graph(graph)
    node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)

    def bold(node):
        return NodeLabel.add_style(node_attr_fn(node), "bold")

    for attrs, bolded in ((node_attr_fn, ""), (bold, ",bold")):
        source = RustworkxToGraphviz.dot_source(pydigraph, attrs, edge_attr_fn)
        objects = json.loads(subprocess.run(["dot", "-Tjson"], input=source.encode(), capture_output=True, check=True).stdout)["objects"]
        styles = {obj["label"].split("\n")[0].split("|")[0].strip("{"): obj["style"] for obj in objects if "style" in obj}
        assert styles["Sorting"] == "dotted"
        assert styles["Inbox"] == "filled,dotted" + bolded
        assert styles["Sorted"] == styles["Carts"] == "filled,dashed" + bolded
        assert styles["Locker"] == "filled" + bolded
    svg = subprocess.run(["dot", "-Tsvg"], input=source.encode(), capture_output=True, check=True).stdout.decode()
    inbox = svg_node(svg, "Inbox")
    assert inbox.count("<ellipse") == 1 and 'stroke-dasharray="1,5"' in inbox, "the port: one dotted ring"
    for place in ("Sorted", "Carts"):
        drawn = svg_node(svg, place)
        assert drawn.count("<ellipse") == 1 and 'stroke-dasharray="5,2"' in drawn
    assert svg.count('stroke-dasharray="5,2"') == 2, "Sorted and Carts"
    assert svg.count('stroke-dasharray="1,5"') == 2, "Inbox and the frame"
    assert SimpleGraphvizVisualization.graph(pydigraph).width > 0


@pytest.mark.skipif(shutil.which("dot") is None, reason="needs the Graphviz dot program")
def test_graphviz_draws_a_closed_nets_port_with_one_dotted_ring():
    """Read back from Graphviz itself: a closed net's port is one ring, dotted, where
    it had a second ring; its other places are one plain ring, dashed nowhere."""
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz

    graph = depot(open=False)
    node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
    source = RustworkxToGraphviz.dot_source(RustworkxGraph.from_executable_graph(graph), node_attr_fn, edge_attr_fn)
    assert "peripheries" not in source
    svg = subprocess.run(["dot", "-Tsvg"], input=source.encode(), capture_output=True, check=True).stdout.decode()
    inbox = svg_node(svg, "Inbox")
    assert inbox.count("<ellipse") == 1 and 'stroke-dasharray="1,5"' in inbox, "the port: one dotted ring"
    for place in ("Sorted", "Carts"):
        drawn = svg_node(svg, place)
        assert drawn.count("<ellipse") == 1 and "stroke-dasharray" not in drawn
    assert 'stroke-dasharray="5,2"' not in svg
