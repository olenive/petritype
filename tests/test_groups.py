"""Groups (PROTOCOL.md §5.1, ``petritype-rs/dev-docs/group-spec.md``): a named set of
places and transitions of one net, set in the net's definition with ``group=`` on the
node, that only drawings read.

These tests are, in turn: declaring and reading; the order of the groups; a subnet's
own groups and a token's net's; what the build accepts; copies, templates and saving;
a net built twice, with and without groups, stepping the same; the Graphviz frames;
and the example. The protocol runner's topology fields are
``tests/test_runner_groups.py``.
"""

import asyncio
import dataclasses
import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from petritype.core.events import Fired, Nested, Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    SubnetPlaceNode,
)
from petritype.core.nested import NestedNets
from petritype.core.serialisation import NetJson, Templates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TIE_BREAKS = ("first_declared", "last_declared", "round_robin")


def run(coroutine):
    return asyncio.run(coroutine)


def same(x: int) -> int:
    return x


def grind(grain: int) -> int:
    return grain * 10


def named(grouped: bool):
    """The group a node names when the net is built with groups, else none."""
    return (lambda name: name) if grouped else (lambda name: None)


def mill(grouped: bool = True, tie_break: str = "first_declared") -> ExecutableGraph:
    """A subnet: ``Hopper`` (a port) -Grind-> ``Flour`` (a port), each grain ground
    tenfold; ``Hopper`` and ``Grind`` in the subnet's own group ``Grinding``."""
    g = named(grouped)
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Hopper", int, port=True, group=g("Grinding")),
        ListPlaceNode("Flour", int, port=True),
        FunctionTransitionNode("Grind", grind, group=g("Grinding")),
        ArgumentEdgeToTransition("Hopper", "Grind", "grain"),
        ReturnedEdgeFromTransition("Grind", "Flour"),
    ], tie_break=tie_break, record_events=True, template="Test Groups Mill")


def farm(grouped: bool = True, tie_break: str = "first_declared") -> ExecutableGraph:
    """A farm whose ``Reap`` and ``Glean`` compete for the field, so the tie-break
    decides, with a mill as a subnet place in the group ``Milling``. ``Selling``
    is named first in the list, by a transition, and comes last in the order: the
    places are read before the transitions."""
    g = named(grouped)
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Field", int, [1, 2, 3, 4], group=g("Harvest")),
        ListPlaceNode("Barn", int, group=g("Harvest")),
        SubnetPlaceNode("Mill", mill(grouped, tie_break), group=g("Milling")),
        ListPlaceNode("Market", int),
        FunctionTransitionNode("Sell", same, group=g("Selling")),
        ArgumentEdgeToTransition(Port("Mill", "Flour"), "Sell", "x"),
        ReturnedEdgeFromTransition("Sell", "Market"),
        FunctionTransitionNode("Reap", same, group=g("Harvest")),
        ArgumentEdgeToTransition("Field", "Reap", "x"),
        ReturnedEdgeFromTransition("Reap", "Barn"),
        FunctionTransitionNode("Glean", same),
        ArgumentEdgeToTransition("Field", "Glean", "x"),
        ReturnedEdgeFromTransition("Glean", "Market"),
        FunctionTransitionNode("Cart", same, group=g("Milling")),
        ArgumentEdgeToTransition("Barn", "Cart", "x"),
        ReturnedEdgeFromTransition("Cart", Port("Mill", "Hopper")),
    ], tie_break=tie_break, record_events=True)


def members(graph: ExecutableGraph, group: str) -> list[str]:
    return [node.name for node in (*graph.places, *graph.transitions) if node.group == group]


# --- declaring and reading -----------------------------------------------------------------------


def test_a_node_names_its_group_and_the_net_lists_its_groups():
    f = farm()
    assert f.groups == ("Harvest", "Milling", "Selling")
    assert members(f, "Harvest") == ["Field", "Barn", "Reap"]
    assert members(f, "Milling") == ["Mill", "Cart"]
    assert members(f, "Selling") == ["Sell"]
    assert f.place_named("Market").group is None, "a place in no group"
    assert f.transition_named("Glean").group is None, "a transition in no group"
    assert [p.name for p in f.places] == ["Field", "Barn", "Mill", "Market"], "declaration order untouched"
    assert [t.name for t in f.transitions] == ["Sell", "Reap", "Glean", "Cart"]


def test_a_group_comes_in_the_order_of_its_first_mention_places_first():
    """``Selling`` is mentioned first in the list given to ``construct_graph``, by a
    transition, and comes after the groups of the places, which are read first."""
    assert farm().groups[-1] == "Selling"
    lone = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("A", int, group="Second"),
        ListPlaceNode("B", int, group="First"),
        ListPlaceNode("C", int, group="Second"),
    ])
    assert lone.groups == ("Second", "First")


def test_a_net_with_no_groups_lists_none():
    assert farm(grouped=False).groups == ()
    assert farm(grouped=False).subnet("Mill").groups == ()


def test_a_subnet_and_a_tokens_net_have_groups_of_their_own():
    f = farm()
    assert "Grinding" not in f.groups, "the subnet's group is not the outer net's"
    inside = f.subnet("Mill")
    assert inside.groups == ("Grinding",)
    assert members(inside, "Grinding") == ["Hopper", "Grind"]
    assert f.place_named(Port("Mill", "Hopper")).group == "Grinding", "a port's group is its subnet's"
    assert f.place_named("Mill").group == "Milling", "the subnet place is in the outer net's group"
    carrier = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Crates", ExecutableGraph, [mill()], nested=True, group="Storage"),
    ])
    assert carrier.groups == ("Storage",)
    assert carrier.place_named("Crates").tokens[0].groups == ("Grinding",)


# --- what the build accepts --------------------------------------------------------------------


def test_a_group_name_may_hold_any_character_and_a_node_keeps_its_name():
    """No character of a name has a meaning: a name that looks like a path or a
    prefix, or that is one space, is a name like any other. A group may share a
    name with a node. The empty string is no name (since 2026-11-28,
    ``tests/test_empty_names.py``)."""
    odd = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Walk", int, group="Walk"),
        ListPlaceNode("B", int, group="a/b: c|d"),
        ListPlaceNode("C", int, group=" "),
        FunctionTransitionNode("T", same, group="Walk"),
        ArgumentEdgeToTransition("Walk", "T", "x"),
        ReturnedEdgeFromTransition("T", "B"),
    ])
    assert odd.groups == ("Walk", "a/b: c|d", " ")
    assert odd.place_named("Walk").name == "Walk"
    with pytest.raises(ValidationError, match="empty string"):
        ListPlaceNode("C", int, group="")


def test_the_group_is_a_string_or_none():
    """The spec's build errors cannot arise here: a node names one group, so it is
    never in two; a group is its name, so two of one name are one group; there are
    no handles to bring from another net; and an outer net has no node for a port,
    whose place in its subnet names a group of the subnet's. What remains is the
    type of the name."""
    with pytest.raises(ValidationError):
        ListPlaceNode("A", int, group=3)
    with pytest.raises(ValidationError):
        FunctionTransitionNode("T", same, group=["One", "Two"])
    with pytest.raises(ValidationError):
        SubnetPlaceNode("M", mill(), group=1.5)
    assert ListPlaceNode("A", int).group is None


# --- copies, templates and saving --------------------------------------------------------------


def test_a_copy_carries_the_groups():
    f = farm()
    copy = deepcopy(f)
    assert copy.groups == f.groups and copy.subnet("Mill").groups == ("Grinding",)
    assert members(copy, "Milling") == ["Mill", "Cart"]
    assert NestedNets.fork(mill()).groups == ("Grinding",)
    assert f.place_named("Field").copy_sans_tokens().group == "Harvest"


def test_a_template_carries_the_groups_and_the_saved_form_never_names_them():
    stamped = Templates.register("Test Groups Mill", lambda: mill())
    try:
        built = stamped()
        built.place_named("Hopper").tokens.append(5)
        restored = NetJson.restore(json.loads(json.dumps(NetJson.save(built))))
        assert restored.groups == ("Grinding",)
        assert members(restored, "Grinding") == ["Hopper", "Grind"]
        assert restored.place_named("Hopper").tokens == [5]
    finally:
        Templates.forget("Test Groups Mill")


def test_a_net_saves_to_the_same_text_with_and_without_groups():
    grouped, plain = farm(), farm(grouped=False)
    assert json.dumps(NetJson.save(grouped)) == json.dumps(NetJson.save(plain))
    assert "Harvest" not in json.dumps(NetJson.save(grouped))
    loaded = NetJson.load_into(farm(), NetJson.save(plain))
    assert loaded.groups == ("Harvest", "Milling", "Selling"), "loading keeps the definition's groups"


# --- groups change nothing about how a net runs ------------------------------------------------


def normalised(events: list) -> list:
    """Events with what differs between two builds of one net made equal: each
    nested id by the order it first appears, and every duration."""
    ids: dict[int, int] = {}

    def one(event):
        if isinstance(event, Nested):
            return dataclasses.replace(event, nested_id=ids.setdefault(event.nested_id, len(ids)), event=one(event.event))
        if isinstance(event, Fired):
            return dataclasses.replace(event, duration_ms=0.0)
        return event

    return [one(event) for event in events]


def marking(graph: ExecutableGraph) -> dict:
    """Every place's tokens and ids, the subnets' at every depth included."""
    out = {}
    for place in graph.places:
        if isinstance(place, SubnetPlaceNode):
            out[place.name] = marking(place.graph)
        else:
            out[place.name] = (list(place.tokens), place.token_ids)
    return out


def journey(graph: ExecutableGraph, budget: int = 100) -> tuple:
    """Step until nothing can fire; what fired, the marking, the saved text and the
    events, each as the net left them."""
    fired = []
    for _ in range(budget):
        what = run(ExecutableGraphOperations.fire_one_transition(graph, *ExecutableGraphOperations.maps(graph)))
        if what is None:
            break
        fired.append(what)
    return (
        fired, marking(graph), json.dumps(NetJson.export_marking(graph)), graph.step_count, dict(graph.fired_counts),
        normalised(graph.drain_events()),
    )


@pytest.mark.parametrize("tie_break", TIE_BREAKS)
def test_a_net_built_twice_with_and_without_groups_steps_the_same(tie_break):
    grouped = journey(farm(True, tie_break))
    plain = journey(farm(False, tie_break))
    assert grouped[0], "something fired"
    assert grouped == plain
    assert len(set(map(tuple, (journey(farm(True, other))[0] for other in TIE_BREAKS)))) > 1, \
        "the tie-breaks give different schedules, so each is a test of its own"


def test_the_example_steps_the_same_with_its_groups_cleared():
    from examples.groups.hike import build_hike

    def cleared(graph: ExecutableGraph) -> ExecutableGraph:
        copy = deepcopy(graph)
        for node in (*copy.places, *copy.transitions):
            node.group = None
        assert copy.groups == ()
        return copy

    original = build_hike()
    plain = cleared(original)
    assert journey(original) == journey(plain)


# --- the Graphviz frames -----------------------------------------------------------------------


def drawing(graph: ExecutableGraph):
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    pydigraph = RustworkxGraph.from_executable_graph(graph)
    node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
    index = {pydigraph[i].name: i for i in pydigraph.node_indices()}
    return pydigraph, node_attr_fn, edge_attr_fn, index


def frame(position: int, name: str, indices: list[int]) -> str:
    return f'subgraph cluster_{position} {{\nlabel="{name}";\nstyle=dotted;\n' + "".join(f"{i};\n" for i in indices) + "}\n"


def test_a_net_with_no_groups_draws_the_source_it_drew_before():
    from examples.subnet_places.levels import build_world
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    for graph in (farm(grouped=False), build_world()):
        pydigraph, node_attr_fn, edge_attr_fn, _ = drawing(graph)
        assert RustworkxToGraphviz.group_frames(pydigraph) == []
        assert RustworkxToGraphviz.dot_source(pydigraph, node_attr_fn, edge_attr_fn) == \
            pydigraph.to_dot(node_attr_fn, edge_attr_fn)


def test_each_group_is_a_dotted_frame_with_its_name_in_order_holding_its_members():
    """Dotted since 2026-11-28, where it was dashed, so that a frame is not taken for
    the longer dashes of an open net's places."""
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    f = farm()
    pydigraph, node_attr_fn, edge_attr_fn, index = drawing(f)
    source = RustworkxToGraphviz.dot_source(pydigraph, node_attr_fn, edge_attr_fn)
    plain = pydigraph.to_dot(node_attr_fn, edge_attr_fn)
    frames = (
        frame(0, "Harvest", [index["Field"], index["Barn"], index["Reap"]])
        + frame(1, "Milling", [index["Mill"], index["Cart"]])
        + frame(2, "Selling", [index["Sell"]])
    )
    assert source == plain.rstrip()[:-1] + frames + "}\n", "the frames, in order, before the closing brace"
    assert "Market" in plain and index["Market"] not in [i for _, m in RustworkxToGraphviz.group_frames(pydigraph) for i in m]


def test_a_subnet_place_in_a_group_is_drawn_in_its_frame_and_the_subnet_draws_its_own():
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    f = farm()
    pydigraph, _, _, index = drawing(f)
    assert ("Milling", [index["Mill"], index["Cart"]]) in RustworkxToGraphviz.group_frames(pydigraph)
    inside, _, _, inner = drawing(f.subnet("Mill"))
    assert RustworkxToGraphviz.group_frames(inside) == [("Grinding", [inner["Hopper"], inner["Grind"]])]


def test_a_port_in_a_group_has_one_dotted_ring_inside_the_dotted_frame():
    """The mill's own drawing: ``Hopper``, a port in the group ``Grinding``, is drawn
    as every port is since 2026-11-28, one dotted ring and no second one, and the
    frame around it is dotted as every frame is; neither changes the other."""
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    inside, node_attr_fn, edge_attr_fn, inner = drawing(farm().subnet("Mill"))
    source = RustworkxToGraphviz.dot_source(inside, node_attr_fn, edge_attr_fn)
    assert frame(0, "Grinding", [inner["Hopper"], inner["Grind"]]) in source
    assert "peripheries" not in source
    for port in ("Hopper", "Flour"):
        assert node_attr_fn(inside[inner[port]])["style"] == '"filled,dotted"'


def test_a_frame_title_draws_any_name_as_it_is():
    from petritype.plotting.rustworkx_to_graphviz import NodeLabel, RustworkxToGraphviz
    assert NodeLabel.quoted_text('a "b" \\ c\nd {e|f}') == 'a \\"b\\" \\\\ c\\nd {e|f}'
    odd = ExecutableGraphOperations.construct_graph([ListPlaceNode("A", int, group='say "hi" \\o/')])
    pydigraph, _, _, _ = drawing(odd)
    assert 'label="say \\"hi\\" \\\\o/";' in RustworkxToGraphviz.dot_source(pydigraph)


@pytest.mark.skipif(shutil.which("dot") is None, reason="needs the Graphviz dot program")
def test_graphviz_puts_each_member_inside_its_frame():
    """Read back from Graphviz itself: each cluster is dotted, titled with its
    group's name, and holds exactly its members; the nodes in no group stand
    outside every frame."""
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization
    f = farm()
    pydigraph, node_attr_fn, edge_attr_fn, index = drawing(f)
    source = RustworkxToGraphviz.dot_source(pydigraph, node_attr_fn, edge_attr_fn)
    laid_out = json.loads(subprocess.run(["dot", "-Tjson"], input=source.encode(), capture_output=True, check=True).stdout)
    objects = laid_out["objects"]
    names = {obj["_gvid"]: obj["name"] for obj in objects if "nodes" not in obj}
    clusters = {
        obj["label"]: sorted(int(names[gvid]) for gvid in obj["nodes"])
        for obj in objects if obj.get("name", "").startswith("cluster_")
    }
    assert clusters == {
        "Harvest": sorted([index["Field"], index["Barn"], index["Reap"]]),
        "Milling": sorted([index["Mill"], index["Cart"]]),
        "Selling": [index["Sell"]],
    }
    assert all(obj["style"] == "dotted" for obj in objects if obj.get("name", "").startswith("cluster_"))
    image = RustworkxToGraphviz.draw(pydigraph, node_attr_fn, edge_attr_fn)
    assert image.width > 0 and image.height > 0
    assert SimpleGraphvizVisualization.graph(pydigraph).width > 0


# --- the example -------------------------------------------------------------------------------


def test_the_hike_example_runs_its_own_day():
    from examples.groups.hike import Energy, build_hike, day
    graph = build_hike()
    assert graph.groups == ("Walk", "Eat", "Rest")
    assert graph.place_named("Energy").group is None, "the shared place is in no group"
    assert run(day(graph)) == ["Walk", "Eat", "Walk", "Walk", "Rest", "Rest", "Walk"]
    assert {place.name: place.tokens for place in graph.places} == {
        "Energy": [Energy(level=0)],
        "Trail": [],
        "Walked": ["Leg 1", "Leg 2", "Leg 3", "Leg 4"],
        "Pack": [],
        "Eaten": ["Apple"],
        "Benches": [],
        "Rested At": ["Bench 1", "Bench 2"],
    }


@pytest.mark.parametrize("tie_break", TIE_BREAKS)
def test_the_hike_goes_the_same_way_under_every_tie_break(tie_break):
    from examples.groups.hike import build_hike, day
    graph = build_hike()
    graph.tie_break = tie_break
    assert run(day(graph)) == ["Walk", "Eat", "Walk", "Walk", "Rest", "Rest", "Walk"]
