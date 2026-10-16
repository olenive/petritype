"""From a petritype net to the protocol's vocabulary: type names, token labels and
colours, JSON forms, transition detail and topology, and the controls and place
styles a net declares beside it.

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
from petritype.core.executable_graph_components import (
    DrainEdge, ExecutableGraph, ListPlaceNode, MutateWhereEdge, SurveyEdge, TakeWhereEdge,
)
from petritype.core.keys import Clause, Filter, Keys, is_key_value
from petritype.core.serialisation import JsonForm
from petritype.runner.protocol import ErrorCode, ProtocolError
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

# The control kinds whose ``config.value`` is an initial value (§5.1); a button's and a
# text's ``value``, if any, stays off the wire.
KINDS_WITH_INITIAL = frozenset({"toggle", "slider", "select"})


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

    def entry(
        token_id: int, token: Any, place_type_name: str, nets: Optional[list[wire.NestedNet]] = None,
        keys: Optional[dict] = None,
    ) -> wire.Token:
        """``{id, label, color?, type_name?, keys?, nets?}``; ``type_name`` only
        when the token's own type name differs from its place's; ``keys`` (§13.1)
        and ``nets`` (§12.2) as given."""
        own = type(token).__name__
        return wire.Token(
            id=str(token_id),
            label=Labels.of(token),
            color=Colors.of(token),
            type_name=own if own != place_type_name else None,
            keys=keys if keys is not None else None,
            nets=nets or None,
        )

    def keys(place: ListPlaceNode, token: Any) -> Optional[dict]:
        """The token's keys when its place declares some, else ``None``; a key
        that will not derive is left out rather than failing the read."""
        if not place.keys:
            return None
        try:
            return Keys.of(place, token)
        except Exception:
            return {}

    def nets(token: Any, prefix: str, place: str, token_id: int) -> list[wire.NestedNet]:
        """The ``nets`` of a nested token: one entry per inner net, with its path
        under ``prefix`` (the wire path of the net the token is in)."""
        from petritype.core.executable_graph_components import NestedNets
        entries = []
        for name, inner in NestedNets.of(token).items():
            entries.append(wire.NestedNet(
                name=name,
                nested_id=str(inner.net_id),
                path=f"{prefix}/{place}/#{token_id}/{name}",
                template=inner.template,
                count=sum(len(node.tokens) for node in inner.places),
            ))
        return entries

    def produced(place: str, token_id: int, token: Any, keys: Optional[dict] = None) -> list[Any]:
        """A ``fired.produced`` element: ``[place, id, label, color?]``, or with
        ``keys`` ``[place, id, label, color | null, keys]`` (§7.1)."""
        element: list[Any] = [place, str(token_id), Labels.of(token)]
        color = Colors.of(token)
        if keys is not None:
            element.extend([color, keys])
        elif color is not None:
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


class Filters:
    """Functions that read a filter off the wire and write one (§13.2)."""

    def from_wire(raw: Any, place_names: Optional[set[str]] = None) -> Filter:
        """A ``Filter`` from ``{clauses?, places?, prefix?}``; ``invalid_params``
        for a shape that is not one, ``unknown_place`` for a place not of the net."""
        if not isinstance(raw, dict):
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "a filter is an object")
        unknown = set(raw) - {"clauses", "places", "prefix"}
        if unknown:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, f"a filter has no {', '.join(map(repr, sorted(unknown)))}")
        clauses = []
        for clause in raw.get("clauses", []) or []:
            if not isinstance(clause, dict) or not isinstance(clause.get("key"), str):
                raise ProtocolError(ErrorCode.INVALID_PARAMS, 'a clause is {"key", "values"} or {"key", "param"}')
            if "param" in clause:
                if not isinstance(clause["param"], str) or "values" in clause:
                    raise ProtocolError(ErrorCode.INVALID_PARAMS, 'a clause has "values" or a string "param"')
                clauses.append(Clause(clause["key"], param=clause["param"]))
                continue
            values = clause.get("values")
            if not isinstance(values, list) or not values or not all(is_key_value(v) for v in values):
                raise ProtocolError(
                    ErrorCode.INVALID_PARAMS, 'a clause\'s "values" is a non-empty list of strings, integers or booleans',
                )
            clauses.append(Clause(clause["key"], tuple(values)))
        places = raw.get("places")
        if places is not None:
            if not isinstance(places, list) or not all(isinstance(name, str) for name in places):
                raise ProtocolError(ErrorCode.INVALID_PARAMS, '"places" is a list of place names')
            if place_names is not None:
                missing = [name for name in places if name not in place_names]
                if missing:
                    raise ProtocolError(ErrorCode.UNKNOWN_PLACE, f"no place named {missing[0]!r}")
            places = tuple(places)
        prefix = raw.get("prefix")
        if prefix is not None and not isinstance(prefix, str):
            raise ProtocolError(ErrorCode.INVALID_PARAMS, '"prefix" is a string')
        return Filter(tuple(clauses), places, prefix)

    def to_wire(filter: Filter) -> dict:
        out: dict = {"clauses": [
            {"key": c.key, "param": c.param} if c.param is not None else {"key": c.key, "values": list(c.values)}
            for c in filter.clauses
        ]}
        if filter.places is not None:
            out["places"] = list(filter.places)
        if filter.prefix is not None:
            out["prefix"] = filter.prefix
        return out


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


class Controls:
    """Functions that put a net's declared controls on the wire and check the value a
    ``press`` carries (PROTOCOL.md §5.1, §6). A control is a ``ControlSpec`` (read by
    its fields, so this module needs no runtime import)."""

    def wire(controls: dict, graph: ExecutableGraph) -> list[wire.Control]:
        """The control-map as ``topology.controls``, in declaration order: ``node``,
        ``action`` and ``config`` left out when absent or empty, and ``config.value``
        when the kind has no initial value (``KINDS_WITH_INITIAL``). ``ValueError`` for a
        ``node`` that is no place or transition of the net or a ``config`` that is not
        plain JSON, and pydantic's (a ``ValueError`` too) for a kind or an action outside
        the vocabulary."""
        nodes = {place.name for place in graph.places} | {transition.name for transition in graph.transitions}
        controls_on_the_wire = []
        for name, spec in controls.items():
            config = dict(spec.config or {})
            if spec.kind not in KINDS_WITH_INITIAL:
                config.pop("value", None)
            try:
                config = json.loads(json.dumps(config, allow_nan=False))
            except (TypeError, ValueError) as error:
                raise ValueError(f"control {name!r}: config is not plain JSON: {error}") from error
            if spec.node and spec.node not in nodes:
                raise ValueError(f"control {name!r} names {spec.node!r}, no place or transition of the net")
            controls_on_the_wire.append(wire.Control(
                name=name, label=spec.label, kind=spec.kind, node=spec.node or None,
                action=spec.action, config=config or None,
            ))
        return controls_on_the_wire

    def value(spec: Any, params: dict) -> Any:
        """The value a ``press`` of ``spec`` carries, checked by its kind: none for a
        ``button``, which hands ``to_command`` ``True`` (clicked, as a notebook's button
        does); a boolean for a ``toggle``; a number for a ``slider``, within
        ``config.start`` and ``config.stop`` where they are given; one of
        ``config.options`` for a ``select``; a string for a ``text``.
        ``invalid_params`` otherwise. Pydantic is not used: the JSON type is the rule,
        so ``true`` is no number and ``"3"`` no number either."""
        kind, config = spec.kind, spec.config or {}
        given, value = "value" in params, params.get("value")
        if kind == "button":
            if given:
                raise ProtocolError(ErrorCode.INVALID_PARAMS, "a button takes no value")
            return True
        if not given:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, f"a {kind} control needs a value")
        if kind == "toggle":
            if not isinstance(value, bool):
                raise ProtocolError(ErrorCode.INVALID_PARAMS, f"a toggle's value is true or false, not {value!r}")
            return value
        if kind == "slider":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ProtocolError(ErrorCode.INVALID_PARAMS, f"a slider's value is a number, not {value!r}")
            start, stop = config.get("start"), config.get("stop")
            if (start is not None and value < start) or (stop is not None and value > stop):
                raise ProtocolError(
                    ErrorCode.INVALID_PARAMS, f"{value!r} is outside the slider's range {start}..{stop}",
                )
            return value
        if kind == "select":
            options = list(config.get("options") or ())
            if not isinstance(value, str) or value not in options:
                raise ProtocolError(ErrorCode.INVALID_PARAMS, f"{value!r} is not one of the options {options}")
            return value
        if kind == "text":
            if not isinstance(value, str):
                raise ProtocolError(ErrorCode.INVALID_PARAMS, f"a text's value is a string, not {value!r}")
            return value
        raise ProtocolError(ErrorCode.INTERNAL, f"the control has an unknown kind {kind!r}")


class Topologies:
    """Functions that read a net's shape (PROTOCOL.md §5.1)."""

    def declare(topology: wire.Topology, controls: list[wire.Control], place_styles: Optional[dict]) -> None:
        """Put on ``topology`` what a net's entry declares beside the net: its
        ``controls`` (left out when there are none) and the ``style`` of each place in
        ``place_styles`` (``{place: PlaceStyle}``). ``ValueError`` for a style on a
        place the net does not have."""
        if place_styles:
            places = {place.name: place for place in topology.places}
            for name, style in place_styles.items():
                if name not in places:
                    raise ValueError(f"a place style names {name!r}, no place of the net")
                places[name].style = wire.PlaceStyle(fill=getattr(style, "fill", None))
        topology.controls = controls or None

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
        """The net's shape. An input edge's ``kind`` is its class's (``take_where``,
        ``drain``, ``snapshot``, ``survey``, ``mutate``, ``mutate_where``), or for a
        plain argument edge ``take_all`` when its argument takes every token as a
        list and ``take`` otherwise."""
        places = {place.name: place for place in graph.places}
        functions = {transition.name: transition.function for transition in graph.transitions}
        edges: list[wire.Edge] = []
        for edge in graph.argument_edges:
            if isinstance(edge, TakeWhereEdge):
                edges.append(wire.Edge(
                    place=edge.place_node_name, transition=edge.transition_node_name,
                    direction="in", kind="take_where", filter=Filters.to_wire(edge.where),
                ))
                continue
            if isinstance(edge, DrainEdge):
                edges.append(wire.Edge(
                    place=edge.place_node_name, transition=edge.transition_node_name,
                    direction="in", kind="drain",
                ))
                continue
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
                direction="in", kind="survey" if isinstance(edge, SurveyEdge) else "snapshot",
            ))
        for edge in graph.mutate_edges:
            if isinstance(edge, MutateWhereEdge):
                edges.append(wire.Edge(
                    place=edge.place_node_name, transition=edge.transition_node_name,
                    direction="in", kind="mutate_where", filter=Filters.to_wire(edge.where),
                ))
                continue
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
                    nested=True if place.nested else None,
                    passive=True if place.passive else None,
                    key_names=list(place.key_names) if place.keys else None,
                )
                for place in graph.places
            ],
            transitions=[
                wire.Transition(name=transition.name, detail=Details.of(transition.function))
                for transition in graph.transitions
            ],
            edges=edges,
        )

