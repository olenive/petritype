"""Token keys, filters and queries (``TOKEN_QUERYING_AND_FILTERING.md``, R1 and R2).

A place may declare *keys*: named attributes derived from each token's value by
an extractor the net author gives, ``ListPlaceNode(..., keys={"species": lambda
s: s.name, "class": lambda s: s.class_})`` or, for attribute names, ``keys=
("name", "diet")``. A key value is a string, an integer or a boolean, nothing
else (``KeyValue``); ``1`` and ``True`` are different values. Keys are derived
from the value, so a token mutated in place has new keys the next time they are
read; there is no index to keep in step (Python's tokens are plain objects in
plain lists, and a query over a net of this size costs a scan).

A ``Filter`` is what a query, a viewer and a projection (PROTOCOL.md §13) share:
clauses ANDed together, each accepting one of a set of values for a key (OR
within the clause) or the current value of a net parameter; and a restriction to
some places, by name or by name prefix. ``Query.matches`` returns the matching
tokens in place order then token order, each with its place, id, keys and value;
``Query.distinct`` the distinct values of a key in order of first appearance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from petritype.core.executable_graph_components import ExecutableGraph, ListPlaceNode, TokenIds

KeyValue = Union[str, int, bool]
KEY_TYPES = (str, int, bool)


def is_key_value(value: Any) -> bool:
    """A string, an integer or a boolean (``bool`` is an ``int`` to Python; here it
    is its own type, and no other ``int`` subclass counts)."""
    return type(value) in KEY_TYPES


def same_value(a: Any, b: Any) -> bool:
    """Equal as key values: same type and equal, so ``1`` is not ``True`` and
    neither is ``"1"``."""
    return type(a) is type(b) and a == b


class Keys:
    """Functions that read the keys a place derives from its tokens."""

    def names(place: ListPlaceNode) -> tuple[str, ...]:
        """The key names a place declares, in declaration order; empty for none."""
        return tuple(place.keys) if place.keys else ()

    def of(place: ListPlaceNode, token: Any) -> dict[str, KeyValue]:
        """The token's keys as the place derives them now. ``TypeError`` if an
        extractor gives anything but a string, an integer or a boolean."""
        keys: dict[str, KeyValue] = {}
        for name, extract in (place.keys or {}).items():
            value = extract(token)
            if not is_key_value(value):
                raise TypeError(
                    f"key {name!r} of a token in {place.name!r} is {type(value).__name__}; "
                    f"a key is a string, an integer or a boolean"
                )
            keys[name] = value
        return keys


# --- filters ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Clause:
    """One condition on one key: the token's value for ``key`` is one of
    ``values``, or, with ``param`` instead, equals the net parameter so named as
    it stands when the filter is applied. Exactly one of the two is given."""

    key: str
    values: tuple[KeyValue, ...] = ()
    param: Optional[str] = None

    def __post_init__(self):
        if (self.param is None) == (not self.values):
            raise ValueError("a clause has either values or a param")
        if not all(is_key_value(value) for value in self.values):
            raise TypeError("a clause's values are strings, integers or booleans")

    def accepts(self, keys: Mapping[str, KeyValue], params: Mapping[str, Any]) -> bool:
        if self.key not in keys:
            return False
        value = keys[self.key]
        if self.param is not None:
            return self.param in params and same_value(value, params[self.param])
        return any(same_value(value, accepted) for accepted in self.values)


@dataclass(frozen=True)
class Filter:
    """What tokens to keep: every clause must accept the token (AND), within the
    named ``places`` and the places whose name begins with ``prefix``, if given.
    No clauses and no restriction keeps everything."""

    clauses: tuple[Clause, ...] = ()
    places: Optional[tuple[str, ...]] = None
    prefix: Optional[str] = None

    @staticmethod
    def where(**accepted: Union[KeyValue, Sequence[KeyValue], set]) -> "Filter":
        """``Filter.where(class_="Bird", diet=["Insectivore", "Omnivore"])``: a
        clause per keyword, a trailing underscore dropped so Python keywords can
        name keys (``class_`` is the key ``class``)."""
        clauses = []
        for name, values in accepted.items():
            key = name[:-1] if name.endswith("_") and not name.endswith("__") else name
            if is_key_value(values):
                values = (values,)
            clauses.append(Clause(key, tuple(values)))
        return Filter(tuple(clauses))

    def in_places(self, *places: str) -> "Filter":
        return Filter(self.clauses, tuple(places), self.prefix)

    def with_prefix(self, prefix: str) -> "Filter":
        return Filter(self.clauses, self.places, prefix)

    def covers(self, place_name: str) -> bool:
        """Whether tokens of this place are considered at all."""
        if self.places is not None and place_name not in self.places:
            return False
        if self.prefix is not None and not place_name.startswith(self.prefix):
            return False
        return True

    def accepts(self, keys: Mapping[str, KeyValue], params: Mapping[str, Any]) -> bool:
        return all(clause.accepts(keys, params) for clause in self.clauses)

    def matches(self, graph: ExecutableGraph, place: ListPlaceNode, token: Any) -> bool:
        """Whether ``token``, resting in ``place`` of ``graph``, passes the filter."""
        if not self.covers(place.name):
            return False
        if not self.clauses:
            return True
        if not place.keys:
            return False
        return self.accepts(Keys.of(place, token), graph.params)


EVERYTHING = Filter()


# --- queries ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    """One token a query found: where it rests, its id, its keys and the value."""

    place: str
    token_id: int
    keys: dict[str, KeyValue] = field(compare=False)
    token: Any = field(compare=False)


class Query:
    """Functions that ask a net for tokens by their keys."""

    def matches(graph: ExecutableGraph, filter: Filter = EVERYTHING) -> list[Match]:
        """Every token the filter keeps, in place order then token order."""
        TokenIds.resync(graph)
        found: list[Match] = []
        for place in graph.places:
            if not filter.covers(place.name):
                continue
            if filter.clauses and not place.keys:
                continue
            ids = place.token_ids
            for index, token in enumerate(place.tokens):
                keys = Keys.of(place, token)
                if filter.accepts(keys, graph.params):
                    found.append(Match(place.name, ids[index] if index < len(ids) else -1, keys, token))
        return found

    def count(graph: ExecutableGraph, place: ListPlaceNode, filter: Filter) -> int:
        """How many tokens of ``place`` the filter keeps."""
        return sum(1 for token in place.tokens if filter.matches(graph, place, token))

    def distinct(graph: ExecutableGraph, key: str, places: Optional[Sequence[str]] = None) -> list[KeyValue]:
        """The distinct values of ``key`` over the net (or ``places``), in order of
        first appearance in place order then token order; ``[]`` for a key no
        place declares."""
        seen: list[KeyValue] = []
        for place in graph.places:
            if places is not None and place.name not in places:
                continue
            if not place.keys or key not in place.keys:
                continue
            for token in place.tokens:
                value = Keys.of(place, token)[key]
                if not any(same_value(value, known) for known in seen):
                    seen.append(value)
        return seen

    def first(graph: ExecutableGraph, place: ListPlaceNode, filter: Filter) -> Optional[int]:
        """The index of the oldest token of ``place`` the filter keeps, or ``None``."""
        for index, token in enumerate(place.tokens):
            if filter.matches(graph, place, token):
                return index
        return None
