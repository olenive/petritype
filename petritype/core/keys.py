"""Token keys, filters and queries (``TOKEN_QUERYING_AND_FILTERING.md``, R1 and R2).

A place may declare *keys*: named attributes derived from each token's value by
an extractor the net author gives, ``ListPlaceNode(..., keys={"species": lambda
s: s.name, "class": lambda s: s.class_})`` or, for attribute names, ``keys=
("name", "diet")``. A key value is a string, an integer or a boolean, nothing
else (``KeyValue``); ``1`` and ``True`` are different values. Keys are derived
from the value, so a token mutated in place has new keys the next time they are
read; there is no index to keep in step (Python's tokens are plain objects in
plain lists, and a query over a net of this size costs a scan).

A ``Filter`` is also what a *keyed edge* carries (R5): ``TakeWhereEdge(place,
transition, argument, where=Filter.where(class_="Bird"))`` consumes the oldest
matching token rather than the front one, and ``MutateWhereEdge`` hands the
oldest matching token to the body live; every other token stays where it is,
with its id. Since 2026-11-23 ``ReadWhereEdge`` hands it over live to look at,
told by no event. All three are re-exported here from the engine module. Since
the same day a keyed edge's filter may also hold a clause that reads a key of
the token the same firing gets from another input, ``Clause("name",
input="Orders", input_key="courier")``: the courier the order names.

A ``Filter`` is what a query, a viewer and a projection (PROTOCOL.md §13) share:
clauses ANDed together, each accepting one of a set of values for a key (OR
within the clause) or the current value of a net parameter; and a restriction to
some places, by name or by name prefix. ``Query.matches`` returns the matching
tokens in place order then token order, each with its place, id, keys and value;
``Query.distinct`` the distinct values of a key in order of first appearance.

A query on a net with subnet places covers their ports too, each at its subnet
place's position, named by its ``Port`` pair; a filter's ``places`` names a port
by that pair, and its ``prefix`` is tested against the port's own name. A port's
keys are the ones its subnet's definition declares, and a port's token ids are
its subnet's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from petritype.core.events import PlaceKey, Port
from petritype.core.executable_graph_components import (
    ExecutableGraph, ListPlaceNode, MapPlaceNames, MutateWhereEdge, ReadWhereEdge, TakeWhereEdge, TokenIds,
)

__all__ = [
    "KeyValue", "Keys", "Clause", "Filter", "Match", "Query", "TakeWhereEdge", "MutateWhereEdge", "ReadWhereEdge",
    "is_key_value", "same_value",
]

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
    ``values``; or, with ``param`` instead, equals the net parameter so named as
    it stands when the filter is applied; or, with ``input`` and ``input_key``
    instead (since 2026-11-23), equals the key ``input_key`` of the token this
    firing gets from the place ``input``, named as an edge names a place (a
    place's name, or a ``Port`` pair). Exactly one of ``values``, ``param`` and
    ``input`` is given, and ``input_key`` goes with ``input``.

    A clause with ``input`` belongs in the filter of a keyed edge
    (``TakeWhereEdge``, ``MutateWhereEdge``, ``ReadWhereEdge``), which is then a *bound edge*: it
    chooses its token by the token its transition gets from the input, the
    edge's *source*. ``MutateWhereEdge("Couriers", "Load", "courier",
    where=Filter((Clause("name", input="Orders", input_key="courier"),)))``
    hands the body the courier the order names. Outside a firing there is no
    such token, so the clause accepts nothing: ``accepts``, ``Filter.keeps``,
    ``Query.matches`` and ``Query.first`` keep no token through it. Which pair a
    firing takes, and what the net may declare, is ``KeyedEdges`` in the engine."""

    key: str
    values: tuple[KeyValue, ...] = ()
    param: Optional[str] = None
    input: Optional[PlaceKey] = None
    input_key: Optional[str] = None

    def __post_init__(self):
        given = sum((bool(self.values), self.param is not None, self.input is not None))
        if given != 1:
            raise ValueError("a clause has exactly one of values, a param and an input")
        if (self.input is None) != (self.input_key is None):
            raise ValueError("a clause's input_key goes with its input, and its input with an input_key")
        if not all(is_key_value(value) for value in self.values):
            raise TypeError("a clause's values are strings, integers or booleans")
        if self.input is not None:
            if not isinstance(self.input, (str, Port)):
                raise TypeError("a clause's input is a place's name or a Port pair")
            if not isinstance(self.input_key, str):
                raise TypeError("a clause's input_key is a key's name")

    def accepts(self, keys: Mapping[str, KeyValue], params: Mapping[str, Any]) -> bool:
        """Whether a token with these keys passes, with ``params`` the net's
        parameters. A clause with ``input`` accepts nothing here: only a firing
        has the token it reads."""
        if self.input is not None:
            return False
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
    No clauses and no restriction keeps everything. ``places`` names a port by
    its ``Port`` pair; a subnet place named alone selects nothing, since it
    holds no tokens."""

    clauses: tuple[Clause, ...] = ()
    places: Optional[tuple[PlaceKey, ...]] = None
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

    def in_places(self, *places: PlaceKey) -> "Filter":
        return Filter(self.clauses, tuple(places), self.prefix)

    def with_prefix(self, prefix: str) -> "Filter":
        return Filter(self.clauses, self.places, prefix)

    def covers(self, place_name: PlaceKey) -> bool:
        """Whether tokens of this place are considered at all: a place by its
        name, or a port by its pair, whose own name the prefix is tested against."""
        if self.places is not None and place_name not in self.places:
            return False
        own_name = place_name.port if isinstance(place_name, Port) else place_name
        if self.prefix is not None and not own_name.startswith(self.prefix):
            return False
        return True

    def accepts(self, keys: Mapping[str, KeyValue], params: Mapping[str, Any]) -> bool:
        return all(clause.accepts(keys, params) for clause in self.clauses)

    def keeps(
        self, place: ListPlaceNode, token: Any, params: Mapping[str, Any], key: Optional[PlaceKey] = None,
    ) -> bool:
        """Whether ``token``, resting in ``place``, passes the filter, with
        ``params`` the net's parameters for a clause that names one. ``key`` is
        how the net the filter belongs to names the place, when that is not the
        place's own name: a port of one of its subnet places, by its ``Port``
        pair, which a ``places`` restriction names it by (PROTOCOL.md §13.2)."""
        if not self.covers(place.name if key is None else key):
            return False
        if not self.clauses:
            return True
        if not place.keys:
            return False
        return self.accepts(Keys.of(place, token), params)

    def matches(self, graph: ExecutableGraph, place: ListPlaceNode, token: Any, key: Optional[PlaceKey] = None) -> bool:
        """``keeps`` with the graph's own parameters."""
        return self.keeps(place, token, graph.params, key)


EVERYTHING = Filter()


# --- queries ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    """One token a query found: where it rests (a place's name, or a port's
    ``Port`` pair), its id, its keys and the value."""

    place: PlaceKey
    token_id: int
    keys: dict[str, KeyValue] = field(compare=False)
    token: Any = field(compare=False)


class Query:
    """Functions that ask a net for tokens by their keys."""

    def matches(graph: ExecutableGraph, filter: Filter = EVERYTHING) -> list[Match]:
        """Every token the filter keeps, in place order then token order, the
        ports of a subnet place at its position (``MapPlaceNames``)."""
        TokenIds.resync(graph)
        found: list[Match] = []
        for key, place in MapPlaceNames.to_list_place_nodes(graph).items():
            if not filter.covers(key):
                continue
            if filter.clauses and not place.keys:
                continue
            ids = place.token_ids
            for index, token in enumerate(place.tokens):
                keys = Keys.of(place, token)
                if filter.accepts(keys, graph.params):
                    found.append(Match(key, ids[index] if index < len(ids) else -1, keys, token))
        return found

    def count(graph: ExecutableGraph, place: ListPlaceNode, filter: Filter) -> int:
        """How many tokens of ``place`` the filter keeps."""
        return sum(1 for token in place.tokens if filter.matches(graph, place, token))

    def distinct(graph: ExecutableGraph, key: str, places: Optional[Sequence[PlaceKey]] = None) -> list[KeyValue]:
        """The distinct values of ``key`` over the net (or ``places``, a port by
        its pair), in order of first appearance in place order then token order,
        ports included; ``[]`` for a key no place declares."""
        seen: list[KeyValue] = []
        for place_key, place in MapPlaceNames.to_list_place_nodes(graph).items():
            if places is not None and place_key not in places:
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
