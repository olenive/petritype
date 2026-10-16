"""The JSON form of tokens and nets: saving and loading markings, templates, and
the saved form of a net that a nested token carries (nets as tokens, R5).

- ``JsonForm``: which place types have a JSON form (a pydantic model, a JSON
  scalar or a subclass of one, or ``ExecutableGraph`` itself), how a token is
  encoded and how one is decoded back, strictly, from the JSON it arrived as.
  The protocol runner's ``inject`` and ``get_token`` use these rules
  (``petritype.runner.translate`` re-exports them), so a token saved here is a
  token the runner can inject.
- ``Templates``: a process-wide registry of named factories that build nets,
  ``Templates.register("Cabin", build_cabin)`` or ``@Templates.template("Cabin")``.
  A net records the template that built it (``ExecutableGraph.template``), which
  is what its saved form names so the net can be rebuilt.
- ``NetJson``: the saved form of a net, PROTOCOL.md §12.5::

      {"template": "Cabin",
       "marking": {"Door": [], "Seats": ["Ada", "Bram"], "Standing": ["Cato"], "Announcements": []},
       "params": {"stopped": true, "seats": 2, "capacity": 6},
       "enabled": {"Sit": true, "Stand": true, ...}}

  ``export_marking`` and ``import_marking`` are the marking part on their own,
  for any net whose places all have a JSON form: by place name, oldest token
  first; a place missing from the JSON loads empty; an unknown place is an
  error. Token ids are not preserved: a loaded marking numbers its tokens afresh
  (an id-preserving variant can follow). ``save`` and ``restore`` are the whole
  form; ``ExecutableGraph`` dumps and validates as it (a ``Vehicle`` model with a
  ``cabin: ExecutableGraph`` field therefore has the JSON form of §12.5 without
  further work), and restoring needs the template registered.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from pydantic import BaseModel, TypeAdapter

from petritype.core.events import Resynced
from petritype.core.executable_graph_components import (
    ExecutableGraph,
    GraphEvents,
    ListPlaceNode,
    TokenIds,
)

JSON_SCALARS = (bool, int, float, str)

SAVED_KEYS = ("template", "marking", "params", "enabled")


class JsonForm:
    """Functions that decide and use a place's JSON form."""

    def scalar(place_type: Any) -> Optional[type]:
        """The JSON scalar type ``place_type`` is or derives from (``str`` for a
        ``class Colour(str)``), else ``None``."""
        if not isinstance(place_type, type):
            return None
        return next((scalar for scalar in JSON_SCALARS if issubclass(place_type, scalar)), None)

    def place_type(place_type: Any) -> bool:
        """True for a pydantic model (whose JSON schema builds, so it has a JSON
        form; ``ExecutableGraph`` and models holding one included) or a JSON
        scalar type (``bool``, ``int``, ``float``, ``str``) or a subclass of one,
        whose JSON form is its scalar's (``scalar``). Whether a nested
        token's inner nets have a JSON form is known only per net (it depends on
        their places), so a nested place of a model type counts as serializable
        and a token whose inner net will not save shows as text."""
        if not isinstance(place_type, type):
            return False
        if JsonForm.scalar(place_type) is not None:
            return True
        if issubclass(place_type, BaseModel):
            try:
                place_type.model_json_schema()
            except Exception:
                return False
            return True
        return False

    def encode(token: Any) -> Any:
        """The token as plain JSON (for a serializable place). Raises if it has
        none. A pydantic model dumps by alias, so a field spelt differently in
        Python (``class_`` for ``class``) carries its JSON name, the one
        ``decode`` accepts."""
        if isinstance(token, BaseModel):
            return token.model_dump(mode="json", by_alias=True)
        return json.loads(json.dumps(token, allow_nan=False))

    def validate(target: Any, value: Any) -> Any:
        """Validate wire input as the JSON it arrived as, in pydantic's strict
        mode. ``target`` is a model class or a ``TypeAdapter``. Raises
        ``ValueError`` (pydantic's ``ValidationError`` is one) on a mismatch.

        Pydantic's default lax mode coerces ``"9"`` to ``9``, ``1`` to ``True``
        and ``true`` to ``1``; a wire value must already be the type it claims.
        Strict *JSON* mode rather than strict Python mode, because the value is
        JSON: strict Python mode refuses the only spellings JSON has for some
        types (an ISO 8601 string for a ``datetime``, an array for a ``tuple``
        or a ``set``), strict JSON mode takes them. It accepts a JSON integer
        for a ``float`` and makes it a ``float``. The value is re-serialised
        with ``json.dumps`` (never ``NaN``), so a value that is not plain JSON
        raises ``ValueError`` too."""
        try:
            text = json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError(f"not plain JSON: {error}") from error
        if isinstance(target, TypeAdapter):
            return target.validate_json(text, strict=True)
        return target.model_validate_json(text, strict=True)

    def message(error: ValueError) -> str:
        """Pydantic's first message for a ``ValidationError``, else the text."""
        errors = getattr(error, "errors", None)
        if callable(errors):
            first = errors()
            if first:
                return first[0].get("msg", str(error))
        return str(error)

    def decode(place_type: Any, payload: Any) -> Any:
        """Build a token of ``place_type`` from plain JSON: ``validate`` for a
        pydantic model (strict all the way down), the constructor for a scalar.
        Raises ``ValueError`` (or pydantic's ``ValidationError``, a subclass)
        when it does not decode.

        Scalars are as strict as the models: the JSON type must be the place's.
        An ``int`` place takes only an integer (never ``"9"``, never ``2.0``), a
        ``str`` place only a string (never ``9``), a ``bool`` place only
        ``true``/``false`` (``bool("false")`` is ``True``), and a ``float``
        place a number, an integer included, as strict JSON mode does for a
        model's float field. JSON's ``true`` is never ``1``.

        A subclass of a scalar (``class Colour(str)``) takes its scalar's JSON
        type and is built by its own constructor, ``Colour("red")``, so a
        token's JSON form can stay ``"red"`` while its type adds behaviour (a
        ``color``); a constructor that refuses the value raises ``ValueError``
        as a mismatch does."""
        if isinstance(place_type, type) and issubclass(place_type, BaseModel):
            return JsonForm.validate(place_type, payload)
        scalar = JsonForm.scalar(place_type)
        if scalar is None:
            raise ValueError(f"{getattr(place_type, '__name__', place_type)} has no JSON form")
        if payload is None or not isinstance(payload, JSON_SCALARS):
            raise ValueError(f"expected a JSON scalar for {place_type.__name__}, got {type(payload).__name__}")
        if scalar is bool:
            if not isinstance(payload, bool):
                raise ValueError(f"expected true or false, got {payload!r}")
            return payload
        if isinstance(payload, bool):
            raise ValueError(f"expected {place_type.__name__}, got a boolean")
        if scalar is int and not isinstance(payload, int):
            raise ValueError(f"expected an integer, got {payload!r}")
        if scalar is float and not isinstance(payload, (int, float)):
            raise ValueError(f"expected a number, got {payload!r}")
        if scalar is str and not isinstance(payload, str):
            raise ValueError(f"expected a string, got {payload!r}")
        try:
            return place_type(payload)
        except (TypeError, ValueError) as error:
            raise ValueError(str(error)) from error


class Templates:
    """A process-wide registry of named net factories: what rebuilds an inner
    net from its saved form. A factory is called with no arguments and must
    return an ``ExecutableGraph``; the saved parameters, marking and switches
    are then loaded into it, so a factory's own arguments, if any, need
    defaults. ``register`` or the ``template`` decorator stamp the name on the
    graphs the factory builds (``ExecutableGraph.template``) when the factory
    did not set one itself."""

    _factories: dict[str, Callable[..., ExecutableGraph]] = {}

    def register(name: str, factory: Callable[..., ExecutableGraph]) -> Callable[..., ExecutableGraph]:
        """Register ``factory`` under ``name`` (replacing a previous one of that
        name) and return a factory that stamps the name on what it builds."""
        if not isinstance(name, str) or not name:
            raise ValueError("a template needs a non-empty name")

        def stamped(*args, **kwargs) -> ExecutableGraph:
            graph = factory(*args, **kwargs)
            if not isinstance(graph, ExecutableGraph):
                raise TypeError(f"template {name!r} built {type(graph).__name__}, not an ExecutableGraph")
            if graph.template is None:
                graph.template = name
            return graph

        stamped.__name__ = getattr(factory, "__name__", name)
        stamped.__doc__ = getattr(factory, "__doc__", None)
        stamped.__wrapped__ = factory
        Templates._factories[name] = stamped
        return stamped

    def template(name: str):
        """``@Templates.template("Cabin")`` on a factory function."""
        def decorator(factory):
            return Templates.register(name, factory)
        return decorator

    def get(name: str) -> Optional[Callable[..., ExecutableGraph]]:
        return Templates._factories.get(name)

    def names() -> tuple[str, ...]:
        return tuple(Templates._factories)

    def build(name: str) -> ExecutableGraph:
        """A fresh net from the template ``name``; ``LookupError`` if none is registered."""
        factory = Templates._factories.get(name)
        if factory is None:
            raise LookupError(f"no template named {name!r} is registered (known: {sorted(Templates._factories)})")
        return factory()

    def forget(name: str) -> None:
        Templates._factories.pop(name, None)


class NetJson:
    """Functions that save a net as JSON and load one back (the module docstring)."""

    def export_marking(graph: ExecutableGraph) -> dict[str, list]:
        """``{place: [tokens as JSON]}`` for every place, oldest token first.
        Raises ``TypeError`` naming the place if one has no JSON form, and
        ``ValueError`` if a token will not encode."""
        marking: dict[str, list] = {}
        for place in graph.places:
            if not JsonForm.place_type(place.type):
                raise TypeError(f"place {place.name!r} ({place.type}) has no JSON form, so the marking cannot be saved")
            try:
                marking[place.name] = [JsonForm.encode(token) for token in place.tokens]
            except Exception as error:
                raise ValueError(f"a token of {place.name!r} will not encode as JSON: {error}") from error
        return marking

    def import_marking(graph: ExecutableGraph, marking: dict[str, list]) -> None:
        """Replace the whole marking with ``marking``: every place listed gets
        the tokens given, decoded strictly to its type; a place not listed is
        emptied; a name that is no place of the net is a ``KeyError``, and a
        token that does not decode a ``ValueError``, both before anything is
        changed. Tokens get fresh ids; each place records ``Resynced``, since the
        events cannot describe a marking replaced wholesale."""
        if not isinstance(marking, dict):
            raise TypeError("a marking is an object of place name to list of tokens")
        places = {place.name: place for place in graph.places}
        unknown = sorted(set(marking) - set(places))
        if unknown:
            raise KeyError(f"the net has no place {', '.join(map(repr, unknown))}")
        decoded: dict[str, list] = {}
        for name, place in places.items():
            tokens = marking.get(name, [])
            if not isinstance(tokens, list):
                raise ValueError(f"the tokens of {name!r} must be a list")
            try:
                decoded[name] = [JsonForm.decode(place.type, token) for token in tokens]
            except ValueError as error:
                raise ValueError(f"a token of {name!r} does not decode as {place.type}: {JsonForm.message(error)}") from error
        for name, place in places.items():
            place.tokens = []
            place._token_ids = []
            TokenIds.append(graph, place, decoded[name])
            GraphEvents.record(graph, Resynced(name))

    def save(graph: ExecutableGraph) -> dict:
        """The saved form: ``{template?, marking, params, enabled}``. ``template``
        is left out when the net records none (a top-level net built by a
        factory); ``restore`` then cannot rebuild it, but ``load_into`` can load
        it into a net built the same way."""
        try:
            params = json.loads(json.dumps(graph.params, allow_nan=False))
        except (TypeError, ValueError) as error:
            raise ValueError(f"a net parameter will not encode as JSON: {error}") from error
        saved: dict = {}
        if graph.template is not None:
            saved["template"] = graph.template
        saved["marking"] = NetJson.export_marking(graph)
        saved["params"] = params
        saved["enabled"] = {t.name: t.name not in graph.disabled for t in graph.transitions}
        return saved

    def load_into(graph: ExecutableGraph, saved: dict) -> ExecutableGraph:
        """Load a saved form into ``graph``: its marking (``import_marking``),
        parameters (each must be one the net declares) and switches (each must
        name a transition). Checked before anything is changed. Returns the graph."""
        if not isinstance(saved, dict):
            raise TypeError("a saved net is an object with marking, params and enabled")
        unknown = sorted(set(saved) - set(SAVED_KEYS))
        if unknown:
            raise KeyError(f"a saved net has no key {', '.join(map(repr, unknown))}")
        template = saved.get("template")
        if template is not None and graph.template is not None and template != graph.template:
            raise ValueError(f"the saved net is a {template!r}, this net a {graph.template!r}")
        params = saved.get("params", {})
        if not isinstance(params, dict):
            raise TypeError("params must be an object")
        undeclared = sorted(set(params) - set(graph.params))
        if undeclared:
            raise KeyError(f"the net declares no parameter {', '.join(map(repr, undeclared))}")
        enabled = saved.get("enabled", {})
        if not isinstance(enabled, dict) or not all(isinstance(on, bool) for on in enabled.values()):
            raise TypeError("enabled must be an object of transition name to boolean")
        names = {t.name for t in graph.transitions}
        unknown = sorted(set(enabled) - names)
        if unknown:
            raise KeyError(f"the net has no transition {', '.join(map(repr, unknown))}")
        NetJson.import_marking(graph, saved.get("marking", {}))
        graph.params.update(params)
        graph.disabled = {name for name, on in enabled.items() if not on}
        # The tie-break's and the deep step's cursors are not saved: a loaded net
        # starts its rounds afresh.
        graph.forget_cursors()
        return graph

    def restore(saved: dict) -> ExecutableGraph:
        """A net rebuilt from its saved form: ``Templates.build(saved["template"])``
        then ``load_into``. ``LookupError`` when the template is unknown or the
        form names none."""
        if not isinstance(saved, dict) or not isinstance(saved.get("template"), str):
            raise LookupError("a saved net names the template that rebuilds it")
        return NetJson.load_into(Templates.build(saved["template"]), saved)

    def looks_saved(value: Any) -> bool:
        """Whether ``value`` is a saved form rather than a graph's own fields
        (what ``ExecutableGraph`` validation uses to tell the two apart)."""
        return isinstance(value, dict) and "marking" in value and "places" not in value
