"""From a petritype net to the protocol's vocabulary: type names, token labels and
colours, JSON forms, transition detail and topology.

Moved from petritype-server's ``worker/net_runner.py`` and ``engine.py`` (the
label rule, ``.color`` resolution, signature introspection, ``inject``
decoding), minus what the protocol dropped: layout, ``id(token)`` ids and
``Type@timestamp_hash`` spellings.
"""

from __future__ import annotations

import inspect
import json
import re
import types
from typing import Any, Callable, Literal, Optional, TypeAliasType, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, TypeAdapter

from petritype.core import serialisation as _serialisation
from petritype.core.executable_graph_components import ExecutableGraph, ListPlaceNode
from petritype.core.serialisation import JsonForm
from petritype.core.type_comparisons import CompareTypes
from petritype.runner import protocol as wire


# The wire cap on a label, in Unicode scalar values, *including* the marker
# (PROTOCOL.md §5.3): a cut label is 997 characters of text plus "...".
LABEL_CAP = 1000
CUT_MARKER = "..."

# Friendly colour names a token's ``.color`` may use, resolved here so a viewer
# never needs the table (the server's table, unchanged).
COLOR_NAMES = {
    "red": "#ef4444",
    "blue": "#3b82f6",
    "green": "#22c55e",
    "yellow": "#eab308",
    "purple": "#a855f7",
    "orange": "#f97316",
    "pink": "#ec4899",
    "gray": "#6b7280",
}

# The JSON-form rules live in the core now (``petritype.core.serialisation``), so
# a token saved by the library is a token the runner can inject; these names stay
# for the runner's own modules.
JSON_SCALARS = _serialisation.JSON_SCALARS


class TypeNames:
    """Functions that spell Python types for display."""

    def of(annotation: Any) -> str:
        """Short text for a type or annotation: class names without module paths,
        generics with their arguments, unions with ``|``. A string annotation
        (postponed evaluation) is returned as written."""
        if annotation is inspect.Parameter.empty:
            return "Any"
        if isinstance(annotation, str):
            return annotation
        if annotation is None or annotation is type(None):
            return "None"
        if annotation is Ellipsis:
            return "..."
        if isinstance(annotation, TypeAliasType):
            return annotation.__name__
        origin, args = get_origin(annotation), get_args(annotation)
        if origin is Union or origin is types.UnionType:
            return " | ".join(TypeNames.of(arg) for arg in args)
        if origin is Literal:
            return f"Literal[{', '.join(repr(arg) for arg in args)}]"
        if origin is not None and args:
            return f"{TypeNames.of(origin)}[{', '.join(TypeNames.of(arg) for arg in args)}]"
        name = getattr(annotation, "__name__", None)
        if isinstance(name, str):
            return name
        return re.sub(r"(?:\w+\.)+(\w+)", r"\1", str(annotation))


class Labels:
    """Functions that render a token as the protocol's short ``label`` (§5.3)."""

    def cut(text: str) -> str:
        """``text`` capped at ``LABEL_CAP`` characters, marker included."""
        if len(text) <= LABEL_CAP:
            return text
        return text[: LABEL_CAP - len(CUT_MARKER)] + CUT_MARKER

    def full(token: Any) -> str:
        """The uncut rendering: the pydantic JSON dump if it works, else ``repr``."""
        if isinstance(token, BaseModel):
            try:
                return token.model_dump_json()
            except Exception:
                pass
        try:
            return repr(token)
        except Exception:
            return f"<{type(token).__name__}>"

    def of(token: Any) -> str:
        return Labels.cut(Labels.full(token))


class Colors:
    """Functions that resolve a token's ``.color`` to a CSS hex string."""

    def of(token: Any) -> Optional[str]:
        """A ``#``-prefixed ``.color`` as given, a known name from ``COLOR_NAMES``,
        else ``None`` (no ``color`` on the wire). The server fell back to grey for
        an unknown or missing colour; the protocol makes ``color`` optional, so
        the runner sends none rather than invent one."""
        try:
            raw = getattr(token, "color", None)
        except Exception:
            return None
        if not raw or not isinstance(raw, str):
            return None
        if raw.startswith("#"):
            return raw
        return COLOR_NAMES.get(raw.lower())


class Tokens:
    """Functions that put a token on the wire."""

    def entry(token_id: int, token: Any, place_type_name: str) -> wire.Token:
        """``{id, label, color?, type_name?}``; ``type_name`` only when the token's
        own type name differs from its place's."""
        own = type(token).__name__
        return wire.Token(
            id=str(token_id),
            label=Labels.of(token),
            color=Colors.of(token),
            type_name=own if own != place_type_name else None,
        )

    def produced(place: str, token_id: int, token: Any) -> list[str]:
        """A ``fired.produced`` element: ``[place, id, label, color?]``."""
        element = [place, str(token_id), Labels.of(token)]
        color = Colors.of(token)
        if color is not None:
            element.append(color)
        return element

    def json_value(token: Any) -> Any:
        """The token as plain JSON (for a serializable place). Raises if it has none."""
        return JsonForm.encode(token)


class StrictJson:
    """Validate wire input as the JSON it arrived as, in pydantic's strict mode
    (``JsonForm.validate``; the rationale is there)."""

    def validate(target: Any, value: Any) -> Any:
        return JsonForm.validate(target, value)

    def message(error: ValueError) -> str:
        return JsonForm.message(error)


class Serializable:
    """Functions that decide and use a place's JSON form (``JsonForm``)."""

    def place_type(place_type: Any) -> bool:
        return JsonForm.place_type(place_type)

    def decode(place_type: Any, payload: Any) -> Any:
        return JsonForm.decode(place_type, payload)


class Details:
    """Functions that introspect a transition's function for ``detail``."""

    def signature(function: Callable) -> str:
        """``(parcel: Parcel) -> Sorted``: parameters with short type names and
        defaults, and the return type."""
        signature = inspect.signature(function)
        parts = []
        for name, parameter in signature.parameters.items():
            prefix = {inspect.Parameter.VAR_POSITIONAL: "*", inspect.Parameter.VAR_KEYWORD: "**"}.get(
                parameter.kind, ""
            )
            part = f"{prefix}{name}"
            if parameter.annotation is not inspect.Parameter.empty:
                part += f": {TypeNames.of(parameter.annotation)}"
            if parameter.default is not inspect.Parameter.empty:
                part += f" = {parameter.default!r}"
            parts.append(part)
        text = f"({', '.join(parts)})"
        if signature.return_annotation is not inspect.Signature.empty:
            text += f" -> {TypeNames.of(signature.return_annotation)}"
        return text

    def of(function: Callable) -> dict:
        """``{function_name, signature?, docstring?}``; a key that cannot be had is
        left out."""
        detail: dict = {"function_name": getattr(function, "__name__", None) or repr(function)}
        try:
            detail["signature"] = Details.signature(function)
        except (TypeError, ValueError):
            pass
        doc = getattr(function, "__doc__", None) if inspect.isroutine(function) else None
        if isinstance(doc, str) and doc.strip():
            detail["docstring"] = inspect.cleandoc(doc)
        return detail


class Topologies:
    """Functions that read a net's shape (PROTOCOL.md §5.1)."""

    def takes_all(function: Callable, argument: str, place: ListPlaceNode) -> bool:
        """Whether an argument edge hands the body every token as a list, by the
        same test stage 1 applies."""
        try:
            argument_type = get_type_hints(function).get(argument)
        except Exception:
            return False
        return get_origin(argument_type) is list and CompareTypes.between_annotations_where_one_maybe_in_list(
            annotation_not_in_list=place.type,
            annotation_maybe_in_list=argument_type,
        )

    def of(graph: ExecutableGraph) -> wire.Topology:
        places = {place.name: place for place in graph.places}
        functions = {transition.name: transition.function for transition in graph.transitions}
        edges: list[wire.Edge] = []
        for edge in graph.argument_edges:
            takes_all = Topologies.takes_all(
                functions[edge.transition_node_name], edge.argument, places[edge.place_node_name]
            )
            edges.append(wire.Edge(
                place=edge.place_node_name, transition=edge.transition_node_name,
                direction="in", kind="take_all" if takes_all else "take",
            ))
        for edge in graph.snapshot_edges:
            edges.append(wire.Edge(
                place=edge.place_node_name, transition=edge.transition_node_name,
                direction="in", kind="snapshot",
            ))
        for edge in graph.mutate_edges:
            edges.append(wire.Edge(
                place=edge.place_node_name, transition=edge.transition_node_name,
                direction="in", kind="mutate",
            ))
        outputs: dict[str, int] = {}
        for edge in graph.return_edges:
            outputs[edge.transition_node_name] = outputs.get(edge.transition_node_name, 0) + 1
        for edge in graph.return_edges:
            several = outputs[edge.transition_node_name] > 1
            edges.append(wire.Edge(
                place=edge.place_node_name, transition=edge.transition_node_name,
                direction="out", slot=edge.return_index if several else None,
            ))
        return wire.Topology(
            places=[
                wire.Place(
                    name=place.name,
                    type_name=TypeNames.of(place.type),
                    serializable=Serializable.place_type(place.type),
                )
                for place in graph.places
            ],
            transitions=[
                wire.Transition(name=transition.name, detail=Details.of(transition.function))
                for transition in graph.transitions
            ],
            edges=edges,
        )

