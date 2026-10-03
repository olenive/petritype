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
"""

from dataclasses import dataclass
from typing import Union


type PlaceTokenId = tuple[str, int]


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
    """``SetParam`` set keyword argument ``param`` of ``transition``."""

    transition: str
    param: str


@dataclass(frozen=True)
class Resynced:
    """A place's token list no longer matched its id list (something assigned or
    resized ``place.tokens`` directly), so every token in it got a fresh id. The
    events cannot describe what happened there; refetch the place."""

    place: str


Event = Union[Fired, Failed, Added, Removed, Mutated, Toggled, ParamChanged, Resynced]
