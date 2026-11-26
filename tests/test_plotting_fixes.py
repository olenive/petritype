"""Tests for core TypeNames extraction and plotting correctness fixes."""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, Optional, Union

import pytest

from petritype import _optional
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.core.keys import Filter
from petritype.core.type_names import TypeNames
from petritype.runner.translate import TypeNames as RunnerTypeNames


type SampleAlias = int | str


def top_level_step(x: int | str) -> int | str:
    return x


def add_offset(offset: int, value: int | str) -> int | str:
    return value


class CallableProcessor:
    def __call__(self, x: int | str) -> int | str:
        return x

    def process(self, x: int | str) -> int | str:
        return x


class NamedOnlyCallable:
    def __init__(self, name: str):
        self.__name__ = name

    def __call__(self, x: Any) -> Any:
        return x


# --- TypeNames in core & re-export from runner.translate ---


def test_type_names_reexported_from_translate():
    assert RunnerTypeNames is TypeNames


def test_type_names_of_annotations():
    assert TypeNames.of(int) == "int"
    assert TypeNames.of(int | str) == "int | str"
    assert TypeNames.of(Optional[int]) == "int | None"
    assert TypeNames.of(Union[int, str]) == "int | str"
    assert TypeNames.of(list[int]) == "list[int]"
    assert TypeNames.of(dict[str, tuple[int, ...]]) == "dict[str, tuple[int, ...]]"
    assert TypeNames.of(Any) == "Any"
    assert TypeNames.of(None) == "None"
    assert TypeNames.of(type(None)) == "None"
    assert TypeNames.of(Literal["a", 1]) == "Literal['a', 1]"
    assert TypeNames.of(inspect.Parameter.empty) == "Any"
    assert TypeNames.of("ForwardType") == "ForwardType"
    assert TypeNames.of(SampleAlias) == "SampleAlias"
    assert TypeNames.of(Ellipsis) == "..."


def test_type_names_callable_name():
    assert TypeNames.callable_name(top_level_step) == "top_level_step"
    assert TypeNames.callable_name(len) == "len"
    assert TypeNames.callable_name(CallableProcessor().process) == "CallableProcessor.process"
    assert TypeNames.callable_name(CallableProcessor()) == "CallableProcessor"
    assert TypeNames.callable_name(NamedOnlyCallable("custom_fn")) == "custom_fn"
    assert TypeNames.callable_name(functools.partial(add_offset, 1)) == "partial(add_offset)"
    assert TypeNames.callable_name(functools.partial(functools.partial(add_offset, 1), 2)) == "partial(add_offset)"
    assert TypeNames.callable_name(functools.partial(CallableProcessor())) == "partial(CallableProcessor)"
    assert "<lambda>" in TypeNames.callable_name(lambda x: x)


# --- rustworkx_to_graphviz label fixes ---


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


# --- simple_graphviz fixes ---


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


def test_simple_graphviz_does_not_import_or_require_matplotlib(monkeypatch):
    pytest.importorskip("rustworkx")
    source_path = Path(__file__).resolve().parent.parent / "petritype" / "plotting" / "simple_graphviz.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "matplotlib" not in imported

    # Reimporting succeeds even if find_spec reports matplotlib is absent.
    real_find_spec = _optional.find_spec
    monkeypatch.setattr(
        _optional,
        "find_spec",
        lambda name: None if name == "matplotlib" else real_find_spec(name),
    )
    monkeypatch.delitem(sys.modules, "petritype.plotting.simple_graphviz", raising=False)
    mod = importlib.import_module("petritype.plotting.simple_graphviz")
    assert hasattr(mod, "SimpleGraphvizVisualization")


def test_animate_execution_generator_docstring_uses_async_sleep():
    pytest.importorskip("rustworkx")
    from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

    doc = SimpleGraphvizVisualization.animate_execution_generator.__doc__ or ""
    assert "await asyncio.sleep(1.0)" in doc
    assert "time.sleep" not in doc


# --- End-to-end Graphviz dot validation ---


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
