"""Event records an ``ExecutableGraph`` appends while ``record_events`` is on.

Passive, frozen dataclasses: the engine and the runtime construct them, a caller
reads them back with ``ExecutableGraph.drain_events()``. They are the Python
counterpart of the Rust crate's ``Event`` and map one to one onto the protocol's
state notifications (PROTOCOL.md §7.1), except ``Resynced``, which becomes
``reloaded {reason: "resync"}`` (§7.2).

Tokens are named by ``(place_name, token_id)`` pairs, where ``token_id`` is the
stable integer id the graph gave the token when it entered a place (see
``ExecutableGraph.next_token_id``).

A firing is reported once, when it completes: any ``Mutated`` records for the
tokens it touched through mutate edges come first, then exactly one ``Fired`` or
``Failed``. Python emits no launched/distributed pair.

An event inside an inner net (a net held by a token of a ``nested`` place, see
``NestedNets``) reaches the outer graph's log wrapped in a ``Nested`` record
carrying the path to the net it happened in and that net's stable id, so a
reader that knows nothing of nesting sees only records it already knows.

A path is a tuple of ``PathStep`` values, one per level, never text: no
character in a name means anything. ``InnerFiring`` is how a deep step tells a
firing inside an inner net, by that path and the transition's own name. These two
are named tuples rather than dataclasses because they are dictionary keys inside
pydantic models (``ExecutableGraph.fired_counts``, ``RunSummary.fired``), and a
named tuple is what pydantic dumps to a key that stays hashable.
"""

from dataclasses import dataclass
from typing import NamedTuple, Optional, Union


type PlaceTokenId = tuple[str, int]


class PathStep(NamedTuple):
    """One level of the way down to an inner net: ``place``, the nested place
    of the net above that holds the token; ``token``, that token's id; ``net``,
    the net's name within the token (``""`` for a token that is itself a net).
    A path is a tuple of steps, outermost first, so the cabin of token 17 in
    ``Depot`` is ``(PathStep("Depot", 17, "cabin"),)``. Names are taken whole,
    whatever characters they hold. The protocol runner's ``wire.PathStep`` is
    the same step as the wire spells it, the token id as a string."""

    place: str
    token: int
    net: str


class InnerFiring(NamedTuple):
    """A firing inside an inner net, as a deep step tells it to the net it was
    taken on: ``path`` from that net down to the net the transition fired in,
    and ``transition``, the transition's own name there. So
    ``InnerFiring((PathStep("Depot", 1, "cabin"),), "Sit")`` is the bus's
    cabin seating a passenger. It is what ``fire_one_transition`` with
    ``descend`` returns for such a firing, and the key under which
    ``fired_counts`` and ``RunSummary.fired`` count it. A firing of the net's
    own transition is told by the transition's name alone."""

    path: tuple[PathStep, ...]
    transition: str


@dataclass(frozen=True)
class Fired:
    """A transition fired: ``consumed`` left their places, ``produced`` arrived.

    ``consumed`` is in the order the tokens were taken (argument-edge order, FIFO
    within a place); ``produced`` in the order they were deposited.
    ``duration_ms`` is the wall time of the body (including output routing).
    """

    transition: str
    consumed: tuple[PlaceTokenId, ...]
    produced: tuple[PlaceTokenId, ...]
    duration_ms: float


@dataclass(frozen=True)
class Failed:
    """A transition's body (or its output routing or deposit) raised after its
    inputs were consumed. ``restored`` says whether the consumed tokens went back,
    with the same ids, to the front of their places. ``error`` names the original
    exception (``"TypeName: message"``)."""

    transition: str
    consumed: tuple[PlaceTokenId, ...]
    restored: bool
    error: str


@dataclass(frozen=True)
class Added:
    """A token arrived from outside a firing (``Extend``, ``SetTokens``, or a
    deposit that a failed firing left behind)."""

    place: str
    token_id: int


@dataclass(frozen=True)
class Removed:
    """A token was removed from outside a firing (``SetTokens``)."""

    place: str
    token_id: int


@dataclass(frozen=True)
class Mutated:
    """A token was handed to a body through a mutate edge and may have changed in
    place. Its id is unchanged."""

    place: str
    token_id: int


@dataclass(frozen=True)
class Toggled:
    """The ``Enable``/``Disable`` switch of a transition changed."""

    transition: str
    enabled: bool


@dataclass(frozen=True)
class ParamChanged:
    """``SetParam`` set keyword argument ``param`` of ``transition``, or, with
    ``transition`` ``None``, ``NetParams.set`` set the net parameter ``param``
    (``ExecutableGraph.params``)."""

    transition: Optional[str]
    param: str


@dataclass(frozen=True)
class Resynced:
    """A place's token list no longer matched its id list (something assigned or
    resized ``place.tokens`` directly), so every token in it got a fresh id. The
    events cannot describe what happened there; refetch the place."""

    place: str


@dataclass(frozen=True)
class Nested:
    """``event`` happened inside an inner net. ``path`` leads to that net from the
    graph this record was drained from, one ``PathStep`` per level, so
    ``(PathStep("Depot", 17, "cabin"),)`` for the cabin of token 17 standing in
    Depot, and two steps for a net one level further down. The path is as it
    stood when the event was recorded (the place the token was in at the time);
    ``nested_id`` is the inner net's ``net_id``, which does not change when the
    token moves. One wrapper whatever the depth: ``event`` is never itself a
    ``Nested``."""

    path: tuple[PathStep, ...]
    nested_id: int
    event: "Event"


Event = Union[Fired, Failed, Added, Removed, Mutated, Toggled, ParamChanged, Resynced, Nested]
