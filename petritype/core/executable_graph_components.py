from contextlib import contextmanager
import contextvars
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TypeAliasType
from types import UnionType
from typing import Awaitable, Callable, ClassVar, Iterable, Iterator, Literal, NoReturn, Optional, Sequence, Union, Any, get_type_hints, get_origin, get_args
from pydantic import BaseModel, Field, PrivateAttr, model_serializer, model_validator
import asyncio
import functools
import inspect
import itertools
import time
import warnings
import weakref

from petritype.core.data_structures import ArgumentName, FunctionName, KwArgs, PlaceNodeName, ReturnIndex
from petritype.core.events import (
    Added, Event, Failed, Fired, InnerFiring, Mutated, Nested, ParamChanged, PathStep, PlaceKey, PlaceTokenId, Port,
    Removed, Resynced,
)
from petritype.core.type_comparisons import CompareTypes
from petritype.helpers.structures import SafeMerge


type TransitionName = str

# Distinguishes "argument not passed" from an explicit None (which means unlimited)
# while `max_transitions` remains a deprecated alias of `stop_after_n_firings`.
_UNSET: Any = object()

# The protocol method a token type implements to hold inner nets (``NestedNets``).
NESTED_NETS_METHOD = "__petritype_nets__"

# Every graph built in this process gets the next id: the stable identity of an
# inner net across the moves of the token that holds it (``ExecutableGraph.net_id``).
_net_ids = itertools.count(1)

# How the nets in a place take their turns beside the net around them, one rule per
# place that holds nets (``ListPlaceNode.stepping``, ``NestedNets``): the nets fire
# until they are at rest before the net around them (``"inner_first"``, the default),
# in turn with it (``"take_turns"``), only once it and its turn-takers have nothing to
# fire (``"outer_first"``), or only when a body or the program steps them (``"passive"``).
# The names are the protocol's (PROTOCOL.md §12.3).
Stepping = Literal["inner_first", "take_turns", "outer_first", "passive"]

# Closed nets (since 2026-11-23, ``ClosedNets``): the rule is in force on a net that marks
# a place as a port, and such a net is closed unless it declares itself open. Stage two of
# the rule turns this default to True, so that every net is under the rule and every net
# that does not declare itself open is closed. Read by ``ClosedNets.under_the_rule`` alone.
CLOSED_WITHOUT_PORTS = False

# What every refusal of a write at a private place says, so that it says how to put it right.
_PUT_IT_RIGHT = "mark the place as a port, start the net with the token in its definition, or declare the net open"

# Identifies the firing currently executing a transition body (propagated across
# async awaits and thread-pool offload) so ``ClosedNets.note`` scopes refusals to
# the offending body rather than tainting concurrent peers holding the same net.
_ACTIVE_FIRING_IDS: contextvars.ContextVar[Optional["FiringTokenIds"]] = contextvars.ContextVar(
    "_petritype_active_firing_ids", default=None,
)

_TYPE_HINTS_CACHE: "weakref.WeakKeyDictionary[Any, tuple[Any, dict[str, Any]]]" = weakref.WeakKeyDictionary()


def _function_type_hints(fn: Any) -> dict[str, Any]:
    """Cached ``get_type_hints(fn)`` with safe fallback to ``__annotations__``.

    Keyed weakly by ``fn`` and validated against ``__annotations__`` identity so
    callables can be garbage-collected and rare runtime annotation mutations are
    never stale.
    """
    annotations = getattr(fn, "__annotations__", None)
    try:
        cached = _TYPE_HINTS_CACHE.get(fn)
    except TypeError:
        cached = None
    if cached is not None and cached[0] is annotations:
        return cached[1]
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = annotations if isinstance(annotations, dict) else {}
    try:
        _TYPE_HINTS_CACHE[fn] = (annotations, hints)
    except TypeError:
        pass
    return hints


class PetritypeError(Exception):
    """Base class for all Petritype library errors.

    Specific error subclasses also inherit from their standard Python exception
    type (``ValueError`` or ``RuntimeError``) for backwards compatibility.
    """


class ClosedNetError(PetritypeError, ValueError):
    """A write from outside a closed net at a place that is not one of its ports
    (``ClosedNets``, since 2026-11-23): putting, taking or replacing tokens there
    through the library's own functions (``NestedNets.put``, ``NestedNets.take``),
    the runtime's ``Extend`` and ``SetTokens``, or the runner's ``inject``; a write
    at the port of one of its subnet places, which is never one of its own ports;
    or, found when a body returns, a body's own edit of such a place's list.

    Attributes:
        net: The net, by the template that built it, or ``#`` and its ``net_id``
            when no template did.
        place: The place, as the write named it (a ``Port`` pair for a port of a
            subnet place).
        act: What was refused, in a few words (``"a put"``, ``"an inject"``).
    """

    def __init__(self, net: str, place: Any, act: str):
        self.net = net
        self.place = place
        self.act = act
        super().__init__(
            f"{act} at {_place_words(place)} of net {net!r} is refused: the net is closed, so from outside it "
            f"only its ports may be written. To make it land, {_PUT_IT_RIGHT}."
        )


class HeldByBodyError(PetritypeError, ValueError):
    """A body of the net around a net asked to work one of its knobs, or to fire
    one of its transitions by name, while it held the net (``ClosedNets``, since
    2026-11-23): setting a parameter (``NetParams.set``, ``NestedNets.set_param``),
    the runtime's ``SetNetParam``, ``SetParam``, ``Enable``, ``Disable`` and
    ``Fire``, a selector of its own for a firing, or, found when the body returns,
    a change it made to them directly. Refused only on a net that is under the rule
    (a net that marks a port, open or closed); a net that marks none keeps its
    knobs for a body, as before. The program, its drivers and the runner keep them
    on every net.

    Attributes:
        net: The net, as for ``ClosedNetError``.
        act: What was refused, in a few words (``"setting parameter 'speed'"``).
    """

    def __init__(self, net: str, act: str):
        self.net = net
        self.act = act
        super().__init__(
            f"{act} on net {net!r} is refused: a body of the net around it holds the net, and its parameters, "
            f"switches, tie-break, stepping rules, selector and firing by name stay with the program and the "
            f"runner. Make the change from the program or the runner, or give the net a transition of its own "
            f"that makes it."
        )


class TransitionFailedError(PetritypeError, RuntimeError):
    """A transition body (or its output routing) raised after its input tokens
    had been consumed.

    Attributes:
        transition_name: Name of the transition whose firing failed.
        consumed: The consumed tokens, keyed by argument name. Recover them
            from here — by default they are NOT put back into their places,
            because the body may have mutated them before failing and a place
            should never silently hold corrupted tokens.
        restored: True if restore_tokens_on_failure was enabled and the tokens
            were put back (best effort) into their source places.

    The original exception is chained as __cause__.

    Restoring is conservation of the consumed tokens, not a rollback of the
    firing: what the body did before failing stays done. In particular a body
    that reached inside a nested token (``NestedNets``) and put tokens into, or
    took them out of, an inner net leaves the inner net as it changed it; only
    the outer tokens go back. A body that reaches inside should do so last, once
    it can no longer fail.
    """

    def __init__(self, transition_name: str, consumed: dict, restored: bool = False):
        self.transition_name = transition_name
        self.consumed = consumed
        self.restored = restored
        tokens_note = (
            "consumed tokens were restored to their places (best effort)"
            if restored
            else "consumed tokens were NOT restored; recover them from this error's .consumed"
        )
        super().__init__(
            f"Firing of transition '{transition_name}' failed; {tokens_note}. "
            f"Consumed arguments: {sorted(consumed)}."
        )


class LaunchNotificationError(PetritypeError, RuntimeError):
    """An observer raised at a sequential firing's launch notification, after the
    inputs were consumed and before the body ran (``on_launch`` in
    ``ExecutableGraphOperations.fire_one_transition``, where the Runner notifies its
    observers).

    The firing fails as a raising body does, so this error is the ``__cause__`` of the
    ``TransitionFailedError`` that follows, the ``Failed`` record's ``error`` names it,
    and the observer's own exception is this error's ``__cause__``.

    Attributes:
        transition_name: Name of the transition that was launching.
        observer_error: The exception the observer raised.
    """

    def __init__(self, transition_name: str, observer_error: Exception):
        self.transition_name = transition_name
        self.observer_error = observer_error
        super().__init__(
            f"an observer raised at the launch notification of {transition_name!r}, "
            f"before its body ran: {observer_error!r}"
        )


class SubnetHeldError(PetritypeError, RuntimeError):
    """The program asked to step or change a subnet (``NestedNets.subnet_scope``)
    while a firing of the net around it on one of its ports is in flight: the
    subnet is held still until that firing lands.

    Attributes:
        place: The subnet place.
        transition_name: The first transition in flight, in declaration order,
            with an edge on one of the subnet's ports.
    """

    def __init__(self, place: str, transition_name: str):
        self.place = place
        self.transition_name = transition_name
        super().__init__(
            f"the subnet of {place!r} is held still while {transition_name!r}, which has an edge on one of its "
            f"ports, is in flight"
        )


class PositionalArgsBaseModel(BaseModel):
    """Base class that enables positional arguments for Pydantic models. A field
    named in ``_keyword_only`` is given by keyword alone: positional arguments
    fill the other fields in order, so adding such a field to a class moves no
    positional argument of its subclasses."""

    _keyword_only: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, *args, **kwargs):
        field_names = [name for name in self.__class__.model_fields if name not in self._keyword_only]
        if len(args) > len(field_names):
            raise TypeError(
                f"{self.__class__.__name__}() takes at most {len(field_names)} positional arguments "
                f"({len(args)} given)"
            )
        # Map positional arguments to field names
        for field_name, arg in zip(field_names, args):
            if field_name in kwargs:
                raise TypeError(
                    f"{self.__class__.__name__}() got multiple values for argument {field_name!r}"
                )
            kwargs[field_name] = arg

        super().__init__(**kwargs)

    model_config = {
        "extra": "forbid"
    }


class ListPlaceNode(PositionalArgsBaseModel):
    """A place: a typed, FIFO list of tokens.

    ``nested`` declares that its tokens hold inner nets (``NestedNets``): every
    token must be an ``ExecutableGraph`` or implement ``__petritype_nets__``, which
    is checked when tokens are seeded, deposited or injected. The engine then runs
    the inner nets as part of running the outer one, in the order the place's
    ``stepping`` rule gives (``Stepping``): ``"inner_first"`` (the default)
    before the outer net's own transitions, ``"take_turns"`` in turn with them,
    ``"outer_first"`` once the outer net has nothing to fire, and ``"passive"``
    never, so that only an outer body or the program steps them. A place that
    holds no nets has no rule: its ``stepping`` is ``None``, and giving it one is
    an error. The rule is part of the net's definition, not saved with it; it
    may be changed by assigning ``stepping``, from the next deep step on.

    ``keys`` declares the keys the place derives from each token
    (``petritype.core.keys``): a dict of key name to extractor, or a sequence of
    attribute names (each read with ``getattr``). Given as a sequence it is
    stored as the dict. Key values are strings, integers or booleans; what a
    query filters on and what the wire carries as a token's ``keys``.

    ``port`` marks the place as a port of its net: when the net is the subnet of
    a subnet place (``SubnetPlaceNode``), the net around it reaches this place,
    and no other, by the pair ``Port(subnet place, name)``. The place stays the
    net's own, with its one list of tokens and its ids, minted by this net,
    whichever net looks at it. A port is an ordinary place or a nested one; its
    keys and its stepping rule are this net's to declare.

    ``group`` names the group the place is in, or is ``None`` for none
    (``ExecutableGraph.groups``): a label on parts of a net that only drawings
    read. It changes nothing about how the net runs, and nothing about the
    place's own name. A port's group is a group of its own net, the subnet.
    """
    name: PlaceNodeName
    type: Any  # Temporarily accept any value
    tokens: list[Any] = Field(default_factory=list)
    nested: bool = False
    stepping: Optional[Stepping] = None
    keys: Optional[dict[str, Callable[[Any], Any]]] = None
    port: bool = False
    group: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _keys_as_extractors(cls, values):
        """A sequence of attribute names becomes a dict of ``getattr`` extractors."""
        if isinstance(values, dict) and values.get("keys") is not None and not isinstance(values["keys"], dict):
            names = values["keys"]
            if isinstance(names, str) or not all(isinstance(name, str) for name in names):
                raise TypeError("keys is a dict of name to extractor, or a sequence of attribute names")
            values = {**values, "keys": {name: _attribute(name) for name in names}}
        return values
    # Stable ids of the tokens, parallel to ``tokens`` (same length, same order). Assigned
    # by the graph that holds the place (``TokenIds``); empty until then. Private so that
    # ``tokens`` keeps its type and every existing reader keeps working.
    _token_ids: list[int] = PrivateAttr(default_factory=list)

    @property
    def token_ids(self) -> tuple[int, ...]:
        """Read-only view of the ids of ``tokens``, index for index.

        Only meaningful once the place belongs to an ``ExecutableGraph``. If code
        assigned ``tokens`` directly the two can disagree until the graph's next
        engine entry point resyncs them (``TokenIds.resync``)."""
        return tuple(self._token_ids)

    @model_validator(mode="after")
    def validate_type_field(self):
        type_of_value = self.type

        if type_of_value is not None:
            # A valid place type is a plain class, any subscripted generic or union (both the
            # typing.List[int] and builtin list[int] spellings, and both the Union[int, str] and
            # int | str spellings, all of which have a non-None origin), or a PEP 695 alias (a
            # bare alias has no origin, so it needs its own check). get_origin replaces earlier
            # isinstance checks against typing's private _GenericAlias / _UnionGenericAlias, which
            # are deprecated and removed in Python 3.17.
            if not (
                isinstance(type_of_value, type)
                or get_origin(type_of_value) is not None
                or isinstance(type_of_value, TypeAliasType)
            ):
                raise ValueError(
                    f"Expected type to be a class, a subscripted generic/union, or a type alias "
                    f"but got: {type_of_value} (type: {type(type_of_value)})"
                )
        else:
            raise NotImplementedError(
                "Unclear at the time of writing what it means for a place node to have no type."
            )

        return self

    @model_validator(mode="after")
    def check_type_matches_tokens(self):
        if self.stepping is not None and not self.nested:
            raise ValueError(
                f"Place {self.name!r} holds no nets, so it has no stepping rule; stepping applies to a nested place."
            )
        if self.nested and self.stepping is None:
            self.stepping = "inner_first"
        for token in self.tokens:
            if not CompareTypes.between_value_and_type(token, self.type):
                raise TypeError(
                    f"Expected token to be of type {self.type} in {self.name}, got {type(token)}."
                    f"\nToken: {token}"
                )
            if self.nested:
                NestedNets.ensure_nested_token(token, self)
        return self

    def copy_sans_tokens(self) -> "ListPlaceNode":
        return ListPlaceNode(
            self.name, self.type, nested=self.nested, stepping=self.stepping, keys=self.keys, port=self.port,
            group=self.group,
        )

    @property
    def key_names(self) -> tuple[str, ...]:
        return tuple(self.keys) if self.keys else ()


def _attribute(name: str) -> Callable[[Any], Any]:
    def extract(token: Any) -> Any:
        return getattr(token, name)
    extract.__name__ = f"attribute_{name}"
    return extract


class FunctionTransitionNode(PositionalArgsBaseModel):
    """A transition node that executes a function when fired.

    Attributes:
        name: Unique identifier for the transition
        function: The function to execute when this transition fires
        output_distribution_function: Optional function to distribute outputs to places
        kwargs: Optional keyword arguments to pass to the function
        guard: Optional (ExecutableGraph) -> bool firing condition, enforced by the
            engine in every execution mode: a transition whose guard returns False is
            not fireable, regardless of available tokens or selector. The guard runs on
            every fireable-discovery sweep, so keep it a cheap, side-effect-free
            predicate over the marking (None costs nothing — the check is skipped).
        priority: Optional (ExecutableGraph) -> float selection hint. The default
            selector fires the highest-priority fireable transition; transitions
            without one score 0.0, and ties go to the first-defined transition (or
            the last-defined, under the graph's ``tie_break="last_declared"``, or
            the next in turn, under ``"round_robin"``). Only called on fireable
            transitions at selection time.
        activation_function: Deprecated — use ``guard`` (engine-enforced firing condition) or
            ``priority`` (selection ranking) instead. The engine never consults this
            field; it is only visible to custom transition selectors, and it will be
            removed in a future release.
        group: The name of the group the transition is in, or None for none
            (``ExecutableGraph.groups``). Only drawings read it: it changes nothing
            about firing, selection or the transition's own name.
    """
    name: str
    function: Callable
    output_distribution_function: Optional[Callable[[Any], dict[PlaceNodeName, Any]]] = None
    kwargs: Optional[KwArgs] = None
    guard: Optional[Callable] = None
    priority: Optional[Callable] = None
    activation_function: Optional[Callable] = None
    # How a *synchronous* body runs: "inline" (default, on the event loop) or "thread"
    # (offloaded to an executor so a blocking / CPU-bound body doesn't freeze the loop). Async
    # bodies already yield and ignore this. Process-level parallelism is the function's own
    # concern (bring your own process pool inside the body) — the engine never pickles a token.
    execution: Literal["inline", "thread"] = "inline"
    group: Optional[str] = None

    @model_validator(mode="after")
    def _warn_if_activation_function(self) -> "FunctionTransitionNode":
        if self.activation_function is not None:
            warnings.warn(
                "activation_function is deprecated: use `guard` for an engine-enforced "
                "firing condition or `priority` for selection ranking. The engine ignores "
                "activation_function; it is only visible to custom transition selectors.",
                DeprecationWarning,
                stacklevel=2,
            )
        return self


class ArgumentEdgeToTransition(PositionalArgsBaseModel):
    """A consuming edge (place -> transition). Every edge names its place by a
    ``PlaceKey``: a place of the net by its name, or a port of one of the net's
    subnet places by the pair, ``Port("Level 3", "Lobby")``, which every edge
    kind accepts where it would take a name.

    ``reaches`` (since 2026-11-23, given by keyword alone, on every input edge
    kind) names the ports the body reaches in the nets the place's tokens carry,
    in the order given: ``MutateEdge("Field", "Hand Over", "unit",
    reaches=("Orders In",))``. It is a declaration for readers and drawings, as a
    group is, and changes nothing about firing. The build refuses it on a place
    that holds no nets and refuses an empty name
    (``ExecutableGraphCheck.ensure_reaches_are_sound``); whether a token's net
    has the ports named is not checked, since nets arrive in tokens as the net
    runs. An output edge has no ``reaches``."""
    _keyword_only: ClassVar[frozenset[str]] = frozenset({"reaches"})
    place_node_name: PlaceKey
    transition_node_name: FunctionName
    argument: ArgumentName
    reaches: tuple[str, ...] = ()


class ReturnedEdgeFromTransition(PositionalArgsBaseModel):
    """A producing edge (transition -> place), its place a name or a ``Port``."""
    transition_node_name: FunctionName
    place_node_name: PlaceKey
    return_index: Optional[ReturnIndex] = None


class TakeWhereEdge(ArgumentEdgeToTransition):
    """A consuming edge that takes the oldest token *matching a filter* rather
    than the front token (``petritype.core.keys``, keyed edges). The transition
    is fireable when the place holds a matching token; its firing takes that
    token alone, leaving every other token where it is, with its id, so nothing
    is removed and re-added around it. ``where`` is a ``Filter`` (a ``{key,
    param}`` clause reads the net's parameters as they stand); the argument
    receives the one token, never a list."""
    where: Any

    @model_validator(mode="after")
    def _where_is_a_filter(self) -> "TakeWhereEdge":
        if not callable(getattr(self.where, "keeps", None)):
            raise TypeError("where must be a Filter (petritype.core.keys)")
        return self


class DrainEdge(ArgumentEdgeToTransition):
    """A consuming edge that takes *every* token of the place, oldest first, zero
    or more, and never holds the transition back: an empty place hands the body an
    empty list (a reset arc, PROTOCOL.md §5.1 and §11). The argument must be
    annotated ``list[T]`` for the place's ``T``, checked when the graph is built,
    since a plain ``T`` has nothing to receive from an empty place. Firing consumes
    what was there, as a ``list``-annotated ``ArgumentEdgeToTransition`` does on a
    non-empty place, and a failed firing restores it to the front with its ids. A
    drain may consume nothing, so ``assert_no_source_transitions`` and
    ``assert_acyclic`` do not count it as moving a token."""


class SnapshotEdge(PositionalArgsBaseModel):
    """A non-consuming edge (place -> transition). The transition receives a **deep-copy
    snapshot** of the place's tokens, so reading cannot disturb the place. Fireability requires
    the place to be non-empty (presence), but firing does not consume the tokens.

    A snapshot of a token that carries a net copies the net, and the copy is a new net with
    a new ``net_id``. To look at such a token without copying it, use a ``ReadEdge``.
    ``reaches``, by keyword alone, is as for ``ArgumentEdgeToTransition``."""
    _keyword_only: ClassVar[frozenset[str]] = frozenset({"reaches"})
    place_node_name: PlaceKey
    transition_node_name: FunctionName
    argument: ArgumentName
    reaches: tuple[str, ...] = ()


class SurveyEdge(SnapshotEdge):
    """A snapshot edge that never holds the transition back: the body receives a
    deep copy of every token of the place, oldest first, zero or more, and nothing
    is consumed (PROTOCOL.md §5.1 and §11). It differs from ``SnapshotEdge``
    only in not requiring presence: an empty place hands the body an empty list. The
    argument must be annotated ``list[T]`` for the place's ``T``, checked when the
    graph is built, as a drain's is."""


class MutateEdge(PositionalArgsBaseModel):
    """A non-consuming edge that reads and writes (place -> transition). The transition receives the
    **live** tokens and may modify them in place; they stay in the place (not consumed) but may
    be changed. Fireability requires the place to be non-empty (presence). ``reaches``, by
    keyword alone, is as for ``ArgumentEdgeToTransition``: on a place of net-carrying tokens,
    the ports the body reaches in the token's net."""
    _keyword_only: ClassVar[frozenset[str]] = frozenset({"reaches"})
    place_node_name: PlaceKey
    transition_node_name: FunctionName
    argument: ArgumentName
    reaches: tuple[str, ...] = ()


class MutateWhereEdge(MutateEdge):
    """A mutate edge that hands the body the oldest token *matching a filter*,
    live, rather than the front token (keyed edges); fireable when the place
    holds a matching token. The argument receives the one token."""
    where: Any

    @model_validator(mode="after")
    def _where_is_a_filter(self) -> "MutateWhereEdge":
        if not callable(getattr(self.where, "keeps", None)):
            raise TypeError("where must be a Filter (petritype.core.keys)")
        return self


class ReadEdge(PositionalArgsBaseModel):
    """A **read edge** (place -> transition, since 2026-11-23): the body is handed the front
    token of the place, live, to look at. Nothing is taken and nothing is copied, so the token
    keeps its place and its id, and no event is recorded for it. The transition is fireable
    only while the place holds a token. The argument receives one token: it is annotated as
    the place's type (or not at all), and ``ReadAllEdge`` is the edge for every token.

    The body must leave the token unchanged. That is the author's promise, which Python cannot
    enforce: a body that changes what it reads changes the token in its place, with no
    ``Mutated`` told. A token that carries a net is handed over as it is, so its net keeps its
    ``net_id``, where a ``SnapshotEdge`` would copy the net under a new one.

    The read edges (``ReadEdge``, ``ReadWhereEdge``, ``ReadAllEdge``) are three of the
    **non-consuming edges**, the edges that leave their token in its place, beside
    ``SnapshotEdge``, ``SurveyEdge``, ``MutateEdge`` and ``MutateWhereEdge``. A net keeps
    them in ``ExecutableGraph.read_edges``. A place a read edge reads is read by no other
    input edge of the same transition (``ExecutableGraphCheck.ensure_read_edges_stand_alone``).
    ``reaches``, by keyword alone, is as for ``ArgumentEdgeToTransition``.
    """
    _keyword_only: ClassVar[frozenset[str]] = frozenset({"reaches"})
    place_node_name: PlaceKey
    transition_node_name: FunctionName
    argument: ArgumentName
    reaches: tuple[str, ...] = ()


class ReadWhereEdge(ReadEdge):
    """A read edge that hands the body the oldest token *matching a filter*, live, rather
    than the front token (keyed edges, ``petritype.core.keys``); fireable while the place
    holds a matching token. Its filter may choose by another input of the same firing
    (a bound edge, ``KeyedEdges``). The argument receives the one token."""
    where: Any

    @model_validator(mode="after")
    def _where_is_a_filter(self) -> "ReadWhereEdge":
        if not callable(getattr(self.where, "keeps", None)):
            raise TypeError("where must be a Filter (petritype.core.keys)")
        return self


class ReadAllEdge(ReadEdge):
    """A read edge that hands the body every token of the place, live, oldest first, as a
    new list, possibly empty, and never holds the transition back. The list is the body's
    own; the tokens in it are the place's. The argument must be annotated ``list[T]`` for the
    place's ``T``, checked when the graph is built, as a survey's is."""


class ExecutableGraph(BaseModel):
    """A Petri net graph that can be executed.

    Attributes:
        places: Collection of place nodes that hold tokens
        transitions: Collection of transition nodes that transform tokens
        argument_edges: Edges from places to transitions (input)
        return_edges: Edges from transitions to places (output)
        snapshot_edges, mutate_edges, read_edges: The non-consuming edges, which leave
            their tokens in their places: snapshots and surveys, mutate and mutate-where
            edges, and the read edges (``ReadEdge``, ``ReadWhereEdge``, ``ReadAllEdge``,
            since 2026-11-23)
        step_count: Monotonic count of transitions that have fired since this
            graph was created. Increments by one every time ``execute_graph``
            successfully fires a transition. Use this as a stable sequence
            number when you need to identify "which step are we on" — e.g.
            for idempotency / compare-and-swap semantics over an unreliable
            transport, or for replay / fork-from-step features. Independent
            of ``transition_history``, which is capped for memory and is
            therefore unsuitable as an authoritative counter.
        last_fired: Name of the most recently fired transition, or None if the
            last firing attempt fired nothing; an ``InnerFiring`` when a deep step
            fired inside an inner net. ``execute_graph`` resets it per
            call; the sequential Runner resets it per attempt (same contract).
            The concurrent Runner loops also set it as they deposit results,
            where it names only *one* completion of each batch — observers
            that must see every firing should diff ``fired_counts`` instead
            (see ``petritype.runtime.fired_since``).
        fired_counts: Cumulative {transition_name: times_fired} since the graph
            was created. Monotonic and never trimmed (like ``step_count``), so
            it is the reliable way to ask "has transition X ever fired" — the
            capped ``transition_history`` cannot answer that. A firing inside an
            inner net, made by a deep step of this graph, is counted under its
            ``InnerFiring``, and in ``step_count`` too.
        transition_history: History of fired transitions
        input_place_history: History of input place states
        output_place_history: History of output place states
        token_history: History of tokens
        transition_selector: Optional function to select which transition fires next.
            Signature: (graph, fireable_transitions) -> transition_to_fire
            If None, the default selector fires the highest-priority fireable
            transition, breaking ties by ``tie_break``. The selector receives the
            full graph context and list of fireable transitions (in definition
            order), allowing for sophisticated selection strategies (priority-based,
            random, etc.). It is called positionally, so existing selectors whose
            second parameter is named ``enabled`` keep working. Petri net texts say
            "enabled" for what this library calls "fireable"; "enabled" here is the
            on/off switch set by the runtime's ``Enable`` / ``Disable`` commands.
        tie_break: Which fireable transition the default selector fires when several
            share the highest priority (including nets that set no priorities).
            ``"first_declared"`` (default) prefers the first-defined one;
            ``"last_declared"`` prefers the last-defined one; ``"round_robin"``
            (since 2026-10-12) prefers the first one declared *after* the
            transition the default selector chose last in this net (its cursor),
            wrapping to the first when none follows, and the first declared when
            there is no cursor yet (a fresh net, or one loaded from its saved form:
            the cursor is kept in memory only, never saved). With FIFO places the
            three give different schedules for a pipeline: first-declared processes
            it in batches (every token through stage one before any reaches stage
            two), last-declared lets each token flow through before the next
            enters, and round robin alternates the stages. Neither is more correct,
            so it is a setting, and the default keeps the original behaviour. Only
            the default selector reads it. The default selector moves the cursor on
            every pick, whatever the tie-break, though only round robin reads it, so
            a tie-break switched to round robin mid-run resumes after the last pick;
            a custom ``transition_selector`` neither reads nor moves it. Each net,
            inner nets included, has its own setting and its own cursor.
        next_token_id: The id the next token to enter a place will get. Every token
            gets a stable integer id when it enters a place (seed tokens at
            construction, in place order, from 1; then deposits, ``Extend`` and
            ``SetTokens``). Ids are never reused: a token keeps its id while it rests
            in a place, a consumed token's id is retired, and a token restored after a
            failed firing comes back with its old id. Read them with
            ``place.token_ids``; find one with ``TokenIds.locate``.
        record_events: When True the engine and the runtime append event records
            (``petritype.core.events``) to the graph, read with ``drain_events()``.
            Off by default, so a program that never drains does not grow a list
            for ever. Id bookkeeping happens either way. Inner nets (``NestedNets``)
            follow the outer graph's setting, and their events are drained into
            the outer log wrapped as ``Nested`` records.
        params: Net-level parameters by name, declared at construction (through
            ``construct_graph(params=...)``) and read by guards, priorities and
            selectors through the graph. A body receives a net parameter as a
            keyword argument when it declares a parameter of that name that no
            edge and no ``kwargs`` entry supplies. Set them with ``NetParams.set``,
            which records ``ParamChanged``; the runtime's ``SetNetParam`` command
            and the protocol's ``set_param`` without a transition go through it.
            The inner nets of nested tokens use them for what the outer net
            reaches in to change (a stopped flag, a capacity).
        net_id: This graph's own id, minted from a counter shared by every graph
            built in the process. It is what names an inner net stably across the
            moves of the token that holds it (``Nested.nested_id``): an outer token
            that is taken and put back gets a new token id, the net inside it keeps
            its ``net_id``. A copy of a graph shares its id until re-minted.
        template: The name of the template (``Templates``) that built this graph,
            if one did; what a saved nested token records so the net can be rebuilt.
        disabled: The graph's own transition switches: a transition named here is
            not fireable. Loaded from and saved as the ``enabled`` map of the net's
            JSON form, and the switch an inner net has (an inner net runs without a
            ``RunContext``). The runtime's ``Enable``/``Disable`` commands are the
            runner's switch on the net it drives, kept on ``RunContext.disabled``.
        open: Whether the net declares itself open (since 2026-11-23), given in its
            definition (``construct_graph(open=True)``). A net that marks a place as a
            port is **closed** unless it declares itself open (``is_closed``): from
            outside it, tokens are put, taken and replaced only at its ports
            (``ClosedNets``). An open net lets anyone outside write to any of its
            places; it opens the places only, not the knobs, and not the nets inside
            it, each of which has its own rule. Part of the definition: a copy, a
            fork and a net restored from its template keep it, and a saved form does
            not mention it.

    Closed nets (since 2026-11-23, ``ClosedNets``): outside a net are a body, guard
    or key function of a transition of the net around it, the program and its
    drivers, and the runner's requests; inside it are its own transitions, the
    handlers of its own controls, the tokens its definition starts it with, and the
    engine's own moves. A closed net refuses a write from outside at a place that is
    not one of its ports with ``ClosedNetError``; a body of the net around a net
    that is under the rule cannot set its knobs or fire it by name
    (``HeldByBodyError``). Reading, stepping and the whole-state acts (importing a
    marking, restoring, forking, saving) stay allowed. Python enforces this through
    the library's own functions, the runtime's commands and the runner, and checks
    after a body returns that it did not edit the private places of a closed net it
    held; a program that edits a place's list, ``params`` or ``disabled`` directly
    is not caught.

    JSON form: a graph dumps (in JSON mode) as, and validates from, its saved form
    ``{template, marking, params, enabled}`` (``petritype.core.serialisation``),
    so a pydantic token type with an ``ExecutableGraph`` field has the JSON form
    of a nested token (PROTOCOL.md §12.5) with no further work; validating one
    needs the template registered. A deep copy of a graph is a new net with its
    own ``net_id``.

    Deep stepping: ``fire_one_transition`` with ``descend`` (so ``execute_graph``,
    the sequential ``Runner`` and the protocol runner's ``step`` too) makes one deep
    step, which fires at most one transition in this net or in a net inside its
    tokens or its subnet places, in the order each such place's ``stepping`` rule
    gives (``NestedNets``, PROTOCOL.md §12.3).

    Subnet places: ``places`` may hold ``SubnetPlaceNode`` entries beside the
    ``ListPlaceNode`` ones, each holding a net of its own for this net's whole
    life. This net's edges reach a subnet through its port places only, named by
    ``Port(subnet place, port)``; ``place_named`` takes such a pair too, and
    ``subnet`` gives the net a subnet place holds.

    Groups: a place, a subnet place or a transition may name the group it is in
    (``group=``), and ``groups`` lists the net's groups in order. Only drawings
    read them.
    """
    places: Sequence[Union[ListPlaceNode, "SubnetPlaceNode"]]
    transitions: Sequence[FunctionTransitionNode]
    argument_edges: Sequence[ArgumentEdgeToTransition]
    return_edges: Sequence[ReturnedEdgeFromTransition]
    # Non-consuming edges (place -> transition), which leave their tokens in their places:
    # SnapshotEdge passes a deep copy (place untouched), and SurveyEdge, a SnapshotEdge, a deep
    # copy of every token, possibly none; MutateEdge passes the live tokens (transition may
    # modify them in place); ReadEdge, ReadWhereEdge and ReadAllEdge pass the live tokens to
    # look at, nothing copied and nothing told (since 2026-11-23).
    snapshot_edges: Sequence[SnapshotEdge] = Field(default_factory=list)
    mutate_edges: Sequence[MutateEdge] = Field(default_factory=list)
    read_edges: Sequence[ReadEdge] = Field(default_factory=list)
    step_count: int = 0
    # Name of the most recent transition fired by the last firing attempt (an
    # ``execute_graph`` call, or one sequential-Runner attempt), or None if it
    # fired nothing. Reset at the start of every attempt, so it always reflects
    # that attempt (never a stale earlier fire).
    # Unlike ``transition_history`` this is independent of history-retention
    # config, giving callers a reliable "what just fired" signal — e.g. for UI
    # highlighting — without inferring it from token-count diffs (which is
    # ambiguous for self-loops). Transition names are unique (enforced by
    # ``check_unique_names``), so a name unambiguously identifies the node.
    # The concurrent Runner deposit loops also set this (outside
    # ``execute_graph``); a batch of completions leaves only one name here, so
    # it is a convenience signal — diffing ``fired_counts`` via
    # ``petritype.runtime.fired_since`` is the lossless "what fired since I
    # last looked" API. A deep step that fired inside an inner net leaves its
    # ``InnerFiring`` here.
    last_fired: Optional[Union[str, InnerFiring]] = None
    # Cumulative count of how many times each transition has fired since this
    # graph was created (keyed by transition name). Like ``step_count`` it is
    # monotonic and never trimmed — independent of ``transition_history``'s
    # retention cap — so it is the authoritative answer to "has transition X
    # fired, and how often". Use it (instead of inspecting the capped history)
    # to drive stage/progress UI that must stay correct after the marking has
    # moved past a stage. A deep step's firing inside an inner net is counted
    # under its ``InnerFiring``.
    fired_counts: dict[Union[str, InnerFiring], int] = Field(default_factory=dict)
    # Names of transitions whose bodies are currently executing: they consumed their inputs but
    # have not yet deposited their outputs. Both modes maintain it. Concurrent mode holds every
    # airborne transition; sequential firing (``fire_one_transition``) holds the one running
    # transition while its body executes and empties the set when the body returns or raises.
    # Writers always assign a fresh set, so a reader on another thread sees a whole value.
    # The renderer lights these "in flight" transitions distinctly.
    in_flight: set[str] = Field(default_factory=set)
    transition_history: Sequence[FunctionTransitionNode] = Field(default_factory=list)
    input_place_history: Sequence[Sequence[ListPlaceNode]] = Field(default_factory=list)
    output_place_history: Sequence[Sequence[ListPlaceNode]] = Field(default_factory=list)
    token_history: Sequence[Any] = Field(default_factory=list)
    transition_selector: Optional[Callable] = None
    # Tie-break for the default selector among equal-priority fireable transitions:
    # "first_declared" (default) fires the first-defined, "last_declared" the
    # last-defined, "round_robin" the first declared after its last pick (wrapping).
    # FIFO pipelines run in batches under the first, token by token under the
    # second, stage by stage in turn under the third. Custom selectors ignore it.
    tie_break: Literal["first_declared", "last_declared", "round_robin"] = "first_declared"
    allow_token_copying: bool = False
    # When a firing fails after its input tokens were consumed, put them back
    # (best effort) before raising. Off by default: the body may have mutated
    # the tokens before failing, and silently restoring possibly-corrupt tokens
    # to a place is worse than a visible loss. Enable only when tokens are
    # immutable or bodies do not mutate them before they can fail. Either way
    # the raised TransitionFailedError carries the consumed tokens. Restoring
    # is not a rollback: what a body did inside a nested token's inner nets
    # (NestedNets.put / take / set_param) before failing stays done.
    restore_tokens_on_failure: bool = False
    # Monotonic token-id counter; see the class docstring. Never reset except by
    # constructing a new graph.
    next_token_id: int = 1
    record_events: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    net_id: int = 0
    template: Optional[str] = None
    disabled: set[str] = Field(default_factory=set)
    open: bool = False
    # Event records waiting to be drained; never dumped with the model.
    _events: list[Event] = PrivateAttr(default_factory=list)
    # The holds of the bodies that hold this net now (``BodyHold``, ``ClosedNets``): set
    # in stage 1 of a firing of the net around it and released when the body returns or
    # the firing ends. Kept in memory only: neither saved nor copied.
    _holds: list = PrivateAttr(default_factory=list)
    # The round-robin cursor (read by ``tie_break="round_robin"``): the declaration
    # index of the default selector's last pick in this net, under any tie-break, or
    # None before its first. Kept in memory only; a net loaded from its saved form
    # starts without one.
    _last_pick: Optional[int] = PrivateAttr(default=None)
    # The ring's cursor (``NestedNets``): the member of this net's ring whose turn
    # last fired something, a failure included, as its ``net_id`` (this net's own
    # for its own turn), or None before the first. Moved by ring turns only, kept in
    # memory only: neither saved nor copied.
    _ring_cursor: Optional[int] = PrivateAttr(default=None)
    # Cached topology lookup maps, invalidated automatically if topology lists/lengths or instance id change.
    _cache_key: Optional[tuple] = PrivateAttr(default=None)
    _places_by_name: dict[str, Union[ListPlaceNode, "SubnetPlaceNode"]] = PrivateAttr(default_factory=dict)
    _transitions_by_name: dict[str, FunctionTransitionNode] = PrivateAttr(default_factory=dict)
    _maps_cache: Optional[tuple[dict, dict, dict, dict]] = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def _from_saved_form(cls, value, handler):
        """A saved form (``{template, marking, ...}``) validates to the net it
        describes, rebuilt from its template; anything else validates as the
        graph's own fields."""
        from petritype.core.serialisation import NetJson  # the module imports this one
        if NetJson.looks_saved(value):
            try:
                return NetJson.restore(value)
            except (LookupError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"not a saved net: {error}") from error
        return handler(value)

    @model_serializer(mode="wrap")
    def _as_saved_form(self, handler, info):
        """In JSON mode (``model_dump_json``, ``model_dump(mode="json")``, a
        pydantic token holding a graph) the saved form; in Python mode the fields,
        as before."""
        if info.mode == "json":
            from petritype.core.serialisation import NetJson
            return NetJson.save(self)
        return handler(self)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """The schema of the saved form: the one JSON shape a graph has (its
        fields hold callables, which have none)."""
        return {
            "title": cls.__name__,
            "description": "A net in its saved form: the template that rebuilds it, its marking by "
                           "place, its parameters and its transition switches (PROTOCOL.md §12.5).",
            "type": "object",
            "properties": {
                "template": {"type": "string"},
                "marking": {"type": "object", "additionalProperties": {"type": "array"}},
                "params": {"type": "object"},
                "enabled": {"type": "object", "additionalProperties": {"type": "boolean"}},
            },
            "required": ["marking"],
        }

    def __deepcopy__(self, memo=None):
        """A deep copy is a new net: it gets its own ``net_id``, so a forked nested
        token is told apart from the original, and its ring starts afresh, the
        ring's cursor being the original's alone, and no body holds it."""
        memo = {} if memo is None else memo
        memo.setdefault(id(self._holds), [])  # the holds are this net's, not the copy's
        copy = super().__deepcopy__(memo)
        copy.net_id = next(_net_ids)
        copy._ring_cursor = None
        copy._holds = []
        return copy

    @property
    def is_closed(self) -> bool:
        """Whether the net is closed (since 2026-11-23): it is under the rule of
        ``ClosedNets`` (it marks a place as a port) and does not declare itself
        ``open``. From outside a closed net, tokens are put, taken and replaced only
        at its ports."""
        return not self.open and ClosedNets.under_the_rule(self)

    @property
    def held_by_body(self) -> bool:
        """Whether a body of the net around this net holds it now (``BodyHold``):
        from stage 1 of that firing until its body returns or the firing ends."""
        return bool(self._holds)

    def export_marking(self) -> dict[str, list]:
        """``{place: [tokens as JSON]}``, oldest first (``NetJson.export_marking``)."""
        from petritype.core.serialisation import NetJson
        return NetJson.export_marking(self)

    def import_marking(self, marking: dict[str, list]) -> None:
        """Replace the marking from ``{place: [tokens as JSON]}``; a place not
        listed loads empty, an unknown place is an error, ids are not preserved
        (``NetJson.import_marking``)."""
        from petritype.core.serialisation import NetJson
        NetJson.import_marking(self, marking)

    def model_post_init(self, context: Any, /) -> None:
        # Seed tokens get their ids here, in place order. Runs once per construction
        # (not on model_copy), after pydantic has initialised the private attributes.
        self.net_id = next(_net_ids)
        TokenIds.seed(self)
        NestedNets.sync_recording(self)

    def drain_events(self) -> list[Event]:
        """Return the recorded events, oldest first, and clear them. Empty unless
        ``record_events`` was on while they happened. The events of inner nets
        (``NestedNets``) that have not yet reached this log are gathered first,
        wrapped as ``Nested``, so the result is complete; they come after whatever
        this graph recorded before they were gathered."""
        NestedNets.drain(self)
        events, self._events = self._events, []
        return events

    def forget_cursors(self) -> None:
        """Start the round-robin tie-break and the ring of the deep step afresh, as
        a freshly built net does. Loading a saved form does this: the cursors are
        kept in memory only."""
        self._last_pick = None
        self._ring_cursor = None

    def _topology_key(self) -> tuple:
        return (
            id(self),
            len(self.places),
            len(self.transitions),
            len(self.argument_edges),
            len(self.return_edges),
            len(self.snapshot_edges),
            len(self.mutate_edges),
            len(self.read_edges),
        )

    def _ensure_caches(self) -> None:
        key = self._topology_key()
        if self._cache_key == key:
            return
        self._places_by_name = {place.name: place for place in self.places}
        self._transitions_by_name = {transition.name: transition for transition in self.transitions}
        self._maps_cache = None
        self._cache_key = key

    def place_named(self, name: PlaceKey) -> Optional[Union[ListPlaceNode, "SubnetPlaceNode"]]:
        """The place of this net with this name, a subnet place included, or, for
        a ``Port`` pair, the port place of that subnet place, which is a place of
        the subnet. ``None`` if there is none. A name alone never finds a port:
        ``place_named("Lobby")`` is this net's own ``Lobby`` or nothing."""
        if isinstance(name, Port):
            holder = self.place_named(name.place)
            return holder.port_named(name.port) if isinstance(holder, SubnetPlaceNode) else None
        self._ensure_caches()
        return self._places_by_name.get(name)

    def subnet(self, name: str) -> Optional["ExecutableGraph"]:
        """The net the subnet place ``name`` holds, or ``None`` if ``name`` is no
        subnet place of this net. For reading: to step or change a subnet from the
        program, use ``NestedNets.subnet_scope``, which holds it still while a
        firing of this net on one of its ports is in flight."""
        place = self.place_named(name) if isinstance(name, str) else None
        return place.graph if isinstance(place, SubnetPlaceNode) else None

    def transition_named(self, name: str) -> Optional[FunctionTransitionNode]:
        self._ensure_caches()
        return self._transitions_by_name.get(name)

    @property
    def groups(self) -> tuple[str, ...]:
        """The names of this net's groups, in the order they are declared: a group
        exists from its first mention by a node's ``group``, reading the places in
        declaration order (subnet places among them) and then the transitions.

        A group is a named set of places and transitions of this net that only
        drawings read: firing, stepping, token ids, saving and the events are
        those of the same net with no groups. A node is in one group or none, a
        group holds no group, and a name may hold any character. A subnet's
        groups are its own, read from it (``subnet(name).groups``), and a port's
        group is one of them. Part of the definition, so a copy or a template
        carries them, and a saved form does not mention them."""
        return tuple(dict.fromkeys(
            node.group for node in (*self.places, *self.transitions) if node.group is not None
        ))

    def __petritype_nets__(self) -> dict[str, "ExecutableGraph"]:
        """A graph used as a token holds itself as its one inner net, unnamed."""
        return {"": self}

    @model_validator(mode='before')
    def check_unique_names(cls, values):
        place_names = [place.name for place in values.get('places', [])]
        transition_names = [transition.name for transition in values.get('transitions', [])]
        if len(place_names) != len(set(place_names)):
            raise ValueError("Place names must be unique.")
        if len(transition_names) != len(set(transition_names)):
            raise ValueError("Transition names must be unique.")
        return values

    @model_validator(mode="before")
    def check_subnet_places(cls, values):
        """A subnet place holds a net of its own: the same net in two subnet
        places of one net would be stepped and told twice."""
        holders: dict[int, str] = {}
        for place in values.get('places', []):
            if isinstance(place, SubnetPlaceNode):
                first = holders.setdefault(id(place.graph), place.name)
                if first != place.name:
                    raise ValueError(
                        f"subnet places {first!r} and {place.name!r} are given the same net; each holds one of its own"
                    )
        return values

    @model_validator(mode="before")
    def check_edge_names(cls, values):
        places = values.get('places', [])
        transitions = values.get('transitions', [])
        argument_edges = values.get('argument_edges', [])
        return_edges = values.get('return_edges', [])

        places_by_name = {place.name: place for place in places}
        transition_names = {transition.name for transition in transitions}

        for edge in argument_edges:
            _edge_place(places_by_name, edge.place_node_name, "Argument", edge.transition_node_name)
            if edge.transition_node_name not in transition_names:
                raise ValueError(f"Argument edge references unknown transition: {edge.transition_node_name}")

        for edge in return_edges:
            _edge_place(places_by_name, edge.place_node_name, "Return", edge.transition_node_name)
            if edge.transition_node_name not in transition_names:
                raise ValueError(f"Return edge references unknown transition: {edge.transition_node_name}")

        for edge in (
            *values.get('snapshot_edges', []), *values.get('mutate_edges', []), *values.get('read_edges', []),
        ):
            _edge_place(places_by_name, edge.place_node_name, "Non-consuming", edge.transition_node_name)
            if edge.transition_node_name not in transition_names:
                raise ValueError(f"Non-consuming edge references unknown transition: {edge.transition_node_name}")

        return values

    @model_validator(mode="before")
    def check_edge_types(cls, values):
        places = values.get('places', [])
        transitions = values.get('transitions', [])
        argument_edges = values.get('argument_edges', [])
        return_edges = values.get('return_edges', [])
        places_by_name = {place.name: place for place in places}
        transition_names_to_nodes = {transition.name: transition for transition in transitions}

        for edge in argument_edges:
            if not isinstance(edge, ArgumentEdgeToTransition):
                raise TypeError(f"Expected ArgumentEdgeToTransition, got {type(edge)}")
            place = _edge_place(places_by_name, edge.place_node_name, "Argument", edge.transition_node_name)
            if not isinstance(place, ListPlaceNode):
                raise NotImplementedError("Currently only ListPlaceNode is supported.")
            transition = transition_names_to_nodes[edge.transition_node_name]
            if not isinstance(transition, FunctionTransitionNode):
                raise NotImplementedError("Currently only FunctionTransitionNode is supported.")
            place_type = place.type  # This needs to be the value of the 'type' field of the place.
            argument_type = get_type_hints(transition.function).get(edge.argument)
            if isinstance(edge, DrainEdge):
                # Exactly list[T] for the place's T, which also covers a place of lists.
                ExecutableGraphCheck.ensure_argument_takes_every_token(edge, place, transition, argument_type)
            elif place_type is not None and argument_type is not None:
                if not CompareTypes.between_annotations_where_both_maybe_in_list(
                    annotation1=place_type,  # The place.type contains the inner type event if the place
                    # is a ListPlaceNode that holds a list of tokens.
                    annotation2=argument_type,  # This is to allow the case of passing in all the tokens
                    # at once as a list.
                ):
                    raise TypeError(
                        f"Type mismatch for argument edge from {_place_words(edge.place_node_name)} to transition "
                        f"'{transition.name}': place type '{place_type}' does not match argument type "
                        f"'{argument_type}'."
                    )

        for edge in return_edges:
            if not isinstance(edge, ReturnedEdgeFromTransition):
                raise TypeError(f"Expected ReturnedEdgeFromTransition, got {type(edge)}")
            place = _edge_place(places_by_name, edge.place_node_name, "Return", edge.transition_node_name)
            if not isinstance(place, ListPlaceNode):
                raise NotImplementedError("Currently only ListPlaceNode is supported.")
            transition = transition_names_to_nodes[edge.transition_node_name]
            if not isinstance(transition, FunctionTransitionNode):
                raise NotImplementedError("Currently only FunctionTransitionNode is supported.")
            if transition.output_distribution_function is not None:
                # A distribution function reshapes the result, so the return
                # annotation does not describe individual tokens; those are
                # validated against their destination places at fire time.
                continue
            place_type = place.type
            return_type = get_type_hints(transition.function).get('return')
            if edge.return_index is not None and return_type is not None:
                # Indexed edge: the token flowing along it is the return tuple's
                # element at return_index, not the whole return value.
                if get_origin(return_type) is not tuple:
                    raise TypeError(
                        f"Return edge from transition '{transition.name}' to {_place_words(edge.place_node_name)} has "
                        f"return_index={edge.return_index} but the return annotation '{return_type}' "
                        f"is not a tuple."
                    )
                tuple_args = get_args(return_type)
                if len(tuple_args) == 2 and tuple_args[1] is Ellipsis:
                    return_type = tuple_args[0]  # tuple[X, ...]: every element is X
                elif edge.return_index < len(tuple_args):
                    return_type = tuple_args[edge.return_index]
                else:
                    raise TypeError(
                        f"Return edge from transition '{transition.name}' to {_place_words(edge.place_node_name)} has "
                        f"return_index={edge.return_index} but the return annotation '{return_type}' "
                        f"only has {len(tuple_args)} elements."
                    )
            # A union return annotation routes by type at fire time, so the edge
            # is valid if any non-None arm can land in this place (None results
            # are dropped, never deposited).
            if get_origin(return_type) in (Union, UnionType):
                candidate_types = [t for t in get_args(return_type) if t is not type(None)]
            elif return_type is type(None):
                candidate_types = []
            else:
                candidate_types = [return_type]
            if not candidate_types:
                continue
            if place_type is not None and return_type is not None:
                if not any(
                    CompareTypes.between_annotations_where_both_maybe_in_list(
                        annotation1=place_type,  # May itself be a list type — places can hold list tokens.
                        annotation2=candidate,  # All the tokens could be returned at once as a list.
                    )
                    for candidate in candidate_types
                ):
                    raise TypeError(
                        f"Type mismatch for return edge from transition '{transition.name}' to "
                        f"{_place_words(edge.place_node_name)}: place type '{place_type}' does not match return type "
                        f"'{return_type}'."
                    )

        # Indexed and non-indexed return edges cannot be mixed on one transition:
        # the fire path either splits a tuple by index or routes the whole result.
        return_edges_by_transition: dict[str, list] = {}
        for edge in return_edges:
            return_edges_by_transition.setdefault(edge.transition_node_name, []).append(edge)
        for transition_name, edges in return_edges_by_transition.items():
            if ExecutableGraphCheck.return_indices_are_a_mix_of_none_and_non_none(tuple(edges)):
                raise ValueError(
                    f"Return edges of transition '{transition_name}' mix indexed and non-indexed "
                    f"return_index values; use integers on all of them or on none."
                )

        for edge in (
            *values.get('snapshot_edges', []), *values.get('mutate_edges', []), *values.get('read_edges', []),
        ):
            place = _edge_place(places_by_name, edge.place_node_name, "Non-consuming", edge.transition_node_name)
            transition = transition_names_to_nodes[edge.transition_node_name]
            place_type = place.type
            argument_type = get_type_hints(transition.function).get(edge.argument)
            if isinstance(edge, (SurveyEdge, ReadAllEdge)):
                ExecutableGraphCheck.ensure_argument_takes_every_token(edge, place, transition, argument_type)
            elif isinstance(edge, ReadEdge):
                ExecutableGraphCheck.ensure_argument_takes_one_token(edge, place, transition, argument_type)
            elif place_type is not None and argument_type is not None:
                if not CompareTypes.between_annotations_where_both_maybe_in_list(
                    annotation1=place_type,
                    annotation2=argument_type,
                ):
                    raise TypeError(
                        f"Type mismatch for non-consuming edge from {_place_words(edge.place_node_name)} to "
                        f"transition '{transition.name}': place type '{place_type}' does not match argument type "
                        f"'{argument_type}'."
                    )

        return values

    @model_validator(mode="after")
    def check_read_edges(self) -> "ExecutableGraph":
        """A place a read edge reads is read by no other input edge of the same
        transition (``ExecutableGraphCheck.ensure_read_edges_stand_alone``)."""
        ExecutableGraphCheck.ensure_read_edges_stand_alone(self)
        return self

    @model_validator(mode="after")
    def check_input_clauses(self) -> "ExecutableGraph":
        """A keyed edge whose filter reads the token of another input (a bound
        edge, ``KeyedEdges``) names an input of its own transition that gives one
        token, with the keys declared on both places
        (``ExecutableGraphCheck.ensure_input_clauses_are_sound``)."""
        ExecutableGraphCheck.ensure_input_clauses_are_sound(self)
        return self

    @model_validator(mode="after")
    def check_reaches(self) -> "ExecutableGraph":
        """An input edge that declares the ports it reaches stands on a place that
        holds nets, and names each port by a name that is not empty
        (``ExecutableGraphCheck.ensure_reaches_are_sound``, since 2026-11-23)."""
        ExecutableGraphCheck.ensure_reaches_are_sound(self)
        return self


class SubnetPlaceNode(PositionalArgsBaseModel):
    """A subnet place: a place of the net that holds one net of its own, its
    **subnet**, given when the net is built and kept for the net's whole life.

    ``SubnetPlaceNode("Level 3", build_level(), stepping="inner_first")``. The
    subnet is a separate net, with its own marking, token ids, parameters and
    log, and the net around it reaches it only through its **port places**: the
    places its own definition marks ``ListPlaceNode(..., port=True)``. An edge of
    the outer net names one by the pair, ``Port("Level 3", "Lobby")``, where it
    would name a place, and every edge kind, every way of choosing an output,
    guards, keyed edges, queries and outside input then use it as a place of
    the outer net. The port keeps its one list of tokens in the subnet, the same
    list whichever net looks, and its ids are minted by the subnet. The subnet
    place itself holds no tokens, and no edge may name it.

    ``stepping`` is the place's stepping rule (``Stepping``), as for a place
    whose tokens carry nets: a deep step of the outer net counts the subnet as
    one net at this place's position. The rule is part of the definition, not
    saved; it may be changed by assigning ``stepping``. A port's own stepping
    rule, if it is a nested place, is the subnet's to give.

    The subnet may be any built net. Built through a template (``Templates``)
    it carries the template's name, and the outer net can then be saved,
    restored and copied with it (``NetJson``); built without one it runs, and
    the outer net cannot be saved.

    ``group`` names the outer net's group the subnet place is in, or is
    ``None`` for none (``ExecutableGraph.groups``); its ports go where it goes.
    The subnet has groups of its own, given on its own nodes.
    """

    name: PlaceNodeName
    graph: ExecutableGraph
    stepping: Stepping = "inner_first"
    group: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _not_a_port(cls, values):
        """A port is an ordinary place or a nested one, so that no edge crosses two
        borders: a net hands tokens to a subnet of its subnet by a transition of its
        own, through a port of its own."""
        if isinstance(values, dict) and "port" in values:
            raise ValueError(
                f"subnet place {values.get('name')!r} cannot be a port: a port is an ordinary place or a nested "
                f"place, so that no edge crosses two borders"
            )
        return values

    @property
    def tokens(self) -> tuple:
        """A subnet place holds no tokens of its own: always empty."""
        return ()

    @property
    def ports(self) -> tuple[ListPlaceNode, ...]:
        """The subnet's port places, in the subnet's declaration order."""
        return tuple(place for place in self.graph.places if isinstance(place, ListPlaceNode) and place.port)

    def port_named(self, name: str) -> Optional[ListPlaceNode]:
        """The subnet's port place of that name, or ``None``: a place of the subnet
        that is not marked as a port cannot be reached from outside."""
        return next((place for place in self.ports if place.name == name), None)


def _named(key: PlaceKey) -> str:
    """A place an edge names, quoted for a message: ``'Orders'``, or ``port
    'Lobby' of 'Level 1'``."""
    if isinstance(key, Port):
        return f"port {key.port!r} of {key.place!r}"
    return repr(key)


def _spelt(annotation: Any) -> str:
    """A type for a message: a class by its name, anything else as written."""
    return annotation.__name__ if isinstance(annotation, type) else str(annotation)


def _place_words(key: PlaceKey) -> str:
    """A place an edge names, for a message: ``place 'Door'``, or ``port 'Lobby'
    of subnet place 'Level 3'``."""
    if isinstance(key, Port):
        return f"port '{key.port}' of subnet place '{key.place}'"
    return f"place '{key}'"


def _edge_place(
    places_by_name: dict[str, Union[ListPlaceNode, SubnetPlaceNode]], key: PlaceKey, edge_kind: str,
    transition_name: str,
) -> ListPlaceNode:
    """The place an edge of ``transition_name`` reaches by ``key``: a place of the
    net by its name, or a port place of one of its subnet places by the pair.
    Raises ``ValueError`` for a name that is no place, an edge on a subnet place
    itself, a ``Port`` beside a place that is not a subnet place, and a port the
    subnet does not have (a place of the subnet that is not marked as a port
    included)."""
    if isinstance(key, Port):
        holder = places_by_name.get(key.place)
        if holder is None:
            raise ValueError(f"{edge_kind} edge references unknown place: {key.place}")
        if not isinstance(holder, SubnetPlaceNode):
            raise ValueError(
                f"{edge_kind} edge of transition {transition_name!r} names Port({key.place!r}, {key.port!r}), "
                f"but {key.place!r} is not a subnet place"
            )
        port = holder.port_named(key.port)
        if port is None:
            raise ValueError(f"subnet place {key.place!r} has no port {key.port!r}")
        return port
    place = places_by_name.get(key)
    if place is None:
        raise ValueError(f"{edge_kind} edge references unknown place: {key}")
    if isinstance(place, SubnetPlaceNode):
        raise ValueError(
            f"transition {transition_name!r} has an edge on subnet place {key!r}, which holds no tokens of its "
            f"own; an edge reaches it through one of its ports, Port({key!r}, port)"
        )
    return place


ExecutableGraph.model_rebuild()


@dataclass
class FiringTokenIds:
    """Which token ids one firing touched. Passive state, filled in as the firing
    goes: stage 1 records what it consumed (per argument, in the order taken), what
    it handed over through mutate edges, and what it handed over through read edges
    (since 2026-11-23); stage 3 records what it deposited.
    ``restore_argument_tokens_to_places`` reads ``consumed`` to put the same ids back.
    ``read`` is told by no event: the concurrent runtime reads it, with ``mutated``,
    for the tokens whose inner nets no inner firing may touch while the body runs.
    Places are named by their ``PlaceKey``, a port by its ``Port`` pair. ``held`` (since
    2026-11-23) is the holds stage 1 set on the nets the body was handed
    (``ClosedNets.lend``), which the firing releases when the body returns.
    """

    consumed: dict[ArgumentName, tuple[PlaceKey, list[int]]] = field(default_factory=dict)
    mutated: list[PlaceTokenId] = field(default_factory=list)
    produced: list[PlaceTokenId] = field(default_factory=list)
    read: list[PlaceTokenId] = field(default_factory=list)
    held: list = field(default_factory=list)


class TokenIds:
    """Functions that keep each place's token ids in step with its tokens.

    Every token in a place has a stable integer id (``ListPlaceNode.token_ids``,
    parallel to ``tokens``) drawn from ``ExecutableGraph.next_token_id``. The engine
    moves ids together with tokens; code that assigns or resizes ``place.tokens``
    directly bypasses them, which ``resync`` detects and repairs at each engine
    entry point.

    A port place's ids are its subnet's: whichever net puts a token into a port,
    the subnet's counter mints its id (``minter``), so a port has one set of ids,
    in increasing order of arrival, whichever net looks.
    """

    def allocate(executable_graph: ExecutableGraph, count: int) -> list[int]:
        """Take ``count`` fresh ids from the graph's counter."""
        start = executable_graph.next_token_id
        executable_graph.next_token_id = start + count
        return list(range(start, start + count))

    def minter(executable_graph: ExecutableGraph, place: PlaceKey) -> ExecutableGraph:
        """The net whose counter mints the ids of tokens entering the place this
        net names ``place``: the subnet, for a ``Port`` pair; this net, for a name."""
        if isinstance(place, Port):
            subnet = executable_graph.subnet(place.place)
            if subnet is None:
                raise KeyError(f"no subnet place {place.place!r}")
            return subnet
        return executable_graph

    def seed(executable_graph: ExecutableGraph) -> None:
        """Give every token already in a place a fresh id, place by place in
        declaration order. Called once, when the graph is constructed; a subnet's
        tokens were given theirs when the subnet was."""
        for place in executable_graph.places:
            if isinstance(place, ListPlaceNode):
                place._token_ids = TokenIds.allocate(executable_graph, len(place.tokens))

    def resync(executable_graph: ExecutableGraph) -> tuple[PlaceNodeName, ...]:
        """Re-id every place whose token list and id list differ in length, and
        record a ``Resynced`` event for each. Returns the names of the places re-ided.
        The subnets of this net's subnet places are resynced too, each by its own
        counter and in its own log, since their ports are places this net's
        transitions reach; the names returned are this net's own places.

        A length check is all it can do: code that swaps a token for another in
        place, keeping the length, goes unseen and the old id names the new token.
        Called at every engine entry point (each firing, each command applied, each
        concurrent-loop sweep, each ``locate``); cheap, one ``len`` pair per place.
        """
        resynced = []
        for place in executable_graph.places:
            if isinstance(place, SubnetPlaceNode):
                TokenIds.resync(place.graph)
                continue
            if len(place._token_ids) != len(place.tokens):
                place._token_ids = TokenIds.allocate(executable_graph, len(place.tokens))
                GraphEvents.record(executable_graph, Resynced(place.name))
                resynced.append(place.name)
        return tuple(resynced)

    def append(executable_graph: ExecutableGraph, place: ListPlaceNode, tokens: list) -> list[int]:
        """Append ``tokens`` to ``place`` with fresh ids; return the ids."""
        ids = TokenIds.allocate(executable_graph, len(tokens))
        place.tokens.extend(tokens)
        place._token_ids.extend(ids)
        return ids

    def locate(executable_graph: ExecutableGraph, token_id: int) -> Optional[tuple[PlaceNodeName, Any]]:
        """``(place_name, token)`` for the live token with this id, or None if no
        place holds it (consumed, removed, or never issued). Resyncs first, so a
        place whose tokens were assigned directly answers with its fresh ids.
        Only this net's own places are searched: a port's tokens carry their
        subnet's ids, which may equal ids of this net's own, so one is located
        through its subnet."""
        TokenIds.resync(executable_graph)
        for place in executable_graph.places:
            if not isinstance(place, ListPlaceNode):
                continue
            for index, candidate in enumerate(place._token_ids):
                if candidate == token_id:
                    return place.name, place.tokens[index]
        return None


class GraphEvents:
    """Functions that append event records (``petritype.core.events``) to a graph.
    Every append goes through ``record``, which does nothing unless
    ``executable_graph.record_events`` is on."""

    def record(executable_graph: ExecutableGraph, event: Event) -> None:
        if executable_graph.record_events:
            executable_graph._events.append(event)

    def consumed_pairs(firing_ids: FiringTokenIds) -> tuple[PlaceTokenId, ...]:
        """The consumed ids as ``(place, id)`` pairs, in the order they were taken."""
        return tuple(
            (place_name, token_id)
            for place_name, ids in firing_ids.consumed.values()
            for token_id in ids
        )

    def record_mutations(
        executable_graph: ExecutableGraph,
        firing_ids: FiringTokenIds,
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
    ) -> None:
        """One ``Mutated`` per token handed over through a mutate edge that is still
        in its place (in concurrent mode a peer may have consumed it meanwhile)."""
        for place_name, token_id in firing_ids.mutated:
            if token_id in place_names_to_nodes[place_name]._token_ids:
                GraphEvents.record(executable_graph, Mutated(place_name, token_id))

    def record_fired(
        executable_graph: ExecutableGraph,
        transition_name: TransitionName,
        firing_ids: FiringTokenIds,
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        duration_ms: float,
    ) -> None:
        """A firing completed: the inner events its body caused (``NestedNets``),
        its ``Mutated`` records, then its ``Fired``."""
        if not executable_graph.record_events:
            return
        NestedNets.drain(executable_graph)
        GraphEvents.record_mutations(executable_graph, firing_ids, place_names_to_nodes)
        GraphEvents.record(executable_graph, Fired(
            transition=transition_name,
            consumed=GraphEvents.consumed_pairs(firing_ids),
            produced=tuple(firing_ids.produced),
            duration_ms=duration_ms,
        ))

    def record_failed(
        executable_graph: ExecutableGraph,
        transition_name: TransitionName,
        firing_ids: FiringTokenIds,
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        restored: bool,
        exception: BaseException,
    ) -> None:
        """A firing failed: the inner events its body caused, its ``Mutated``
        records, ``Added`` for anything a failed deposit had already placed, then
        its ``Failed``."""
        if not executable_graph.record_events:
            return
        NestedNets.drain(executable_graph)
        GraphEvents.record_mutations(executable_graph, firing_ids, place_names_to_nodes)
        for place_name, token_id in firing_ids.produced:
            GraphEvents.record(executable_graph, Added(place_name, token_id))
        GraphEvents.record(executable_graph, Failed(
            transition=transition_name,
            consumed=GraphEvents.consumed_pairs(firing_ids),
            restored=restored,
            error=f"{type(exception).__name__}: {exception}",
        ))


class NetParams:
    """Functions over a graph's net-level parameters (``ExecutableGraph.params``)."""

    def set(executable_graph: ExecutableGraph, name: str, value: Any) -> None:
        """Set a declared parameter and record ``ParamChanged(None, name)``. A name
        the net did not declare at construction is a ``KeyError``: parameters are
        part of what a net is, not a free store. A body of the net around a net
        under the rule, holding it, is refused with ``HeldByBodyError``
        (``ClosedNets``, since 2026-11-23)."""
        if name not in executable_graph.params:
            raise KeyError(f"the net declares no parameter {name!r}")
        ClosedNets.ensure_controls(executable_graph, f"setting parameter {name!r}")
        executable_graph.params[name] = value
        GraphEvents.record(executable_graph, ParamChanged(None, name))


@dataclass(eq=False)
class BodyHold:
    """One body's hold on a net it was handed (``ClosedNets``, since 2026-11-23).
    Stage 1 sets one on every net carried by a token it hands a body live, by any
    edge but a snapshot or a survey, which hand over copies; the firing releases it
    when the body returns, or when the firing ends otherwise. ``NestedNets.subnet_scope``
    on a held net sets one on the subnet for the block's time. Passive state.

    ``places`` is the before-record of a closed net's private places, per place its
    tokens and their ids, and ``controls`` that of the knobs of a net under the
    rule; ``None`` where the rule asks for none. ``refusal`` is the first refusal or
    difference found while the hold stood, which fails the firing when the body
    returns, even if the body caught the error."""

    net: "ExecutableGraph"
    places: Optional[dict[str, tuple[list, list[int]]]]
    controls: Optional[tuple]
    refusal: Optional[Exception] = None


class ClosedNets:
    """Functions of the rule on closed nets (since 2026-11-23).

    A net that marks a place as a port is **under the rule**, and **closed** unless
    its definition declares it ``open`` (``ExecutableGraph.is_closed``). Stage two of
    the rule will put every net under it (``CLOSED_WITHOUT_PORTS``).

    **Outside** a net: a body, guard or key function of a transition of the net
    around it, by any edge; the program and its drivers; the runner's requests.
    **Inside** it: its own transitions' bodies, guards and keys, the handlers of its
    own controls (a ``press``, ``apply_control``), the tokens its definition starts it
    with, and the engine's own moves (a firing's inputs and outputs, a failed
    firing's restore, a deep step's inner firings).

    - **Tokens.** From outside a closed net, putting, taking or replacing tokens is
      allowed at its ports and refused at any other place with ``ClosedNetError``
      (``ensure_tokens``): ``NestedNets.put`` and ``NestedNets.take``, the runtime's
      ``Extend`` and ``SetTokens``, and the runner's ``inject``. A write at
      ``Port(subnet place, port)`` is a write to a place of the net around the
      subnet, judged by that net's rule first: a closed net refuses it, since a port
      of a subnet place is never one of its own ports, and an open one lets it land
      at the subnet's port. An open net lets every write land. The check is the
      net's own, whoever calls, a body holding the net or not. A body that builds a
      new net and puts it in place of a token's net has made a new net, which no
      hold covers.
    - **Knobs and firing by name.** Setting a parameter, a transition's switch or
      keywords, firing by name (``Fire``) and a selector of one's own stay with the
      program, its drivers and the runner. A body of the net around a net that is
      under the rule, while it holds the net, is refused them with
      ``HeldByBodyError`` (``ensure_controls``), on a closed net and on one declared
      open alike: ``open`` opens the places, not the knobs. A held net that marks no
      port keeps today's behaviour, so a body still sets its parameters (the transit
      example's ``Depart`` and ``Arrive`` set their cabin's ``stopped``). Stage two
      puts every net under the rule, so such a net will give the setting to a
      transition of its own or leave it to the program; declaring it open will
      not serve, since ``open`` opens the places only.
    - **Reading and stepping** are allowed for everyone, a body included: a body may
      step a net it holds (``execute_graph``), whose transitions are inside it.
    - **Whole-state acts** (``import_marking``, ``NetJson.load_into`` and
      ``restore``, a fork, saving) reach the places through the library's own
      paths and stay the program's. A body that loads a marking into a closed net
      it holds changes its private places, which the before-and-after check finds.

    **The before-and-after check.** A body holds the live net and can edit
    ``place.tokens``, ``params`` or ``disabled`` directly, which no function sees.
    So each hold records the tokens and ids of a closed net's private places, and
    the knobs of a net under the rule (``record``), and the firing fails when the
    body returns if they differ (``settle``). Each move of the net's own while the
    hold stands (a step the body makes) compares first and records afresh after
    (``moving``), so that the net's own firings count as its own. A change made
    inside a token object of a private place, or inside a parameter's value, keeps
    its place, its id and its identity, and is not seen. Neither is a program that
    edits a place's list directly: Python enforces the rule through its own
    functions, the runtime's commands and the runner, and through this check for a
    body."""

    def under_the_rule(executable_graph: ExecutableGraph) -> bool:
        """Whether the rule is in force on the net: it marks a place as a port, or
        stage two's default puts every net under it. The one test that reads
        ``CLOSED_WITHOUT_PORTS``."""
        return CLOSED_WITHOUT_PORTS or any(
            isinstance(place, ListPlaceNode) and place.port for place in executable_graph.places
        )

    def label(executable_graph: ExecutableGraph) -> str:
        """The net as a refusal names it: its template, or ``#`` and its ``net_id``."""
        return executable_graph.template if executable_graph.template is not None else f"#{executable_graph.net_id}"

    def note(executable_graph: ExecutableGraph, refusal: Exception) -> None:
        """Keep ``refusal`` on the active firing's hold(s) on ``executable_graph``
        (when called from inside a running transition body), or on every hold on
        the net when called outside a tracked firing, so concurrent peers holding
        the same net are not cross-tainted."""
        active = _ACTIVE_FIRING_IDS.get()
        if active is not None:
            scoped = [hold for hold in active.held if hold.net is executable_graph]
            if scoped:
                for hold in scoped:
                    if hold.refusal is None:
                        hold.refusal = refusal
                return
        for hold in executable_graph._holds:
            if hold.refusal is None:
                hold.refusal = refusal

    def ensure_tokens(executable_graph: ExecutableGraph, place: PlaceKey, act: str) -> None:
        """Refuse with ``ClosedNetError`` a write from outside at ``place`` of a
        closed net, unless it is one of the net's ports; a ``Port`` pair is never
        one. Called after the place is known to exist."""
        if not executable_graph.is_closed:
            return
        if not isinstance(place, Port):
            node = executable_graph.place_named(place)
            if not isinstance(node, ListPlaceNode) or node.port:
                return
        refusal = ClosedNetError(ClosedNets.label(executable_graph), place, act)
        ClosedNets.note(executable_graph, refusal)
        raise refusal

    def ensure_controls(executable_graph: ExecutableGraph, act: str) -> None:
        """Refuse with ``HeldByBodyError`` a knob or a firing by name on a net under
        the rule that a body holds now."""
        if not executable_graph._holds or not ClosedNets.under_the_rule(executable_graph):
            return
        refusal = HeldByBodyError(ClosedNets.label(executable_graph), act)
        ClosedNets.note(executable_graph, refusal)
        raise refusal

    def record(executable_graph: ExecutableGraph) -> tuple[Optional[dict], Optional[tuple]]:
        """The before-record of a hold: the tokens and ids of each private place of
        a closed net, and the knobs of a net under the rule (its parameters, its
        switches, its tie-break, its selector, the stepping rules of its places and
        each transition's keywords); ``None`` for each the rule does not ask for."""
        places = None
        if executable_graph.is_closed:
            places = {
                place.name: (list(place.tokens), list(place._token_ids))
                for place in executable_graph.places
                if isinstance(place, ListPlaceNode) and not place.port
            }
        controls = None
        if ClosedNets.under_the_rule(executable_graph):
            controls = (
                _snapshot_mapping(executable_graph.params),
                frozenset(executable_graph.disabled),
                executable_graph.tie_break,
                executable_graph.transition_selector,
                tuple(getattr(place, "stepping", None) for place in executable_graph.places),
                tuple(_snapshot_mapping(transition.kwargs or {}) for transition in executable_graph.transitions),
            )
        return places, controls

    def hold(executable_graph: ExecutableGraph) -> BodyHold:
        """A new hold on the net, with its before-record, set on the net."""
        places, controls = ClosedNets.record(executable_graph)
        hold = BodyHold(executable_graph, places, controls)
        executable_graph._holds.append(hold)
        return hold

    def lend(firing_ids: "FiringTokenIds", tokens: Iterable[Any]) -> None:
        """Stage 1 hands ``tokens`` to a body live: hold every net each carries."""
        for token in tokens:
            if not NestedNets.holds_nets(token):
                continue
            try:
                nets = NestedNets.of(token)
            except TypeError:
                continue
            firing_ids.held.extend(ClosedNets.hold(net) for net in nets.values())

    def release(firing_ids: "FiringTokenIds") -> Optional[Exception]:
        """Release the firing's holds and return the first refusal or difference
        found, if any; releasing again does nothing."""
        held, firing_ids.held = firing_ids.held, []
        found = None
        for hold in held:
            ClosedNets.unhold(hold)
            if found is None:
                found = hold.refusal or ClosedNets.difference(hold)
        return found

    def settle(firing_ids: "FiringTokenIds") -> None:
        """The body returned: release the firing's holds, and raise the first
        refusal or difference, which fails the firing."""
        found = ClosedNets.release(firing_ids)
        if found is not None:
            raise found

    def unhold(hold: BodyHold) -> None:
        holds = hold.net._holds
        for index, held in enumerate(holds):
            if held is hold:
                del holds[index]
                return

    def difference(hold: BodyHold) -> Optional[Exception]:
        """What changed under the hold since its record, as the refusal it would
        have been: ``ClosedNetError`` for a private place whose tokens or ids differ,
        ``HeldByBodyError`` for knobs that differ."""
        net = hold.net
        places, controls = ClosedNets.record(net)
        if hold.places is not None:
            for name, (tokens, ids) in hold.places.items():
                now_tokens, now_ids = places.get(name, ([], [])) if places is not None else ([], [])
                if ids != now_ids or len(tokens) != len(now_tokens) or any(
                    a is not b for a, b in zip(tokens, now_tokens)
                ):
                    return ClosedNetError(ClosedNets.label(net), name, "an edit of the place's tokens by a body")
        if hold.controls is not None and controls is not None and not _same_controls(hold.controls, controls):
            return HeldByBodyError(
                ClosedNets.label(net),
                "a change by a body to its parameters, switches, tie-break, selector, stepping rules or keywords",
            )
        return None

    @contextmanager
    def moving(executable_graph: ExecutableGraph) -> Iterator[None]:
        """Around a move of the net's own (one of its firings, a deep step from it)
        while a body holds it: compare each hold's record first, keeping what a body
        changed as the hold's refusal, and record afresh after, so that what the net
        did itself is its own. Nothing for a net no body holds."""
        if not executable_graph._holds:
            yield
            return
        for hold in executable_graph._holds:
            if hold.refusal is None:
                hold.refusal = ClosedNets.difference(hold)
        try:
            yield
        finally:
            for hold in executable_graph._holds:
                hold.places, hold.controls = ClosedNets.record(executable_graph)

    def pass_on(executable_graph: ExecutableGraph, subnet: ExecutableGraph) -> list[BodyHold]:
        """``subnet_scope`` on a net a body holds lends the body its subnet: hold
        the subnet for the block's time."""
        return [ClosedNets.hold(subnet)] if executable_graph._holds else []

    def hand_back(executable_graph: ExecutableGraph, holds: list[BodyHold]) -> None:
        """The block of ``subnet_scope`` ended: release the subnet's holds, and keep
        any refusal or difference found there on the holds of the net around it."""
        for hold in holds:
            ClosedNets.unhold(hold)
            found = hold.refusal or ClosedNets.difference(hold)
            if found is not None:
                ClosedNets.note(executable_graph, found)


def _snapshot_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy values in ``mapping`` where possible so in-place mutations to mutable
    containers inside params/kwargs are caught by ``_same_controls``; fall back to the
    value itself when it cannot be deep-copied (e.g. functions or locks)."""
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        try:
            result[key] = deepcopy(value)
        except Exception:
            result[key] = value
    return result


def _same_value(a: Any, b: Any) -> bool:
    if a is b:
        return True
    try:
        return bool(a == b)
    except Exception:
        return False


def _same_controls(before: tuple, now: tuple) -> bool:
    """Whether two records of a net's knobs (``ClosedNets.record``) agree."""
    params_before, disabled_before, tie_before, selector_before, stepping_before, kwargs_before = before
    params_now, disabled_now, tie_now, selector_now, stepping_now, kwargs_now = now
    if params_before.keys() != params_now.keys() or not all(
        _same_value(params_before[name], params_now[name]) for name in params_before
    ):
        return False
    if len(kwargs_before) != len(kwargs_now) or any(
        a.keys() != b.keys() or not all(_same_value(a[key], b[key]) for key in a)
        for a, b in zip(kwargs_before, kwargs_now)
    ):
        return False
    return (
        disabled_before == disabled_now and tie_before == tie_now and selector_before is selector_now
        and stepping_before == stepping_now
    )


@dataclass(frozen=True, eq=False)
class Member:
    """One net in a place of a net, as a deep step of that net comes to it: where
    it is (``step``, one level down from that net), the net, and the stepping rule
    of its place. Passive state, listed by ``NestedNets.members``; where it stands
    holds for the deep step it was listed in, since nothing around it moves until
    something fires and the step ends there."""

    step: PathStep
    net: "ExecutableGraph"
    stepping: Stepping


class NestedNets:
    """Functions over the nets a net holds in its places: the nets carried by
    tokens (nets as tokens) and the subnets of subnet places.

    A token of a place declared ``nested=True`` must be an ``ExecutableGraph`` (its
    one inner net is unnamed, ``""``) or implement ``__petritype_nets__(self) ->
    dict[str, ExecutableGraph]`` naming the nets it holds. A ``SubnetPlaceNode``
    holds one net, its subnet, for the outer net's whole life. The outer net then:

    - runs the inner nets as part of running itself, by one **deep step** at a
      time (``fire_one_transition`` with ``descend``), in the order the places'
      stepping rules give (below);
    - gathers their events into its own log as ``Nested`` records (``drain``), with
      the path to the net they happened in and the net's stable ``net_id``;
    - lets an outer body reach inside the tokens it holds (``put``, ``take``,
      ``set_param``), at the ports alone of a closed net (``ClosedNets``, since
      2026-11-23), and its edges reach a subnet's port places, which is the
      only coupling: the one place a net shares is a port, with its own subnet,
      and no inner transition sees the outer net.

    A subnet is one net at its place's position in walk order, under its place's
    rule, and its path step names the place alone, ``PathStep("Level 3")``. While
    a firing of the outer net that touches one of a subnet's ports is in flight
    (launched and not yet landed), the subnet takes no turn (``members``), and
    the program cannot step or change it (``subnet_scope``).

    One deep step of a net fires at most one transition, in that net or anywhere
    below it (PROTOCOL.md §12.3). It tries three bands in this order and stops at
    the first firing, or failure, it comes to:

    1. every net in a place whose rule is ``"inner_first"`` (the default), in walk
       order: a deep step of that net;
    2. the **ring**: the net itself and every net in a place whose rule is
       ``"take_turns"``, in walk order, starting after the member that fired last
       and going round once. The net's own turn is one firing chosen by its own
       selector; any other member's turn is a deep step of that net;
    3. every net in a place whose rule is ``"outer_first"``, in walk order, while
       nothing of the net itself is in flight: a deep step of that net.

    A ``"passive"`` place is in no band: only an outer body or the program steps
    its nets. **Walk order** is places in declaration order, a place's tokens
    oldest first, a token's nets in their declared order. A member's turn is a
    deep step of its own, so every level follows the rules of its own places, and
    one deep step still fires at most one transition in the whole tree.

    The ring's **cursor** is the member whose turn last fired something, the net's
    own ``net_id`` for its own turn: a ring turn moves it, a failed firing as well
    as one that landed, and a firing in the other two bands leaves it where it
    is. With no cursor (a fresh net, a loaded one, a copy) or one naming a net no
    longer in the ring (its token taken, or moved to a place with another rule),
    the ring starts at the net itself. Each net keeps its own, in memory only.

    A net is quiescent deep when it and every net in its places whose rule is not
    passive are at rest, at every depth: a deep step finds nothing to fire.

    A path is a tuple of ``PathStep`` values, one per level, outermost first, and
    a firing inside an inner net is told as an ``InnerFiring``: the path to the
    net it fired in and the transition's own name.
    """

    def of(token: Any) -> dict[str, ExecutableGraph]:
        """The inner nets of ``token`` by name, or ``TypeError`` if it holds none."""
        method = getattr(token, NESTED_NETS_METHOD, None)
        if method is None:
            raise TypeError(
                f"{type(token).__name__} holds no nets: a token of a nested place must be an "
                f"ExecutableGraph or implement {NESTED_NETS_METHOD}()"
            )
        nets = method()
        if not isinstance(nets, dict) or not all(
            isinstance(name, str) and isinstance(net, ExecutableGraph) for name, net in nets.items()
        ):
            raise TypeError(
                f"{type(token).__name__}.{NESTED_NETS_METHOD}() must return a dict of str to ExecutableGraph"
            )
        return nets

    def ensure_nested_token(token: Any, place: ListPlaceNode) -> None:
        """Raise ``TypeError`` naming the place if ``token`` cannot sit in the nested ``place``."""
        try:
            NestedNets.of(token)
        except TypeError as error:
            raise TypeError(f"Place {place.name!r} is nested but its token cannot hold nets: {error}") from error

    def any_nested(executable_graph: ExecutableGraph) -> bool:
        """Whether a place of this net holds nets: a nested place or a subnet place."""
        return any(isinstance(place, SubnetPlaceNode) or place.nested for place in executable_graph.places)

    def holds_nets_in(place: Union[ListPlaceNode, "SubnetPlaceNode"]) -> bool:
        """Whether ``place`` holds nets, so has a stepping rule: a subnet place, or
        a place whose tokens carry nets."""
        return isinstance(place, SubnetPlaceNode) or place.nested

    def walk(executable_graph: ExecutableGraph) -> Iterator[tuple[PathStep, ExecutableGraph]]:
        """Yield ``(step, inner)`` for every inner net one level down, in walk order
        (places in declaration order, a place's tokens oldest first, a token's nets
        in their declared order, a subnet place's subnet as one net at its place's
        position), passive places included. The step is as the marking stands now.
        It only reads: a token that has no id yet, because code appended to
        ``place.tokens`` directly, is left out until the next engine entry point
        resyncs its place (``TokenIds.resync``), which every deep step does first.
        The nets of tokens in a subnet's ports are the subnet's, one level further
        down."""
        for place in executable_graph.places:
            if isinstance(place, SubnetPlaceNode):
                yield PathStep(place.name), place.graph
                continue
            if not place.nested:
                continue
            for token_id, token in zip(list(place._token_ids), list(place.tokens)):
                for name, inner in NestedNets.of(token).items():
                    yield PathStep(place.name, token_id, name), inner

    def sync_recording(executable_graph: ExecutableGraph) -> None:
        """Make every inner net record events exactly when the outer one does."""
        for _step, inner in NestedNets.walk(executable_graph):
            if inner.record_events != executable_graph.record_events:
                inner.record_events = executable_graph.record_events
                NestedNets.sync_recording(inner)

    def drain(executable_graph: ExecutableGraph) -> None:
        """Gather the undrained events of every inner net into this graph's log,
        each wrapped as ``Nested`` with the path to the net it happened in. An
        inner net's own ``Nested`` records (from nets inside it) are re-based onto
        the longer path, so there is one wrapper whatever the depth. Called by
        ``drain_events``, before a firing is recorded and after each inner
        firing, so inner events land in the outer log in the order they happened."""
        for step, inner in NestedNets.walk(executable_graph):
            inner.record_events = executable_graph.record_events
            for event in inner.drain_events():
                if isinstance(event, Nested):
                    event = Nested((step, *event.path), event.nested_id, event.event)
                else:
                    event = Nested((step,), inner.net_id, event)
                GraphEvents.record(executable_graph, event)

    def resolve(executable_graph: ExecutableGraph, path: Sequence[PathStep]) -> ExecutableGraph:
        """The inner net at ``path`` (steps as ``walk`` yields them), or
        ``LookupError`` naming the step that failed."""
        graph = executable_graph
        for step in path:
            if not isinstance(step, PathStep):
                raise LookupError(f"a path is a sequence of PathStep values, not {step!r}")
            place = graph.place_named(step.place)
            if step.is_subnet:
                if step.net is not None:
                    raise LookupError(f"a step that names a net names a token: {step!r}")
                if not isinstance(place, SubnetPlaceNode):
                    raise LookupError(f"no subnet place {step.place!r}")
                graph = place.graph
                continue
            if not isinstance(place, ListPlaceNode) or not place.nested:
                raise LookupError(f"no nested place {step.place!r}")
            TokenIds.resync(graph)
            try:
                index = list(place._token_ids).index(step.token)
            except ValueError:
                raise LookupError(f"no token {step.token} in {step.place!r}") from None
            nets = NestedNets.of(place.tokens[index])
            if step.net not in nets:
                raise LookupError(f"token {step.token} in {step.place!r} holds no net named {step.net!r}")
            graph = nets[step.net]
        return graph

    def find(executable_graph: ExecutableGraph, net_id: int) -> Optional[tuple[tuple[PathStep, ...], ExecutableGraph]]:
        """``(path, inner)`` for the inner net with this ``net_id``, at any depth, or
        ``None``; the path is as the marking stands now."""
        for step, inner in NestedNets.walk(executable_graph):
            if inner.net_id == net_id:
                return (step,), inner
            found = NestedNets.find(inner, net_id)
            if found is not None:
                return (step, *found[0]), found[1]
        return None

    # --- deep stepping (the stepping rule of each place) ---

    def members(executable_graph: ExecutableGraph, held: frozenset = frozenset()) -> list[Member]:
        """Every net in a place of this net that deep stepping enters, in walk
        order, each with its place's rule: a passive place's nets are left out,
        and so are the nets of a token in ``held``, ``(place, token id)`` pairs of
        tokens an outer body in flight has in hand through a mutate or a read edge
        (the concurrent runtime's), which no inner firing may touch meanwhile, and the
        subnet of a subnet place one of whose ports a transition of this net in
        flight has an edge on (``held_subnets``): until that firing lands, the
        subnet takes no turn."""
        rules = {
            place.name: place.stepping for place in executable_graph.places if NestedNets.holds_nets_in(place)
        }
        still = NestedNets.held_subnets(executable_graph)
        return [
            Member(step, inner, rules[step.place])
            for step, inner in NestedNets.walk(executable_graph)
            if rules[step.place] != "passive" and (step.place, step.token) not in held and step.place not in still
        ]

    def held_subnets(executable_graph: ExecutableGraph) -> dict[str, str]:
        """The subnet places held still by a firing in flight: for each subnet place
        one of whose ports a transition in ``in_flight`` has an edge on (input or
        output, of any kind), the first such transition in declaration order."""
        in_flight = executable_graph.in_flight
        if not in_flight:
            return {}
        touching: dict[str, set[str]] = {}
        for edge in (
            *executable_graph.argument_edges, *executable_graph.return_edges,
            *executable_graph.snapshot_edges, *executable_graph.mutate_edges, *executable_graph.read_edges,
        ):
            if isinstance(edge.place_node_name, Port) and edge.transition_node_name in in_flight:
                touching.setdefault(edge.place_node_name.place, set()).add(edge.transition_node_name)
        held = {}
        for transition in executable_graph.transitions:
            for place, names in touching.items():
                if transition.name in names:
                    held.setdefault(place, transition.name)
        return held

    def turns(
        executable_graph: ExecutableGraph, part: Literal["ahead", "behind"], held: frozenset = frozenset(),
    ) -> list[Member]:
        """The members whose turn falls in one part of this net's round, in the
        order they are tried. ``"ahead"`` are the turns before the net's own: the
        inner-first members, then the ring's members after the cursor.
        ``"behind"`` are the turns after it: the rest of the ring, then the
        outer-first members, offered only while nothing of this net is in flight
        (until a body out here lands, this net may still have something to fire).

        The ring is this net and its take-turns members in walk order, entered
        after the cursor: after this net, every take-turns member comes before its
        own turn; after the member at ``k``, those after ``k`` come before it and
        those up to ``k`` after it; with no cursor, or one that names no member,
        all of them come after it."""
        members = NestedNets.members(executable_graph, held)
        ring = [member for member in members if member.stepping == "take_turns"]
        cursor = executable_graph._ring_cursor
        if cursor == executable_graph.net_id:
            start = 0
        else:
            start = next((at + 1 for at, member in enumerate(ring) if member.net.net_id == cursor), len(ring))
        if part == "ahead":
            return [member for member in members if member.stepping == "inner_first"] + ring[start:]
        later = [member for member in members if member.stepping == "outer_first"] if not executable_graph.in_flight else []
        return ring[:start] + later

    def took_own_turn(executable_graph: ExecutableGraph) -> None:
        """This net took its own turn in its ring: the next round starts after it.
        Its own choice counts as the turn, whatever comes of the firing."""
        executable_graph._ring_cursor = executable_graph.net_id

    def took_turn(executable_graph: ExecutableGraph, member: Member) -> None:
        """``member`` of this net took its turn and fired, or failed: the ring's
        cursor moves to it if it is in the ring, and stays where it is for a
        member of the other two bands."""
        if member.stepping == "take_turns":
            executable_graph._ring_cursor = member.net.net_id

    async def take_turn(
        executable_graph: ExecutableGraph,
        part: Literal["ahead", "behind"],
        executor=None,
        allow_token_copying: Optional[bool] = None,
        restore_tokens_on_failure: Optional[bool] = None,
        held: frozenset = frozenset(),
    ) -> Optional[InnerFiring]:
        """One firing among the turns of one part of this net's round (``turns``),
        never one of this net's own transitions: the first member whose deep step
        fires, or fails, is the step. Its events are drained into this net's log
        at once, the ring's cursor moves as ``took_turn`` says, and the firing is
        counted on this net (``count``) and returned as an ``InnerFiring``; a
        failure's ``TransitionFailedError`` propagates. ``None`` when no member of
        that part has anything to fire."""
        for member in NestedNets.turns(executable_graph, part, held):
            try:
                fired = await NestedNets.deep_step(
                    member.net, executable_graph.record_events, executor=executor,
                    allow_token_copying=allow_token_copying, restore_tokens_on_failure=restore_tokens_on_failure,
                )
            except TransitionFailedError:
                NestedNets.took_turn(executable_graph, member)
                NestedNets.drain(executable_graph)
                raise
            if fired is None:
                continue
            NestedNets.took_turn(executable_graph, member)
            NestedNets.drain(executable_graph)
            firing = NestedNets.below(member.step, fired)
            NestedNets.count(executable_graph, firing)
            return firing
        return None

    async def deep_step(
        inner: ExecutableGraph,
        record_events: bool,
        executor=None,
        allow_token_copying: Optional[bool] = None,
        restore_tokens_on_failure: Optional[bool] = None,
    ) -> Optional[Union[TransitionName, InnerFiring]]:
        """A member's turn: one deep step of ``inner``, by its own selector and the
        rules of its own places, recording events when the net around it does.
        What ``fire_one_transition`` with ``descend`` returns for it."""
        inner.record_events = record_events
        return await ExecutableGraphOperations.fire_one_transition(
            inner, *ExecutableGraphOperations.maps(inner),
            allow_token_copying=allow_token_copying,
            restore_tokens_on_failure=restore_tokens_on_failure,
            executor=executor,
        )

    def below(step: PathStep, fired: Union[TransitionName, InnerFiring]) -> InnerFiring:
        """What a deep step of the net at ``step`` fired, as the net above it tells
        it: one step longer."""
        if isinstance(fired, InnerFiring):
            return InnerFiring((step, *fired.path), fired.transition)
        return InnerFiring((step,), fired)

    def count(executable_graph: ExecutableGraph, firing: InnerFiring) -> None:
        """Count an inner firing as a firing of ``executable_graph`` under its
        ``InnerFiring``: what ``step_count``, ``last_fired`` and ``fired_counts``
        then say. Each net between counts it likewise, under the path from it."""
        executable_graph.step_count += 1
        executable_graph.last_fired = firing
        executable_graph.fired_counts[firing] = executable_graph.fired_counts.get(firing, 0) + 1

    def can_fire(executable_graph: ExecutableGraph) -> bool:
        """Whether one of this net's own transitions can fire now."""
        place_nodes, incoming, _outgoing, read = ExecutableGraphOperations.maps(executable_graph)
        return any(
            ExecutableGraphCheck.transition_is_fireable(t, executable_graph, incoming, place_nodes, read)
            for t in executable_graph.transitions
        )

    def any_fireable(
        executable_graph: ExecutableGraph,
        part: Optional[Literal["ahead", "behind"]] = None,
        held: frozenset = frozenset(),
    ) -> bool:
        """Whether a net that deep stepping enters below this one, at any depth, has
        a transition that can fire now (passive places left out); with ``part``,
        only the members of that part of this net's round (``turns``) and the
        nets below them."""
        members = NestedNets.members(executable_graph, held) if part is None else NestedNets.turns(
            executable_graph, part, held,
        )
        return any(NestedNets.can_fire(member.net) or NestedNets.any_fireable(member.net) for member in members)

    # --- reaching inside, for outer bodies ---

    def token_place(executable_graph: ExecutableGraph, place: PlaceKey) -> ListPlaceNode:
        """The place of ``executable_graph`` that holds tokens under ``place``: one
        of its own by name, or a port of one of its subnet places by the pair.
        ``KeyError`` naming it otherwise, a subnet place named alone included,
        since it holds no tokens of its own."""
        node = executable_graph.place_named(place)
        if isinstance(node, SubnetPlaceNode):
            raise KeyError(
                f"{place!r} is a subnet place, which holds no tokens of its own; name one of its ports, "
                f"Port({place!r}, port)"
            )
        if node is None:
            raise KeyError(f"the net has no place {place!r}")
        return node

    def put(inner: ExecutableGraph, place_name: PlaceKey, tokens: list) -> list[int]:
        """Append ``tokens`` to a place of an inner net, type-checked, with fresh
        ids, recording ``Added`` for each; returns the ids. ``place_name`` may be a
        port of one of the inner net's subnet places, ``Port(...)``: the inner net
        tells the change, naming the pair, and the subnet mints the ids. A closed
        net takes tokens from outside only at its ports: any other place, a port
        pair included, is refused with ``ClosedNetError`` and nothing is put
        (``ClosedNets``, since 2026-11-23)."""
        place = NestedNets.token_place(inner, place_name)
        ClosedNets.ensure_tokens(inner, place_name, "a put")
        TokenIds.resync(inner)
        for token in tokens:
            ExecutableGraphCheck.ensure_resident_token_type_matches_place_type(token, place)
        ids = TokenIds.append(TokenIds.minter(inner, place_name), place, list(tokens))
        for token_id in ids:
            GraphEvents.record(inner, Added(place_name, token_id))
        return ids

    def take(inner: ExecutableGraph, place_name: PlaceKey, count: int = 1) -> list:
        """Remove and return the ``count`` oldest tokens of a place of an inner net,
        recording ``Removed`` for each. Fewer than ``count`` present is an error
        (nothing is taken). A port is named by its pair, as for ``put``, and a
        closed net gives tokens up only at its ports, as ``put`` takes them."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"count must be a non-negative integer, got {count!r}")
        place = NestedNets.token_place(inner, place_name)
        ClosedNets.ensure_tokens(inner, place_name, "a take")
        TokenIds.resync(inner)
        if len(place.tokens) < count:
            raise ValueError(f"{place_name!r} holds {len(place.tokens)} tokens, not {count}")
        taken, ids = place.tokens[:count], place._token_ids[:count]
        del place.tokens[:count]
        del place._token_ids[:count]
        for token_id in ids:
            GraphEvents.record(inner, Removed(place_name, token_id))
        return taken

    def set_param(inner: ExecutableGraph, name: str, value: Any) -> None:
        """``NetParams.set`` on an inner net, refused to a body holding a net under
        the rule (``HeldByBodyError``)."""
        NetParams.set(inner, name, value)

    @contextmanager
    def subnet_scope(executable_graph: ExecutableGraph, place: str) -> Iterator[ExecutableGraph]:
        """``with NestedNets.subnet_scope(world, "Level 3") as level:`` the program's
        way to step or change the subnet of a subnet place: the block gets the
        subnet, and whatever it does there is told by the subnet, its events lifted
        into ``executable_graph``'s log as the block ends. Refused with
        ``SubnetHeldError`` while a firing of ``executable_graph`` on one of the
        subnet's ports is in flight, since the subnet is held still until it lands;
        ``LookupError`` if ``place`` is no subnet place. ``ExecutableGraph.subnet``
        reads a subnet at any time.

        The block is outside the subnet (``ClosedNets``, since 2026-11-23): a closed
        subnet takes tokens only at its ports. Called on a net a body holds, the
        block holds the subnet for the body too, so the subnet's knobs are the
        body's to read and not to set when it is under the rule, and what the block
        did there that the rule refuses fails the body's firing when it returns."""
        subnet = executable_graph.subnet(place)
        if subnet is None:
            raise LookupError(f"{place!r} is no subnet place of the net")
        holder = NestedNets.held_subnets(executable_graph).get(place)
        if holder is not None:
            raise SubnetHeldError(place, holder)
        subnet.record_events = executable_graph.record_events
        holds = ClosedNets.pass_on(executable_graph, subnet)
        try:
            yield subnet
        finally:
            ClosedNets.hand_back(executable_graph, holds)
            NestedNets.drain(executable_graph)

    def token_count(executable_graph: ExecutableGraph) -> int:
        """The number of tokens in a net's own places, its ports included; a subnet
        place holds none of its own, and the tokens of its subnet are the subnet's
        to count."""
        return sum(len(place.tokens) for place in executable_graph.places if isinstance(place, ListPlaceNode))

    def holds_nets(token: Any) -> bool:
        """Whether ``token`` is a net or implements the nested-nets protocol."""
        return callable(getattr(token, NESTED_NETS_METHOD, None))

    def summary(token: Any, maximum_length: int = 100) -> str:
        """A one-line summary of a nested token for a label: its own fields, with
        each inner net reduced to its name and token count (``Vehicle(name='Bus
        1', kind='Bus', destination=None) ⊂ cabin: 3``), so a renderer can mark
        a token that contains a net without printing the net. A token that is a
        net itself reads ``net: 3``. Cut at ``maximum_length`` with ``...``."""
        nets = NestedNets.of(token)
        counts = ", ".join(f"{name or 'net'}: {NestedNets.token_count(inner)}" for name, inner in nets.items())
        if isinstance(token, ExecutableGraph):
            text = counts
        else:
            if isinstance(token, BaseModel):
                fields = ", ".join(
                    f"{name}={value!r}" for name, value in token
                    if not isinstance(value, ExecutableGraph)
                )
                own = f"{type(token).__name__}({fields})"
            else:
                own = type(token).__name__
            text = f"{own} \u2282 {counts}"
        if len(text) > maximum_length:
            text = text[:maximum_length] + "..."
        return text

    def fork(token: Any) -> Any:
        """A copy of a nested token whose inner nets are new nets: a deep copy,
        which gives every graph in it its own ``net_id`` (same markings,
        parameters and switches; nothing shared with the original). The clone of
        a nested token the design asks for, done by copying rather than by
        rebuilding from the template and importing the marking."""
        return deepcopy(token)


# This is intended as shorthand for the common case when adding a transition with output(s) to a graph.
def function_transition_node_and_output_edges(
    *,
    name: str,
    function: Callable,
    output_place_names: list[PlaceNodeName],
    kwargs: Optional[KwArgs] = None,
    output_distribution_function: Optional[Callable[[Any], dict[PlaceNodeName, Any]]] = None,
    use_return_indices: bool = False,
) -> Sequence[FunctionTransitionNode | ReturnedEdgeFromTransition]:
    transition = FunctionTransitionNode(
        name=name, function=function, kwargs=kwargs, output_distribution_function=output_distribution_function
    )
    output_edges = []
    if use_return_indices:
        for i, place_name in enumerate(output_place_names):
            output_edges.append(
                ReturnedEdgeFromTransition(transition_node_name=name, place_node_name=place_name, return_index=i)
            )
    else:
        for place_name in output_place_names:
            output_edges.append(ReturnedEdgeFromTransition(transition_node_name=name, place_node_name=place_name))
    return (transition, *output_edges)


def function_transition_node_and_output_places(
    *,
    name: str,
    function: Callable,
    output_place_names_and_types: list[tuple[PlaceNodeName, Any]],
    kwargs: Optional[KwArgs] = None,
    output_distribution_function: Optional[Callable[[Any], dict[PlaceNodeName, Any]]] = None,
    use_return_indices: bool = False,
) -> Sequence[FunctionTransitionNode | ReturnedEdgeFromTransition | ListPlaceNode]:
    transition = FunctionTransitionNode(
        name=name, function=function, kwargs=kwargs, output_distribution_function=output_distribution_function
    )
    output_edges = []
    output_places = []
    if use_return_indices:
        for i, (place_name, place_type) in enumerate(output_place_names_and_types):
            output_edges.append(
                ReturnedEdgeFromTransition(transition_node_name=name, place_node_name=place_name, return_index=i)
            )
            output_places.append(ListPlaceNode(name=place_name, type=place_type))
    else:
        for place_name, place_type in output_place_names_and_types:
            output_edges.append(ReturnedEdgeFromTransition(transition_node_name=name, place_node_name=place_name))
            output_places.append(ListPlaceNode(name=place_name, type=place_type))
    return (transition, *output_edges, *output_places)


class MapPlaceNames:

    def to_list_place_nodes(executable_graph: ExecutableGraph) -> dict[PlaceKey, ListPlaceNode]:
        """Every place this net's transitions can reach, by its ``PlaceKey``, in
        declaration order: a place of its own under its name, and in a subnet
        place's position each of its ports, in the subnet's order, under
        ``Port(subnet place, port)``. A port's node is the subnet's own place, the
        one list of tokens both nets use; it keeps its own name, so it never
        collides with a place of this net that has the same one. A subnet place
        itself is not listed: it holds no tokens."""
        list_place_nodes: dict[PlaceKey, ListPlaceNode] = {}
        for node in executable_graph.places:
            if isinstance(node, ListPlaceNode):
                list_place_nodes[node.name] = node
            elif isinstance(node, SubnetPlaceNode):
                for port in node.ports:
                    list_place_nodes[Port(node.name, port.name)] = port
            else:
                raise ValueError(f"Unexpected node type: {type(node)}")
        return list_place_nodes


class MapTransitionNames:

    def to_function_transition_nodes(executable_graph: ExecutableGraph) -> dict[str, FunctionTransitionNode]:
        function_nodes = {}
        for node in executable_graph.transitions:
            if isinstance(node, FunctionTransitionNode):
                function_nodes[node.name] = node
            else:
                raise ValueError(f"Unexpected node type: {type(node)}")
        return function_nodes

    def to_incoming_edges(executable_graph: ExecutableGraph) -> dict[str, tuple[ArgumentEdgeToTransition, ...]]:
        incoming_edges = {}
        for edge_to in executable_graph.argument_edges:
            if isinstance(edge_to, ArgumentEdgeToTransition):
                if edge_to.transition_node_name in incoming_edges:
                    incoming_edges[edge_to.transition_node_name] += (edge_to,)
                else:
                    incoming_edges[edge_to.transition_node_name] = (edge_to,)
            else:
                raise ValueError(f"Unexpected node type: {type(edge_to)}")
        return incoming_edges

    def to_outgoing_edges(executable_graph: ExecutableGraph) -> dict[str, tuple[ReturnedEdgeFromTransition, ...]]:
        outgoing_edges = {}
        for edge_from in executable_graph.return_edges:
            if isinstance(edge_from, ReturnedEdgeFromTransition):
                if edge_from.transition_node_name in outgoing_edges:
                    outgoing_edges[edge_from.transition_node_name] += (edge_from,)
                else:
                    outgoing_edges[edge_from.transition_node_name] = (edge_from,)
            else:
                raise ValueError(f"Unexpected node type: {type(edge_from)}")
        return outgoing_edges

    def to_read_edges(
        executable_graph: ExecutableGraph,
    ) -> dict[str, tuple[Union["SnapshotEdge", "MutateEdge", "ReadEdge"], ...]]:
        """Map each transition to its non-consuming edges: its snapshot and survey
        edges, its mutate and mutate-where edges, then its read, read-where and
        read-all edges. The function and the engine's
        ``transition_names_to_read_edges`` keep the name they had when this text
        called the group read edges; a read edge is now one of the three kinds."""
        non_consuming: dict[str, tuple] = {}
        for edge in (*executable_graph.snapshot_edges, *executable_graph.mutate_edges, *executable_graph.read_edges):
            non_consuming[edge.transition_node_name] = non_consuming.get(edge.transition_node_name, ()) + (edge,)
        return non_consuming


class KeyedEdges:
    """Functions over keyed edges (``TakeWhereEdge``, ``MutateWhereEdge``,
    ``ReadWhereEdge``).

    **Bound edges** (since 2026-11-23). A keyed edge whose filter holds a clause
    with ``input`` (``Clause(key, input=place, input_key=key)``) chooses its token
    by the token its transition gets from that place, its **source**: the
    courier an order names. The source edge gives one token (a take, take-where,
    snapshot, mutate, mutate-where, read or read-where edge), and several edges
    may be bound to one source; ``ExecutableGraphCheck.ensure_input_clauses_are_sound``
    refuses the rest when the net is built.

    **Which pair fires**: for each source, the oldest source token that has a
    match, with the oldest match for it. A source's candidates are its place's
    tokens in queue order, only those its own filter keeps for a keyed source. A
    candidate needs a match in every edge bound to it: the oldest token of the
    bound place that the bound edge's whole filter keeps, each input clause
    comparing the bound token's ``key`` with the candidate's ``input_key`` as
    key values compare (same type and equal). A candidate with no match waits
    and does not hold back those behind it, and the sources of one transition
    choose independently. ``pairing`` finds the pair; fireability asks it and
    launch takes what it gives, before any token moves."""

    def first_kept(
        edge: Union[TakeWhereEdge, MutateWhereEdge, ReadWhereEdge], place: ListPlaceNode, params: dict,
    ) -> Optional[int]:
        """The index of the oldest token of ``place`` the edge's filter keeps, or ``None``.
        The filter sees the place as the edge names it, so on a port its ``places``
        names the port by its ``Port`` pair."""
        for index, token in enumerate(place.tokens):
            if edge.where.keeps(place, token, params, edge.place_node_name):
                return index
        return None

    def input_clauses(edge: Any) -> tuple:
        """The clauses of an edge's filter that read the token of another input,
        in declared order; empty for an edge that is not bound."""
        if not isinstance(edge, (TakeWhereEdge, MutateWhereEdge, ReadWhereEdge)):
            return ()
        return tuple(clause for clause in edge.where.clauses if clause.input is not None)

    def source_of(edge: Any) -> Optional[PlaceKey]:
        """The place a bound edge's input clauses name, its source, or ``None``
        for an edge that is not bound. A built net's bound edge names one."""
        clauses = KeyedEdges.input_clauses(edge)
        return clauses[0].input if clauses else None

    def pairing(
        transition: FunctionTransitionNode,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[PlaceKey, ListPlaceNode],
        transition_names_to_read_edges: Optional[dict[str, tuple]],
        params: dict,
    ) -> Optional[dict[ArgumentName, int]]:
        """The index, in its place as it stands, of the token each source and each
        bound edge of ``transition`` gets, by argument; ``{}`` for a transition
        with no bound edge, and ``None`` when a source has no token with a match
        in every edge bound to it.

        One pass over each bound place builds a lookup from the values its input
        clauses compare to the first index whose other clauses pass; then each
        source token tried costs one lookup per bound edge, never a scan."""
        edges = (
            *transition_names_to_incoming_edges.get(transition.name, ()),
            *(transition_names_to_read_edges or {}).get(transition.name, ()),
        )
        bound = [edge for edge in edges if KeyedEdges.input_clauses(edge)]
        if not bound:
            return {}
        by_source: dict[PlaceKey, list] = {}
        for edge in bound:
            by_source.setdefault(KeyedEdges.source_of(edge), []).append(edge)
        chosen: dict[ArgumentName, int] = {}
        for source, bound_to_it in by_source.items():
            source_edge = next((edge for edge in edges if edge.place_node_name == source), None)
            found = KeyedEdges._pair(source, source_edge, bound_to_it, place_names_to_nodes, params)
            if found is None:
                return None
            chosen.update(found)
        return chosen

    def _pair(
        source: PlaceKey, source_edge: Any, bound: list, place_names_to_nodes: dict, params: dict,
    ) -> Optional[dict[ArgumentName, int]]:
        """The oldest token of ``source`` with a match in every edge of ``bound``,
        and the match in each, by argument; ``None`` if there is none."""
        from petritype.core.keys import Keys  # the module imports this one

        lookups = [KeyedEdges._lookup(edge, place_names_to_nodes[edge.place_node_name], params) for edge in bound]
        if any(not lookup for _clauses, lookup in lookups):
            return None
        place = place_names_to_nodes[source]
        own = source_edge.where if isinstance(source_edge, (TakeWhereEdge, MutateWhereEdge, ReadWhereEdge)) else None
        for index, token in enumerate(place.tokens):
            if own is not None and not own.keeps(place, token, params, source):
                continue
            keys = Keys.of(place, token)
            matched = []
            for clauses, lookup in lookups:
                match = lookup.get(tuple(_typed(keys[clause.input_key]) for clause in clauses))
                if match is None:
                    break
                matched.append(match)
            else:
                chosen = {edge.argument: match for edge, match in zip(bound, matched)}
                if source_edge is not None:
                    chosen[source_edge.argument] = index
                return chosen
        return None

    def _lookup(edge: Any, place: ListPlaceNode, params: dict) -> tuple[tuple, dict[tuple, int]]:
        """A bound edge's input clauses, and the lookup one pass over its place
        builds: from the values of those clauses' keys, each as ``(type, value)``
        so that ``1`` is not ``True``, to the index of the oldest token whose
        other clauses pass, within the places the filter covers."""
        from petritype.core.keys import Keys  # the module imports this one

        clauses = KeyedEdges.input_clauses(edge)
        others = tuple(clause for clause in edge.where.clauses if getattr(clause, "input", None) is None)
        lookup: dict[tuple, int] = {}
        if not edge.where.covers(edge.place_node_name) or not place.keys:
            return clauses, lookup
        for index, token in enumerate(place.tokens):
            keys = Keys.of(place, token)
            if all(clause.accepts(keys, params) for clause in others):
                lookup.setdefault(tuple(_typed(keys[clause.key]) for clause in clauses), index)
        return clauses, lookup


def _typed(value: Any) -> tuple[type, Any]:
    """A key value with its type, so that values compare as keys do: ``1`` is not ``True``."""
    return type(value), value


class ExecutableGraphCheck:
    """Functions that do not alter the executable graph."""

    def ensure_input_clauses_are_sound(executable_graph: ExecutableGraph) -> None:
        """Raise ``ValueError`` for a bound edge (``KeyedEdges``) the net cannot
        fire as declared, naming the transition and the places. Checked when the
        graph is built, for each bound edge in declaration order:

        - its input clauses name one place, its source;
        - the source is one of its transition's input places;
        - neither the bound edge's place nor the source is read by another edge
          of the transition, so that the source is one edge and the positions the
          pairing chooses stay true until each edge acts on them;
        - the source's edge is not bound itself, so no chain and no cycle (an
          edge that names its own place is its own source, and bound);
        - the source's edge gives one token: a take, take-where, snapshot, mutate
          or mutate-where edge whose argument is not annotated ``list[T]``, or a
          read or read-where edge (since 2026-11-23), which always gives one;
        - the bound edge's place declares each clause's key, and the source's
          place each ``input_key``.

        Constant and parameter clauses are left as they were: a key the place
        does not declare matches nothing there."""
        places = MapPlaceNames.to_list_place_nodes(executable_graph)
        functions = {transition.name: transition.function for transition in executable_graph.transitions}
        edges_of: dict[str, list] = {}
        for edge in (*executable_graph.argument_edges, *executable_graph.snapshot_edges,
                     *executable_graph.mutate_edges, *executable_graph.read_edges):
            edges_of.setdefault(edge.transition_node_name, []).append(edge)
        for transition_name, edges in edges_of.items():
            bound = [edge for edge in edges if KeyedEdges.input_clauses(edge)]
            if not bound:
                continue
            readers: dict[PlaceKey, list] = {}
            for edge in edges:
                readers.setdefault(edge.place_node_name, []).append(edge)
            for edge in bound:
                here = edge.place_node_name
                clauses = KeyedEdges.input_clauses(edge)
                sources = list(dict.fromkeys(clause.input for clause in clauses))
                chooses = f"transition {transition_name!r} chooses its token from {_named(here)}"
                if len(sources) > 1:
                    raise ValueError(f"{chooses} by two inputs, {_named(sources[0])} and {_named(sources[1])}")
                source = sources[0]
                if source not in readers:
                    raise ValueError(f"{chooses} by {_named(source)}, which it does not read")
                for key in (here, source):
                    if len(readers[key]) > 1:
                        raise ValueError(
                            f"transition {transition_name!r} reads {_named(key)} through more than one edge, and a "
                            f"token there chooses or is chosen by another input"
                        )
                source_edge = readers[source][0]
                if KeyedEdges.input_clauses(source_edge):
                    raise ValueError(f"{chooses} by {_named(source)}, whose own token is chosen by an input")
                if ExecutableGraphCheck.gives_every_token(source_edge, places[source], functions[transition_name]):
                    raise ValueError(f"{chooses} by {_named(source)}, whose edge gives every token rather than one")
                for clause in clauses:
                    for key, place in ((clause.key, here), (clause.input_key, source)):
                        if key not in places[place].key_names:
                            raise ValueError(f"{chooses} by key {key!r}, which {_named(place)} does not declare")

    def gives_every_token(edge: Any, place: ListPlaceNode, function: Callable) -> bool:
        """Whether an input edge hands its body every token of its place as one
        list rather than one token: a drain, a survey, a read-all, or a take,
        snapshot or mutate edge whose argument is annotated ``list[T]`` for the
        place's ``T``, by the test stage 1 applies. A keyed edge and a read edge
        give one token."""
        if isinstance(edge, (DrainEdge, SurveyEdge, ReadAllEdge)):
            return True
        if isinstance(edge, (TakeWhereEdge, MutateWhereEdge, ReadEdge)):
            return False
        argument_type = _function_type_hints(function).get(edge.argument)
        return ExecutableGraphCheck.argument_takes_every_token(argument_type, place.type)

    def ensure_read_edges_stand_alone(executable_graph: ExecutableGraph) -> None:
        """Raise ``ValueError`` for a read edge (``ReadEdge``, ``ReadWhereEdge``,
        ``ReadAllEdge``) whose place another input edge of the same transition
        names, of any kind, a second read edge included, naming the transition and
        the place. Beside a take from the same place a read edge would hand the
        body a token that is gone, or the wrong one; beside a snapshot or a mutate
        edge, a token the other edge copies or changes.
        The Rust core refuses every place a transition reads twice; this library
        refuses it where a read edge is one of the two. A place the transition
        produces into is no input, and may be the place it reads."""
        inputs: dict[tuple[str, PlaceKey], int] = {}
        for edge in (
            *executable_graph.argument_edges, *executable_graph.snapshot_edges, *executable_graph.mutate_edges,
            *executable_graph.read_edges,
        ):
            key = (edge.transition_node_name, edge.place_node_name)
            inputs[key] = inputs.get(key, 0) + 1
        for edge in executable_graph.read_edges:
            if inputs[(edge.transition_node_name, edge.place_node_name)] > 1:
                raise ValueError(
                    f"transition {edge.transition_node_name!r} reads {_named(edge.place_node_name)} through a "
                    f"{type(edge).__name__} and another input edge; a place a read edge reads is read by no other "
                    f"input edge of its transition"
                )

    def ensure_reaches_are_sound(executable_graph: ExecutableGraph) -> None:
        """Raise ``ValueError`` for an input edge whose ``reaches`` (since
        2026-11-23) is given on a place that holds no nets, a place not declared
        ``nested`` (a port of a subnet place by that port's own ``nested``), or
        names a port by the empty string; the message names the transition and the
        place. These are the only refusals: whether a token's net has the ports an
        edge names is checked neither here nor when the edge fires, since the nets
        arrive in tokens as the net runs, so ``reaches`` is a declaration for
        readers and drawings and changes nothing about firing."""
        for edge in (
            *executable_graph.argument_edges, *executable_graph.snapshot_edges, *executable_graph.mutate_edges,
            *executable_graph.read_edges,
        ):
            if not edge.reaches:
                continue
            place = executable_graph.place_named(edge.place_node_name)
            if not (isinstance(place, ListPlaceNode) and place.nested):
                raise ValueError(
                    f"the {type(edge).__name__} from {_named(edge.place_node_name)} to transition "
                    f"{edge.transition_node_name!r} reaches {list(edge.reaches)}, but its place holds no nets: "
                    f"reaches names ports of the nets a nested place's tokens carry"
                )
            if any(not name for name in edge.reaches):
                raise ValueError(
                    f"the {type(edge).__name__} from {_named(edge.place_node_name)} to transition "
                    f"{edge.transition_node_name!r} reaches a port named by the empty string"
                )

    def requires_presence(edge: Union[ArgumentEdgeToTransition, SnapshotEdge, MutateEdge, ReadEdge]) -> bool:
        """Whether an input edge holds its transition back until its place has a
        token: true for every edge but the three that read every token, zero or
        more (``DrainEdge``, ``SurveyEdge``, ``ReadAllEdge``), which accept an
        empty place."""
        return not isinstance(edge, (DrainEdge, SurveyEdge, ReadAllEdge))

    def argument_takes_every_token(argument_type: Any, place_type: Any) -> bool:
        """Whether an argument annotated ``argument_type`` receives every token of a
        place of ``place_type`` as one list: ``list[T]`` for the place's ``T``, the
        test stage 1 applies to a plain argument edge and to a snapshot or mutate
        edge."""
        return get_origin(argument_type) is list and CompareTypes.between_annotations_where_one_maybe_in_list(
            annotation_not_in_list=place_type,
            annotation_maybe_in_list=argument_type,
        )

    def ensure_argument_takes_every_token(
        edge: Union[DrainEdge, SurveyEdge, ReadAllEdge],
        place: ListPlaceNode,
        transition: FunctionTransitionNode,
        argument_type: Any,
    ) -> None:
        """Raise ``TypeError`` unless the argument of a ``DrainEdge``, ``SurveyEdge``
        or ``ReadAllEdge`` is annotated ``list[T]`` for its place's ``T``: the edge
        hands the body every token, possibly none, and a plain ``T`` has nothing to
        receive from an empty place."""
        if ExecutableGraphCheck.argument_takes_every_token(argument_type, place.type):
            return
        given = "no annotation" if argument_type is None else _spelt(argument_type)
        raise TypeError(
            f"{type(edge).__name__} from place '{place.name}' to transition '{transition.name}': "
            f"argument '{edge.argument}' must be annotated list[{_spelt(place.type)}], since the edge "
            f"hands the body every token of the place, possibly none; it has {given}."
        )

    def ensure_argument_takes_one_token(
        edge: Union[ReadEdge, ReadWhereEdge],
        place: ListPlaceNode,
        transition: FunctionTransitionNode,
        argument_type: Any,
    ) -> None:
        """Raise ``TypeError`` unless the argument of a ``ReadEdge`` or
        ``ReadWhereEdge`` is annotated as its place's type, or not at all: the edge
        hands the body one token, a list only when the place holds lists, and
        ``ReadAllEdge`` is the edge that hands over every token."""
        if argument_type is None or CompareTypes.between_annotations(place.type, argument_type):
            return
        raise TypeError(
            f"{type(edge).__name__} from {_place_words(edge.place_node_name)} to transition '{transition.name}': "
            f"argument '{edge.argument}' must be annotated {_spelt(place.type)}, the place's type, since the edge "
            f"hands the body one token (a ReadAllEdge hands over every token); it has {_spelt(argument_type)}."
        )

    def sufficient_tokens_are_available(
        transition: FunctionTransitionNode,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        transition_names_to_read_edges: Optional[dict[str, tuple]] = None,
        executable_graph: Optional[ExecutableGraph] = None,
    ) -> bool:
        """Every input place non-empty, and, for a keyed edge (``TakeWhereEdge``,
        ``MutateWhereEdge``, ``ReadWhereEdge``), holding a token its filter keeps;
        ``executable_graph`` supplies the parameters a filter may name (without it
        a keyed edge is judged by presence alone). A ``DrainEdge``, ``SurveyEdge``
        or ``ReadAllEdge`` asks nothing of its place (``requires_presence``): it
        reads every token, zero or more, so an empty place never holds its
        transition back; a guard still can.

        A bound edge, whose filter reads the token of another input, is judged with
        its source: every source must have a token with a match in each edge bound
        to it (``KeyedEdges.pairing``), asked after every edge on its own."""
        # Transitions with no incoming edges (generators) are always ready to fire
        return ExecutableGraphOperations.plan_stage_1(
            transition=transition,
            transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            place_names_to_nodes=place_names_to_nodes,
            transition_names_to_read_edges=transition_names_to_read_edges,
            net_params=executable_graph.params if executable_graph is not None else None,
            check_filters=executable_graph is not None,
        ) is not None

    def transition_is_fireable(
        transition: FunctionTransitionNode,
        executable_graph: ExecutableGraph,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        transition_names_to_read_edges: Optional[dict[str, tuple]] = None,
    ) -> bool:
        """Can fire now: not switched off on the graph (``disabled``), sufficient
        tokens, and the transition's guard, if any.

        Every fireable-discovery sweep (sequential execution and both concurrent runner
        loops) must go through this, so a guard means the same thing in every execution
        mode. Guards run on every sweep — they should be cheap, side-effect-free
        predicates over the marking.
        """
        if transition.name in executable_graph.disabled:
            return False
        if not ExecutableGraphCheck.sufficient_tokens_are_available(
            transition=transition,
            transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            place_names_to_nodes=place_names_to_nodes,
            transition_names_to_read_edges=transition_names_to_read_edges,
            executable_graph=executable_graph,
        ):
            return False
        return transition.guard is None or bool(transition.guard(executable_graph))

    def transition_is_enabled(
        transition: FunctionTransitionNode,
        executable_graph: ExecutableGraph,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        transition_names_to_read_edges: Optional[dict[str, tuple]] = None,
    ) -> bool:
        """Deprecated alias of ``transition_is_fireable``; warns and delegates.

        Renamed so that "enabled" means only the on/off switch (``Enable`` / ``Disable``).
        """
        warnings.warn(
            "ExecutableGraphCheck.transition_is_enabled is deprecated: use "
            "ExecutableGraphCheck.transition_is_fireable instead (\"enabled\" now means "
            "only the Enable/Disable switch).",
            DeprecationWarning,
            stacklevel=2,
        )
        return ExecutableGraphCheck.transition_is_fireable(
            transition=transition,
            executable_graph=executable_graph,
            transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            place_names_to_nodes=place_names_to_nodes,
            transition_names_to_read_edges=transition_names_to_read_edges,
        )

    def all_return_indices_are_none(outgoing_edges: tuple[ReturnedEdgeFromTransition, ...]) -> bool:
        for edge in outgoing_edges:
            if edge.return_index is not None:
                return False
        return True

    def all_return_indices_are_integers(outgoing_edges: tuple[ReturnedEdgeFromTransition, ...]) -> bool:
        for edge in outgoing_edges:
            if not isinstance(edge.return_index, int):
                return False
        return True

    def value_disposition_for_place(value: Any, place: ListPlaceNode) -> Literal["token", "batch", "nothing"]:
        """The single point of truth for what a value means at a destination place.

        The interpretation depends on the (value, place-type) pair:
        - "token": the value is one token. Always the case when the place's own
          type is a list type (there the list *is* the token — an empty list is
          a real token), and for any non-list value.
        - "batch": a list value at a scalar-typed place is a batch of tokens,
          deposited element-wise.
        - "nothing": an empty batch; nothing is deposited.

        Routing (value_and_places_types_match), deposit-time validation
        (ensure_token_type_matches_place_type), stage-2 output arbitration
        (only non-"nothing" destinations count toward ambiguity) and the
        stage-3 deposit (add_tokens_to_places) all derive their behaviour
        from this classification — change it here and nowhere else.

        Deliberately NOT a client: the pre-flight sweep over tokens already
        resting in places (ensure_resident_token_type_matches_place_type). A
        resident value is always exactly one token, never a batch, so it is
        validated whole regardless of this rule.
        """
        place_type_is_a_list_type = place.type is list or get_origin(place.type) is list
        if isinstance(value, list) and not place_type_is_a_list_type:
            return "batch" if value else "nothing"
        return "token"

    def value_can_be_deposited(value: Any, place: ListPlaceNode) -> bool:
        """Whether the value, interpreted per its disposition, type-checks at the place."""
        disposition = ExecutableGraphCheck.value_disposition_for_place(value, place)
        if disposition == "nothing":
            return True
        if disposition == "batch":
            return all(CompareTypes.between_value_and_type(item, place.type) for item in value)
        return CompareTypes.between_value_and_type(value, place.type)

    def ensure_token_type_matches_place_type(token: any, place: ListPlaceNode):
        disposition = ExecutableGraphCheck.value_disposition_for_place(token, place)
        if disposition == "batch":
            for item in token:
                if not CompareTypes.between_value_and_type(item, place.type):
                    raise TypeError(
                        f"Expected token item to be of type {place.type} in {place.name}, got {type(item)}."
                        f"\nToken item: {item}"
                    )
                if place.nested:
                    NestedNets.ensure_nested_token(item, place)
        elif disposition == "token":
            if not CompareTypes.between_value_and_type(token, place.type):
                raise TypeError(
                    f"Expected token to be of type {place.type} in {place.name}, got {type(token)}."
                    f"\nToken: {token}"
                )
            if place.nested:
                NestedNets.ensure_nested_token(token, place)
        # "nothing" (an empty batch) is always acceptable — it deposits no tokens.

    def ensure_resident_token_type_matches_place_type(token: any, place: ListPlaceNode):
        """Validate a token *already resting* in a place — always as one whole token.

        Deliberately does NOT go through value_disposition_for_place. That rule is
        about deposits: a list arriving at a scalar place is a batch to unpack. But a
        value already in a place is by definition exactly one token, so 'batch' is a
        category error here. This mirrors ListPlaceNode.check_type_matches_tokens, the
        place's own construction validator, so the pre-flight sweep and construction
        agree; only post-construction mutation can produce a token they must reject.
        """
        if not CompareTypes.between_value_and_type(token, place.type):
            raise TypeError(
                f"Expected token to be of type {place.type} in {place.name}, got {type(token)}."
                f"\nToken: {token}"
            )
        if place.nested:
            NestedNets.ensure_nested_token(token, place)

    def ensure_all_token_types_match_place_types(executable_graph: ExecutableGraph):
        """Every token in a place this net's transitions reach, its subnets' ports
        included, fits its place."""
        for holder in executable_graph.places:
            for place in holder.ports if isinstance(holder, SubnetPlaceNode) else (holder,):
                for token in place.tokens:
                    ExecutableGraphCheck.ensure_resident_token_type_matches_place_type(token, place)

    def return_indices_are_a_mix_of_none_and_non_none(outgoing_edges: tuple[ReturnedEdgeFromTransition, ...]) -> bool:
        return (
            not ExecutableGraphCheck.all_return_indices_are_none(outgoing_edges)
            and not ExecutableGraphCheck.all_return_indices_are_integers(outgoing_edges)
        )

    def value_and_places_types_match(value: Any, places: Iterable[ListPlaceNode]) -> tuple[ListPlaceNode, ...]:
        """Find places where the value could be deposited.

        A place matches iff the value type-checks under its disposition there
        (see value_disposition_for_place): whole-token at list-typed places,
        element-wise batch at scalar-typed places. At run time we can not
        distinguish the intended type of an empty list's contents, so an empty
        list — an empty batch, or an empty token at a list-typed place —
        matches every place. Stage 2 arbitrates multiple matches: only
        destinations where the deposit is observable (disposition other than
        "nothing") count toward ambiguity, and genuine ambiguity requires
        allow_token_copying.
        """
        return tuple(
            place for place in places
            if ExecutableGraphCheck.value_can_be_deposited(value, place)
        )

    def assert_acyclic(executable_graph: ExecutableGraph) -> None:
        """Raise ValueError if the net's token flow contains a cycle, naming its path
        (``P1 → T1 → P2 → T2 → P1``).

        Token flow is the directed bipartite graph of ``argument_edges``
        (place → transition) and ``return_edges`` (transition → place). The
        non-consuming edges are excluded: a snapshot or a read edge moves no
        tokens, so a cycle through one cannot feed itself; mutate edges are
        excluded on the same conservative grounds (they modify tokens in place but
        move none).

        Drain edges (``DrainEdge``) are excluded too, by hand, although a drain is
        an argument edge: it takes every token, zero or more, so it moves nothing
        guaranteed, and a loop closed only through a drain cannot keep itself
        going. Each round needs the draining transition to fire, and that firing
        is paid for by a token it takes through another edge (or the transition is
        a source, which ``assert_no_source_transitions`` names). A transition
        that takes a signal token and drains a queue, with another that puts what
        it let through back into the queue, closes a loop through the drain; every
        round costs a signal token, so the net still quiesces, and this check
        passes it. The traffic light (``examples/edges_on_empty_places/traffic_light.py``)
        drains its waiting cars while the light is green and is rejected all the
        same, for its light: ``Change`` takes the light from ``Light`` and puts its
        next colour back, through a take edge, a cycle by design.

        Passing proves **no token cycles** — not termination. A transition with no
        ``ArgumentEdgeToTransition`` input other than drains (a source — possibly
        fed only by non-consuming edges and drains, which may consume nothing) can fire
        forever in a perfectly acyclic net. The static termination condition is
        acyclic *and* every transition consumes: pair this with
        ``assert_no_source_transitions``. Nets that legitimately run forever should
        be bounded at run time instead (``RunContext.error_after_n_firings``).
        """
        # Nodes are namespaced (kind, name): unique-name enforcement is per kind,
        # so a place and a transition may legally share a name.
        adjacency: dict[tuple, list[tuple]] = {}
        for edge in executable_graph.argument_edges:
            if isinstance(edge, DrainEdge):
                continue  # moves nothing guaranteed (see above)
            adjacency.setdefault(("place", edge.place_node_name), []).append(
                ("transition", edge.transition_node_name)
            )
        for edge in executable_graph.return_edges:
            adjacency.setdefault(("transition", edge.transition_node_name), []).append(
                ("place", edge.place_node_name)
            )

        # Iterative DFS — a long pipeline must not hit the recursion limit.
        state: dict[tuple, str] = {}  # "visiting" (on the current path) or "done"
        for start in list(adjacency):
            if start in state:
                continue
            state[start] = "visiting"
            path = [start]
            stack = [iter(adjacency.get(start, ()))]
            while stack:
                successor = next(stack[-1], None)
                if successor is None:
                    state[path.pop()] = "done"
                    stack.pop()
                elif state.get(successor) == "visiting":
                    cycle = path[path.index(successor):] + [successor]
                    raise ValueError(
                        "Token flow contains a cycle: "
                        + " → ".join(name if isinstance(name, str) else repr(name) for _, name in cycle)
                    )
                elif successor not in state:
                    state[successor] = "visiting"
                    path.append(successor)
                    stack.append(iter(adjacency.get(successor, ())))

    def assert_no_source_transitions(executable_graph: ExecutableGraph) -> None:
        """Raise ValueError if any transition has no ``ArgumentEdgeToTransition``
        input, naming the offenders. Such a *source* transition consumes nothing
        when it fires (non-consuming edges don't count — snapshots, mutates and
        reads leave the place untouched), so it can fire forever and even an acyclic net
        containing one never quiesces. Together with ``assert_acyclic`` this is a
        static termination proof: tokens only flow forward, and every firing
        consumes at least one.

        A ``DrainEdge`` does not count either, although it is an argument edge: it
        takes every token, zero or more, so it may consume nothing, and a
        transition whose only argument edge is a drain is a source. The traffic
        light's ``Go`` (``examples/edges_on_empty_places/traffic_light.py``) drains
        ``Waiting`` and only reads ``Light``, so this check names it: unguarded, it
        would fire at every green taking nothing. Its guard, green and someone
        waiting, is what makes it safe, which the check cannot see, so that example
        does not call it.

        Deliberate sources (generators, external-input transitions) are
        legitimate — don't assert this on such nets; bound them at run time with
        ``RunContext.error_after_n_firings`` or drive them via
        ``run_indefinitely``.
        """
        consuming = {
            edge.transition_node_name for edge in executable_graph.argument_edges
            if not isinstance(edge, DrainEdge)
        }
        sources = [t.name for t in executable_graph.transitions if t.name not in consuming]
        if sources:
            raise ValueError(
                "Source transitions (no ArgumentEdgeToTransition input other than a DrainEdge — "
                f"firing may consume nothing, so the net cannot quiesce): {', '.join(sources)}"
            )


# Default selector: highest `priority` wins; transitions without one score 0.0.
# `max` keeps the first maximum, so ties — including nets where nobody sets a
# priority — go to the first-defined transition; for `tie_break="last_declared"`
# we scan the fireable list reversed, so the last-defined one is that first maximum;
# for `tie_break="round_robin"` the first of the top-priority ones declared after
# the cursor (the last pick), wrapping round.
def default_transition_selector(
    graph: ExecutableGraph, fireable: list[FunctionTransitionNode]
) -> Optional[FunctionTransitionNode]:
    """Default selector: highest-priority fireable transition, ties broken by ``graph.tie_break``.
    ``fireable`` is in declaration order. Every pick, under any tie-break, moves the
    graph's cursor (``_last_pick``), which nothing else moves and only
    ``"round_robin"`` reads, so a tie-break switched to round robin mid-run resumes
    after the last pick."""
    if not fireable:
        return None
    if graph.tie_break == "round_robin":
        chosen = _round_robin(graph, fireable)
    else:
        candidates = reversed(fireable) if graph.tie_break == "last_declared" else fireable
        chosen = max(candidates, key=lambda t: t.priority(graph) if t.priority is not None else 0.0)
    graph._last_pick = next((position for position, t in enumerate(graph.transitions) if t.name == chosen.name), None)
    return chosen


def _round_robin(graph: ExecutableGraph, fireable: list[FunctionTransitionNode]) -> FunctionTransitionNode:
    """The first top-priority fireable transition declared after the cursor,
    wrapping to the first; the first declared when there is no cursor yet."""
    scores = [t.priority(graph) if t.priority is not None else 0.0 for t in fireable]
    top = max(scores)
    index = {t.name: position for position, t in enumerate(graph.transitions)}
    candidates = [t for t, score in zip(fireable, scores) if score == top]
    candidates.sort(key=lambda t: index.get(t.name, -1))
    cursor = graph._last_pick
    chosen = None
    if cursor is not None:
        chosen = next((t for t in candidates if index.get(t.name, -1) > cursor), None)
    return chosen if chosen is not None else candidates[0]


def _a_move_of_its_own(function):
    """A firing of a net is a move of its own: around it, ``ClosedNets.moving``
    tells what a body holding the net changed before it from what the net did
    itself. A firing with a selector of the caller's own, on a net a body holds,
    is firing by name, refused to that body (``HeldByBodyError``)."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    async def moved(*args, **kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        graph = bound.arguments["executable_graph"]
        if graph._holds and bound.arguments.get("transition_selector") is not None:
            ClosedNets.ensure_controls(graph, "a firing chosen by a selector of the caller's own")
        with ClosedNets.moving(graph):
            return await function(*args, **kwargs)

    return moved


@dataclass(frozen=True)
class PlannedIncoming:
    """One incoming (consuming) edge's planned extraction from its place."""

    edge: ArgumentEdgeToTransition
    place: ListPlaceNode
    indices: tuple[int, ...]
    takes_every: bool


@dataclass(frozen=True)
class PlannedRead:
    """One non-consuming edge's planned selection from its place."""

    edge: Union["SnapshotEdge", "MutateEdge", "ReadEdge"]
    place: ListPlaceNode
    indices: tuple[int, ...]
    takes_every: bool


@dataclass(frozen=True)
class Stage1Plan:
    """A complete, validated, pre-mutation plan for stage 1 of a transition firing.

    Computed without mutating any place so that fireability checks, concurrent
    per-token locking, handover prediction, and atomic extraction all agree on
    the exact indices and tokens selected.
    """

    incoming: tuple[PlannedIncoming, ...]
    reads: tuple[PlannedRead, ...]


def _first_unconsumed_index(place: ListPlaceNode, already: set[int]) -> Optional[int]:
    for index in range(len(place.tokens)):
        if index not in already:
            return index
    return None


def _first_kept_unconsumed_index(
    edge: Union[TakeWhereEdge, MutateWhereEdge, ReadWhereEdge],
    place: ListPlaceNode,
    params: dict,
    already: set[int],
) -> Optional[int]:
    for index, token in enumerate(place.tokens):
        if index not in already and edge.where.keeps(place, token, params, edge.place_node_name):
            return index
    return None


class ExecutableGraphOperations:
    """Functions that alter the executable graph.

    ## Transitions Algorithm

    ### 1. Pick next transition to execute.
    - Iterate over transition nodes.
    - If there are no more transitions that can fire, end the algorithm.
    - Given a transition node, check if it can fire.
        - Are there sufficient input tokens?

    ### 2. Fire the transition.
    - Remove input tokens from places.
    - Call transition function with the input tokens to generate the output tokens.
    - Add output tokens (append?).
    """

    def construct_graph(
        mixed_nodes_and_edges: Iterable[
            Union[
                ListPlaceNode, SubnetPlaceNode, FunctionTransitionNode, ArgumentEdgeToTransition,
                ReturnedEdgeFromTransition, SnapshotEdge, MutateEdge, ReadEdge,
            ]
        ],
        allow_token_copying: bool = False,
        restore_tokens_on_failure: bool = False,
        expect_acyclic: bool = False,
        tie_break: Literal["first_declared", "last_declared", "round_robin"] = "first_declared",
        record_events: bool = False,
        params: Optional[dict[str, Any]] = None,
        template: Optional[str] = None,
        open: bool = False,
    ) -> ExecutableGraph:
        """The net of the given nodes and edges, places (subnet places among them)
        in the order given, which is their declaration order. ``open=True``
        declares the net open (``ExecutableGraph.open``, since 2026-11-23): a net
        that marks a port is then written from outside at any of its places."""
        places, transitions, edges_to, edges_from = [], [], [], []
        snapshot_edges, mutate_edges, read_edges = [], [], []
        for node in mixed_nodes_and_edges:
            if isinstance(node, (ListPlaceNode, SubnetPlaceNode)):
                places.append(node)
            elif isinstance(node, FunctionTransitionNode):
                transitions.append(node)
            elif isinstance(node, ArgumentEdgeToTransition):
                edges_to.append(node)
            elif isinstance(node, ReturnedEdgeFromTransition):
                edges_from.append(node)
            elif isinstance(node, SnapshotEdge):
                snapshot_edges.append(node)
            elif isinstance(node, MutateEdge):
                mutate_edges.append(node)
            elif isinstance(node, ReadEdge):
                read_edges.append(node)
            else:
                raise ValueError(f"Unexpected node type: {type(node)}")
        graph = ExecutableGraph(
            places=places, transitions=transitions, argument_edges=edges_to, return_edges=edges_from,
            snapshot_edges=snapshot_edges, mutate_edges=mutate_edges, read_edges=read_edges,
            allow_token_copying=allow_token_copying,
            restore_tokens_on_failure=restore_tokens_on_failure, tie_break=tie_break,
            record_events=record_events, params=dict(params or {}), template=template, open=open,
        )
        # Declares "this net is a DAG" at build time; an accidental cycle then
        # fails here with its path named instead of hanging at run time.
        if expect_acyclic:
            ExecutableGraphCheck.assert_acyclic(graph)
        return graph

    def maps(executable_graph: ExecutableGraph) -> tuple[dict, dict, dict, dict]:
        """The four adjacency maps ``fire_one_transition`` fires on:
        ``(place_nodes, incoming, outgoing, read)``. No validation. Cached per
        graph topology."""
        executable_graph._ensure_caches()
        if executable_graph._maps_cache is None:
            executable_graph._maps_cache = (
                MapPlaceNames.to_list_place_nodes(executable_graph),
                MapTransitionNames.to_incoming_edges(executable_graph),
                MapTransitionNames.to_outgoing_edges(executable_graph),
                MapTransitionNames.to_read_edges(executable_graph),
            )
        return executable_graph._maps_cache

    def update_output_place_with_result_tokens(result: Any, place: ListPlaceNode) -> None:
        """Update the given place by appending the result token to its tokens list."""
        place.tokens.append(result)

    def plan_stage_1(
        transition: FunctionTransitionNode,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[PlaceKey, ListPlaceNode],
        transition_names_to_read_edges: Optional[dict[str, tuple]] = None,
        net_params: Optional[dict[str, Any]] = None,
        check_filters: bool = True,
    ) -> Optional[Stage1Plan]:
        """Compute the exact token indices every incoming and non-consuming edge of
        ``transition`` will consume or read, without mutating any place.

        Returns ``None`` if any edge lacks sufficient or matching unconsumed tokens
        (including when multiple input edges share a place or a bound pairing has no
        match). When ``check_filters`` is ``False``, keyed edges are checked by
        presence alone (used by legacy ``sufficient_tokens_are_available`` calls
        without a graph).
        """
        incoming_edges: tuple[ArgumentEdgeToTransition, ...] = transition_names_to_incoming_edges.get(
            transition.name, tuple()
        )
        read_edges: tuple = (transition_names_to_read_edges or {}).get(transition.name, ())
        params = net_params if net_params is not None else {}

        if check_filters:
            chosen = KeyedEdges.pairing(
                transition, transition_names_to_incoming_edges, place_names_to_nodes,
                transition_names_to_read_edges, params,
            )
            if chosen is None:
                return None
        else:
            chosen = {}

        type_hints: Optional[dict[str, Any]] = None

        def arg_type(arg_name: str) -> Any:
            nonlocal type_hints
            if type_hints is None:
                type_hints = _function_type_hints(transition.function)
            return type_hints.get(arg_name)

        consumed_by_place: dict[PlaceKey, set[int]] = {}
        planned_incoming: list[PlannedIncoming] = []

        for edge in incoming_edges:
            place = place_names_to_nodes[edge.place_node_name]
            already = consumed_by_place.setdefault(edge.place_node_name, set())

            if edge.argument in chosen:
                index = chosen[edge.argument]
                if index in already or index >= len(place.tokens):
                    return None
                already.add(index)
                planned_incoming.append(PlannedIncoming(edge, place, (index,), False))
                continue

            if isinstance(edge, TakeWhereEdge):
                if check_filters:
                    index = _first_kept_unconsumed_index(edge, place, params, already)
                else:
                    index = _first_unconsumed_index(place, already)
                if index is None:
                    return None
                already.add(index)
                planned_incoming.append(PlannedIncoming(edge, place, (index,), False))
                continue

            takes_every = isinstance(edge, DrainEdge) or ExecutableGraphCheck.argument_takes_every_token(
                arg_type(edge.argument), place.type,
            )
            if takes_every:
                remaining = tuple(i for i in range(len(place.tokens)) if i not in already)
                if ExecutableGraphCheck.requires_presence(edge) and not remaining:
                    return None
                already.update(remaining)
                planned_incoming.append(PlannedIncoming(edge, place, remaining, True))
            else:
                index = _first_unconsumed_index(place, already)
                if index is None:
                    return None
                already.add(index)
                planned_incoming.append(PlannedIncoming(edge, place, (index,), False))

        planned_reads: list[PlannedRead] = []
        for edge in read_edges:
            place = place_names_to_nodes[edge.place_node_name]
            already = consumed_by_place.get(edge.place_node_name, set())

            if isinstance(edge, ReadAllEdge):
                remaining = tuple(i for i in range(len(place.tokens)) if i not in already)
                planned_reads.append(PlannedRead(edge, place, remaining, True))
                continue

            if isinstance(edge, ReadEdge):
                if edge.argument in chosen:
                    index = chosen[edge.argument]
                    if index in already or index >= len(place.tokens):
                        return None
                elif isinstance(edge, ReadWhereEdge) and check_filters:
                    index = _first_kept_unconsumed_index(edge, place, params, already)
                    if index is None:
                        return None
                else:
                    index = _first_unconsumed_index(place, already)
                    if index is None:
                        return None
                planned_reads.append(PlannedRead(edge, place, (index,), False))
                continue

            if edge.argument in chosen:
                index = chosen[edge.argument]
                if index in already or index >= len(place.tokens):
                    return None
                planned_reads.append(PlannedRead(edge, place, (index,), False))
                continue

            if isinstance(edge, MutateWhereEdge):
                if check_filters:
                    index = _first_kept_unconsumed_index(edge, place, params, already)
                else:
                    index = _first_unconsumed_index(place, already)
                if index is None:
                    return None
                planned_reads.append(PlannedRead(edge, place, (index,), False))
                continue

            takes_every = isinstance(edge, SurveyEdge) or ExecutableGraphCheck.argument_takes_every_token(
                arg_type(edge.argument), place.type,
            )
            if takes_every:
                remaining = tuple(i for i in range(len(place.tokens)) if i not in already)
                if ExecutableGraphCheck.requires_presence(edge) and not remaining:
                    return None
                planned_reads.append(PlannedRead(edge, place, remaining, True))
            else:
                index = _first_unconsumed_index(place, already)
                if index is None:
                    return None
                planned_reads.append(PlannedRead(edge, place, (index,), False))

        return Stage1Plan(incoming=tuple(planned_incoming), reads=tuple(planned_reads))

    def stage_1_extract_argument_tokens_from_places(
        transition: FunctionTransitionNode,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        allow_token_copying: bool = False,
        # place_history_length: int = 1,
        token_history_length: int = 0,
        transition_names_to_read_edges: Optional[dict[str, tuple]] = None,
        firing_ids: Optional[FiringTokenIds] = None,
        net_params: Optional[dict[str, Any]] = None,
        plan: Optional[Stage1Plan] = None,
        record_input_places: bool = True,
    ) -> tuple[dict[ArgumentName, any], Sequence[ListPlaceNode]]:
        """Remove input tokens from source places atomically.

        All selections are validated via ``plan_stage_1`` before any place is mutated,
        so a missing or unmatched token never leaves places partially drained.
        """
        if plan is None:
            plan = ExecutableGraphOperations.plan_stage_1(
                transition=transition,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                place_names_to_nodes=place_names_to_nodes,
                transition_names_to_read_edges=transition_names_to_read_edges,
                net_params=net_params,
                check_filters=True,
            )
        if plan is None:
            raise ValueError(f"insufficient or unmatched tokens for transition {transition.name!r}")

        input_edge_names_to_tokens: dict[ArgumentName, any] = {}
        input_places = []
        lent: list = []

        # Collect incoming tokens & ids by original index BEFORE mutating any place.
        place_removals: dict[int, tuple[ListPlaceNode, set[int]]] = {}
        incoming_extracted: list[tuple[PlannedIncoming, Any, list[int], bool]] = []
        incoming_count_per_place: dict[int, int] = {}
        for item in plan.incoming:
            pid = id(item.place)
            incoming_count_per_place[pid] = incoming_count_per_place.get(pid, 0) + 1

        for item in plan.incoming:
            place = item.place
            pid = id(place)
            whole_take = (
                item.takes_every
                and incoming_count_per_place[pid] == 1
                and len(item.indices) == len(place.tokens)
            )
            if whole_take:
                value: Any = place.tokens
                taken_ids = list(place._token_ids)
            elif item.takes_every:
                value = [place.tokens[i] for i in item.indices]
                taken_ids = [place._token_ids[i] for i in item.indices if i < len(place._token_ids)]
            else:
                idx = item.indices[0]
                value = place.tokens[idx]
                taken_ids = place._token_ids[idx:idx + 1]
            incoming_extracted.append((item, value, taken_ids, whole_take))
            _, removed = place_removals.setdefault(pid, (place, set()))
            removed.update(item.indices)

        # Also snapshot single-token reads by original index before removing consumed tokens.
        single_reads: dict[int, tuple[Any, Optional[int]]] = {}
        for idx_pos, item in enumerate(plan.reads):
            if not item.takes_every:
                tok_idx = item.indices[0]
                tok = item.place.tokens[tok_idx]
                tok_id = item.place._token_ids[tok_idx] if tok_idx < len(item.place._token_ids) else None
                single_reads[idx_pos] = (tok, tok_id)

        # Apply removals atomically now that all picks are captured.
        whole_cleared: set[int] = {id(item.place) for item, _v, _ids, whole in incoming_extracted if whole}
        for pid, (place, removed_indices) in place_removals.items():
            if pid in whole_cleared:
                place.tokens = []
                place._token_ids = []
            elif removed_indices:
                for idx in sorted(removed_indices, reverse=True):
                    del place.tokens[idx]
                    if idx < len(place._token_ids):
                        del place._token_ids[idx]

        # Populate outputs, history copies, lent list, and firing_ids for incoming edges.
        for item, value, taken_ids, _whole in incoming_extracted:
            place = item.place
            edge = item.edge
            if record_input_places:
                place_copy = place.copy_sans_tokens()
                input_places.append(place_copy)
                if allow_token_copying and token_history_length >= 1:
                    if item.takes_every:
                        place_copy.tokens.extend(deepcopy(value))
                    else:
                        place_copy.tokens.append(deepcopy(value))
            input_edge_names_to_tokens[edge.argument] = value
            if item.takes_every:
                lent.extend(value)
            else:
                lent.append(value)
            if firing_ids is not None:
                firing_ids.consumed[edge.argument] = (edge.place_node_name, taken_ids)

        # Populate non-consuming edges.
        for idx_pos, item in enumerate(plan.reads):
            edge = item.edge
            place = item.place
            if isinstance(edge, ReadEdge):
                if isinstance(edge, ReadAllEdge):
                    value = list(place.tokens)
                    read_ids = list(place._token_ids)
                else:
                    value, tok_id = single_reads[idx_pos]
                    read_ids = [tok_id] if tok_id is not None else []
                if firing_ids is not None:
                    firing_ids.read.extend((edge.place_node_name, token_id) for token_id in read_ids)
                input_edge_names_to_tokens[edge.argument] = value
                lent.extend(value if isinstance(edge, ReadAllEdge) else (value,))
                continue

            if item.takes_every:
                value = place.tokens
                read_ids = list(place._token_ids)
                if firing_ids is not None and isinstance(edge, MutateEdge):
                    firing_ids.mutated.extend((edge.place_node_name, token_id) for token_id in read_ids)
                if isinstance(edge, MutateEdge):
                    lent.extend(value)
                input_edge_names_to_tokens[edge.argument] = (
                    deepcopy(value) if isinstance(edge, SnapshotEdge) else value
                )
            else:
                value, tok_id = single_reads[idx_pos]
                if isinstance(edge, MutateEdge):
                    if firing_ids is not None and tok_id is not None:
                        firing_ids.mutated.append((edge.place_node_name, tok_id))
                    lent.append(value)
                    input_edge_names_to_tokens[edge.argument] = value
                else:
                    input_edge_names_to_tokens[edge.argument] = deepcopy(value)

        if firing_ids is not None:
            ClosedNets.lend(firing_ids, lent)
        return input_edge_names_to_tokens, input_places

    def restore_argument_tokens_to_places(
        transition: FunctionTransitionNode,
        input_edge_names_to_tokens: dict[ArgumentName, any],
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        firing_ids: Optional[FiringTokenIds] = None,
    ) -> None:
        """Put the tokens consumed by stage 1 back into their source places.

        With ``firing_ids`` (as filled by stage 1) the tokens get their old ids back,
        at the front, in the order taken. Without it the ids are not restored and the
        graph's next ``TokenIds.resync`` re-ids those places.

        Used when restore_tokens_on_failure is enabled and a firing fails
        after extraction. Single tokens go back to the front of their place
        (they were consumed FIFO from the front) and whole-list extractions,
        a drain's included (possibly empty), are prepended, so the pre-firing
        order is restored. A token a keyed take, a source or a bound edge took
        from further back goes to the front too. This is
        conservation, not rollback: a body that mutated a token before
        failing leaves it mutated, and external side effects and in-place
        mutation via MutateEdge are not undone — which is why this is opt-in.
        """
        incoming_edges = transition_names_to_incoming_edges.get(transition.name, tuple())
        for edge in reversed(incoming_edges):
            if edge.argument not in input_edge_names_to_tokens:
                continue
            place = place_names_to_nodes[edge.place_node_name]
            argument_type = _function_type_hints(transition.function).get(edge.argument)
            # Mirror the extraction cases of stage 1 (a keyed take goes back to the front).
            if isinstance(edge, DrainEdge) or (
                not isinstance(edge, TakeWhereEdge)
                and ExecutableGraphCheck.argument_takes_every_token(argument_type, place.type)
            ):  # All tokens were extracted as a list.
                place.tokens[:0] = input_edge_names_to_tokens[edge.argument]
            else:  # A single token was popped from the front.
                place.tokens.insert(0, input_edge_names_to_tokens[edge.argument])
            if firing_ids is not None and edge.argument in firing_ids.consumed:
                place._token_ids[:0] = firing_ids.consumed[edge.argument][1]

    def handle_failed_firing(
        transition: FunctionTransitionNode,
        exception: Exception,
        input_edge_names_to_tokens: dict[ArgumentName, any],
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        restore_tokens_on_failure: bool,
        executable_graph: Optional[ExecutableGraph] = None,
        firing_ids: Optional[FiringTokenIds] = None,
    ) -> NoReturn:
        """Raise TransitionFailedError for a firing that failed after stage 1.

        The error carries the tokens consumed from input places (the arguments of
        non-consuming edges are not consumed, so they are excluded). By default the
        tokens are NOT put back: the body may have mutated them before
        failing, and silently restoring possibly-corrupt tokens to a place is
        worse than a visible loss. With restore_tokens_on_failure=True they
        are restored first (best effort).

        Given the graph and the firing's ids, the restored tokens get their old ids
        back and the firing's ``Mutated`` and ``Failed`` events are recorded.
        """
        incoming_edges = transition_names_to_incoming_edges.get(transition.name, tuple())
        consumed = {
            edge.argument: input_edge_names_to_tokens[edge.argument]
            for edge in incoming_edges
            if edge.argument in input_edge_names_to_tokens
        }
        restored = False
        if restore_tokens_on_failure:
            ExecutableGraphOperations.restore_argument_tokens_to_places(
                transition=transition,
                input_edge_names_to_tokens=input_edge_names_to_tokens,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                place_names_to_nodes=place_names_to_nodes,
                firing_ids=firing_ids,
            )
            restored = True
        if executable_graph is not None and firing_ids is not None:
            GraphEvents.record_failed(
                executable_graph, transition.name, firing_ids, place_names_to_nodes, restored, exception,
            )
        raise TransitionFailedError(transition.name, consumed, restored) from exception

    async def stage_2_call_transition_function(
        transition: FunctionTransitionNode,
        tokens_kwargs: dict[ArgumentName, any],
        transition_names_to_outgoing_edges: dict[str, tuple[ReturnedEdgeFromTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        allow_token_copying: bool = False,
        executor=None,
        net_params: Optional[dict[str, Any]] = None,
        firing_ids: Optional[FiringTokenIds] = None,
    ) -> dict[ListPlaceNode, Any]:
        """Call the transition function and match output tokens to destination places.

        Return a mapping of output places to the tokens to be added to them.

        A synchronous body marked ``execution="thread"`` is run via ``executor`` (a thread pool;
        ``None`` uses the default) so it does not block the event loop. Async bodies already
        yield and run inline regardless.

        ``net_params`` (the graph's ``params``) are passed as keyword arguments to a
        body that declares a parameter of the same name that neither an edge nor
        the transition's own ``kwargs`` supplies. A transition with no output edge
        is a sink: its body must return ``None`` and nothing is deposited.
        """
        if transition.kwargs is not None:
            merged_kwargs = SafeMerge.dictionaries(tokens_kwargs, transition.kwargs)
        else:
            merged_kwargs = dict(tokens_kwargs)
        if net_params:
            wanted = ExecutableGraphOperations._net_params_wanted(transition.function, net_params)
            for name in wanted:
                if name not in merged_kwargs:
                    merged_kwargs[name] = net_params[name]
        active_token = _ACTIVE_FIRING_IDS.set(firing_ids) if firing_ids is not None else None
        try:
            if inspect.iscoroutinefunction(transition.function):
                # Async bodies already yield; "execution" does not apply.
                result = await transition.function(**merged_kwargs)
            elif transition.execution == "thread":
                # Offload a blocking / CPU-bound sync body to a thread so it doesn't freeze the
                # event loop. Copy the contextvar context so ``ClosedNets.note`` scopes refusals
                # to this firing inside the worker thread too.
                loop = asyncio.get_running_loop()
                ctx_copy = contextvars.copy_context()
                result = await loop.run_in_executor(
                    executor, functools.partial(ctx_copy.run, transition.function, **merged_kwargs)
                )
            else:
                result = transition.function(**merged_kwargs)
        finally:
            if active_token is not None:
                _ACTIVE_FIRING_IDS.reset(active_token)
        output_place_names_to_tokens: dict[PlaceNodeName, Any] = dict()
        outgoing_edges: tuple[ReturnedEdgeFromTransition, ...] = transition_names_to_outgoing_edges.get(
            transition.name, ()
        )
        if not outgoing_edges and transition.output_distribution_function is None:
            # A sink: the body consumes (or reads) and produces nothing.
            if result is not None:
                raise ValueError(
                    f"Transition \"{transition.name}\" has no output edge but returned {type(result).__name__}; "
                    f"a transition without output edges must return None."
                )
        elif (
            transition.output_distribution_function is None
            and outgoing_edges
            and ExecutableGraphCheck.all_return_indices_are_integers(outgoing_edges)
        ):
            # Indexed routing: the result is a tuple and element i flows along
            # the edge with return_index == i.
            if not isinstance(result, tuple):
                raise TypeError(
                    f"Transition \"{transition.name}\" has indexed return edges but returned "
                    f"{type(result)} instead of a tuple."
                )
            for edge in outgoing_edges:
                if not 0 <= edge.return_index < len(result):
                    raise ValueError(
                        f"Return edge to place \"{edge.place_node_name}\" expects element "
                        f"{edge.return_index} of transition \"{transition.name}\"'s result, "
                        f"which has only {len(result)} elements."
                    )
                output_place_names_to_tokens[edge.place_node_name] = result[edge.return_index]
        elif transition.output_distribution_function is None:
            # Use place types to determine where tokens should go. Each candidate
            # keeps the key its edge names it by, since two ports of different
            # subnet places may share a name.
            potential_output_places: list[tuple[PlaceKey, ListPlaceNode]] = [
                (edge.place_node_name, place_names_to_nodes[edge.place_node_name]) for edge in outgoing_edges
            ]
            # What value_and_places_types_match decides, kept with the keys.
            matching_places = [
                (key, place) for key, place in potential_output_places
                if ExecutableGraphCheck.value_can_be_deposited(result, place)
            ]
            # An empty list type-matches every place but observably deposits
            # only at list-typed ones, so ambiguity is judged over the places
            # where depositing the value actually does something. For any
            # other value every match is observable and this filter is a
            # no-op.
            observable_places = [
                (key, place) for key, place in matching_places
                if ExecutableGraphCheck.value_disposition_for_place(result, place) != "nothing"
            ]
            if len(observable_places) > 1 and not allow_token_copying:
                # Multiple observable destinations but token copying is not allowed.
                raise ValueError(
                    "There are multiple matching destination place nodes but token copying is not allowed. "
                    "Expected only a single output place. To allow the same token to be copied to multiple places, "
                    "set the `allow_token_copying` parameter to True."
                )
            elif len(observable_places) > 1:
                # The token can be copied to each observable destination.
                # Note: actual copying will be handled by add_tokens_to_places
                for key, _place in observable_places:
                    output_place_names_to_tokens[key] = result
            elif len(observable_places) == 1:
                # There is a single observable destination.
                output_place_names_to_tokens[observable_places[0][0]] = result
            elif not matching_places:
                # No matching places found.
                raise ValueError(
                    f"No output place of transition \"{transition.name}\" matches its result "
                    f"of type {type(result)}."
                )
            # else: the value matched only as a no-op (an empty batch with
            # only scalar-typed candidates) — the firing succeeds and
            # deposits nothing.
        else:  # TODO: create and test separate functions for these two branches.
            # Use the given output distribution function to determine where the tokens should go.
            if not ExecutableGraphCheck.all_return_indices_are_none(outgoing_edges):
                raise ValueError(
                    "Expected all return indices to be None when an output distribution function is used but this is"
                    f" not the case for transition \"{transition.name}\" and outgoing_edges:\n{outgoing_edges}."
                )
            destination_place_names_to_tokens = transition.output_distribution_function(result)
            if not destination_place_names_to_tokens:
                raise ValueError(
                    "Unexpected branch: no output places found for the result of the transition."
                )
            # Token copying (and rejection of multi-place reuse when copying is
            # off) is enforced later in add_tokens_to_places, so the number of
            # destinations and the allow_token_copying flag don't change what we
            # do here: place each (place_name -> token) the distributor produced.
            # A distributor names a port by its pair, as an edge does.
            for place_name, token in destination_place_names_to_tokens.items():
                destination_place = place_names_to_nodes[place_name]
                ExecutableGraphCheck.ensure_token_type_matches_place_type(token, destination_place)
                if token is not None:
                    output_place_names_to_tokens[place_name] = token
        return output_place_names_to_tokens

    def _net_params_wanted(function: Callable, net_params: dict[str, Any]) -> tuple[str, ...]:
        """The net parameters ``function`` declares a parameter for (by name)."""
        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            return ()
        return tuple(name for name in net_params if name in parameters)

    def add_tokens_to_places(  # Stage 3
        output_place_names_to_tokens: dict[PlaceNodeName, Any],
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        allow_token_copying: bool = False,
        check_types: bool = True,
        executable_graph: Optional[ExecutableGraph] = None,
        firing_ids: Optional[FiringTokenIds] = None,
    ) -> dict[PlaceNodeName, ListPlaceNode]:
        """Distribute the output tokens to the corresponding places.

        Every deposited token is a new token with a fresh id from
        ``executable_graph`` (a copy made under token copying included), recorded on
        ``firing_ids.produced`` if given; a token deposited into a port, a key that is
        a ``Port`` pair, gets its id from the port's subnet (``TokenIds.minter``).
        Without a graph no ids are assigned and the graph's next ``TokenIds.resync``
        re-ids the places that changed.

        Handle token copying if required.
        Handle updating ListPlaceNode with either a single token or a list of tokens.
        Handle updating multiple place nodes with the same token if copying is allowed.
        Handle updating multiple place nodes with different tokens.
        """
        updated_places = {}
        token_usage = {}  # Maps id(token) -> list of place_names where it's used
        need_to_copy = False

        # First pass: track token usage
        for place_name, token in output_place_names_to_tokens.items():
            if token is None:
                continue  # Skip None tokens
            token_id = id(token)
            if token_id not in token_usage:
                token_usage[token_id] = []
            token_usage[token_id].append(place_name)

        # Second pass: add tokens to places
        for place_name, token in output_place_names_to_tokens.items():
            if token is None:
                continue  # Skip None tokens
            place = place_names_to_nodes[place_name]
            token_id = id(token)

            # Determine if we need to copy this token
            if len(token_usage[token_id]) > 1:
                if allow_token_copying:
                    need_to_copy = True
                else:
                    raise RuntimeError(
                        "Token is being added to multiple places but token copying is not allowed.\n"
                        f"Token: {token}, used in places: {token_usage[token_id]}"
                    )

            # Get the token to add (copy if needed and not the first usage)
            if need_to_copy and token_usage[token_id].index(place_name) > 0:
                token_or_list_to_add = deepcopy(token)
            else:
                token_or_list_to_add = token

            # Deposit per the value's disposition at this place — batch values
            # extend element-wise, single tokens (including a whole list at a
            # list-typed place) append, an empty batch deposits nothing.
            if check_types:
                ExecutableGraphCheck.ensure_token_type_matches_place_type(token_or_list_to_add, place)
            disposition = ExecutableGraphCheck.value_disposition_for_place(token_or_list_to_add, place)
            if disposition == "batch":
                deposited = list(token_or_list_to_add)
            elif disposition == "token":
                deposited = [token_or_list_to_add]
            else:
                deposited = []
            if executable_graph is None:
                place.tokens.extend(deposited)
            else:
                # A port's ids are its subnet's, whichever net deposits.
                ids = TokenIds.append(TokenIds.minter(executable_graph, place_name), place, deposited)
                if firing_ids is not None:
                    firing_ids.produced.extend((place_name, token_id) for token_id in ids)
            updated_places[place_name] = place
        return updated_places

    def deposit_and_record(
        executable_graph: ExecutableGraph,
        transition: FunctionTransitionNode,
        output_place_names_to_tokens: dict[PlaceNodeName, Any],
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        firing_ids: FiringTokenIds,
        duration_ms: float,
        allow_token_copying: bool = False,
    ) -> dict[PlaceNodeName, ListPlaceNode]:
        """Stage 3 for a firing whose body returned, then its events: ``Mutated``s and
        ``Fired`` on success. A deposit that raises (a token that does not fit its
        place) ends the firing as ``Failed`` (consumed tokens not restored; anything
        already deposited reported as ``Added``) and the error propagates unchanged.
        Shared by sequential firing and the concurrent runner's completion paths, so
        both report every firing exactly once."""
        try:
            updated_places = ExecutableGraphOperations.add_tokens_to_places(
                output_place_names_to_tokens=output_place_names_to_tokens,
                place_names_to_nodes=place_names_to_nodes,
                allow_token_copying=allow_token_copying,
                executable_graph=executable_graph,
                firing_ids=firing_ids,
            )
        except Exception as exception:
            GraphEvents.record_failed(
                executable_graph, transition.name, firing_ids, place_names_to_nodes,
                restored=False, exception=exception,
            )
            raise
        GraphEvents.record_fired(
            executable_graph, transition.name, firing_ids, place_names_to_nodes, duration_ms,
        )
        return updated_places

    @_a_move_of_its_own
    async def fire_one_transition(
        executable_graph: ExecutableGraph,
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        transition_names_to_outgoing_edges: dict[str, tuple[ReturnedEdgeFromTransition, ...]],
        transition_names_to_read_edges: dict[str, tuple],
        transition_selector: Optional[Callable[[ExecutableGraph, list[FunctionTransitionNode]], Optional[FunctionTransitionNode]]] = None,
        allow_token_copying: Optional[bool] = None,
        restore_tokens_on_failure: Optional[bool] = None,
        executor=None,
        transition_history_length=1,
        place_history_length=1,
        token_history_length=0,
        descend: bool = True,
        on_launch: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Optional[Union[TransitionName, InnerFiring]]:
        """Fire at most one fireable transition, using prebuilt adjacency maps.

        The single-firing primitive that ``execute_graph`` and the Runner's
        sequential loops compose. The caller owns the per-run setup: building the
        four maps (``MapPlaceNames`` / ``MapTransitionNames``) and validating the
        marking (``ensure_all_token_types_match_place_types``) at whatever cadence
        it wants — so driving an N-firing net pays for that setup once, not once
        per firing. ``last_fired`` is *not* reset here; resetting it per call is
        ``execute_graph``'s contract.

        Selector resolution matches ``execute_graph``: provided > graph's >
        default (highest priority, ties broken by the graph's ``tie_break``). ``None`` for
        ``allow_token_copying`` / ``restore_tokens_on_failure`` means use the
        graph's setting.

        Returns the fired transition's name, or None if no transition was fireable
        (or the selector declined every fireable one).

        Token ids move with the tokens (``TokenIds``); places whose tokens were
        assigned directly are resynced first. With ``record_events`` on, a firing
        records its ``Mutated`` events then one ``Fired`` (or ``Failed``).

        ``on_launch``, if given, is awaited once when one of this net's own
        transitions fires, after its inputs are consumed and ``in_flight`` names it
        and before its body runs; the Runner notifies its observers there. A firing
        inside an inner net, reached by descent, awaits nothing. An ``on_launch``
        that raises fails the firing as a raising body does, with a
        ``LaunchNotificationError`` carrying its exception: the consumed tokens are
        restored when ``restore_tokens_on_failure`` says so, ``Failed`` is recorded
        and ``TransitionFailedError`` is raised.

        Nested tokens and subnet places (``NestedNets``): the undrained events of
        every inner net are gathered into this graph's log first, so a firing that
        consumes a token loses none of them. With ``descend`` on, the call is one
        deep step, in the order the stepping rules of this net's nested and subnet
        places give: the turns ahead of this net's own (the inner-first places, then
        the ring's members after its cursor), this net's own choice, then the turns
        behind it (the rest of the ring, then the outer-first places); passive
        places are never entered, and neither is a subnet while a transition of this
        net with an edge on one of its ports is in flight. A firing of this net that
        takes from a port or puts into one is told by this net, naming the port by
        its ``Port`` pair, and the tokens it puts there get the subnet's ids.
        A firing inside an inner net is returned as an ``InnerFiring``, the path to
        the net it fired in and the transition's own name, and counted on this
        graph under it (``step_count``, ``last_fired``, ``fired_counts``); its
        events are in this graph's log by the time this returns.
        ``transition_selector`` and ``on_launch`` are for this net's own turn only,
        and that turn moves the ring's cursor to this net when the selector
        chooses, whatever comes of the firing. With ``descend`` off the call is a
        plain step: this net's own transitions only, the cursor untouched.

        Closed nets (``ClosedNets``, since 2026-11-23): the nets the firing hands its
        body are held until the body returns, and a refusal while they were held,
        or a private place of a closed one found edited, fails the firing as a
        raising body does, the error as the ``TransitionFailedError``'s cause. On a
        net a body holds, the call is the net's own move (a body may step a net it
        holds), and a ``transition_selector`` of the caller's is refused to that
        body when the net is under the rule.
        """
        TokenIds.resync(executable_graph)
        NestedNets.drain(executable_graph)
        if allow_token_copying is None:
            allow_token_copying = executable_graph.allow_token_copying
        if restore_tokens_on_failure is None:
            restore_tokens_on_failure = executable_graph.restore_tokens_on_failure
        selector = (
            transition_selector
            or executable_graph.transition_selector
            or default_transition_selector
        )
        deep = descend and NestedNets.any_nested(executable_graph)

        if deep:
            # The turns ahead of this net's own: inner first, then the ring after its cursor.
            fired_inside = await NestedNets.take_turn(
                executable_graph, "ahead", executor=executor,
                allow_token_copying=allow_token_copying, restore_tokens_on_failure=restore_tokens_on_failure,
            )
            if fired_inside is not None:
                return fired_inside

        # Get all fireable transitions (sufficient tokens and a passing guard), in
        # definition order.
        fireable_transitions = []
        for transition in executable_graph.transitions:
            if ExecutableGraphCheck.transition_is_fireable(
                transition=transition,
                executable_graph=executable_graph,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                place_names_to_nodes=place_names_to_nodes,
                transition_names_to_read_edges=transition_names_to_read_edges,
            ):
                fireable_transitions.append(transition)

        # Let selector choose which transition to fire
        transition = selector(executable_graph, fireable_transitions)
        if transition is None:
            if not deep:
                return None
            # The turns behind it: the rest of the ring, then outer first.
            return await NestedNets.take_turn(
                executable_graph, "behind", executor=executor,
                allow_token_copying=allow_token_copying, restore_tokens_on_failure=restore_tokens_on_failure,
            )
        if transition not in fireable_transitions:
            raise ValueError(
                f"transition_selector returned {transition!r}, which is not among the fireable transitions "
                f"{[t.name for t in fireable_transitions]}"
            )
        if descend:
            # This net took its turn in its ring, whatever comes of the firing.
            NestedNets.took_own_turn(executable_graph)

        firing_ids = FiringTokenIds()
        input_args_to_tokens, input_places = ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
            transition=transition,
            transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            place_names_to_nodes=place_names_to_nodes,
            allow_token_copying=allow_token_copying,
            token_history_length=token_history_length,
            transition_names_to_read_edges=transition_names_to_read_edges,
            firing_ids=firing_ids,
            net_params=executable_graph.params,
            record_input_places=place_history_length > 0,
        )
        # Mark the chosen transition as running while its body executes, as the
        # concurrent runner does, so an observer on another thread (a heartbeat
        # reading the graph during a long synchronous body) can name it. Each
        # write assigns a fresh set: replacing the reference is atomic, whereas
        # mutating the set in place could be seen half-done.
        executable_graph.in_flight = {transition.name}
        try:
            if on_launch is not None:
                # The launch notification. Inside the outer ``try`` so that an
                # observer that raises fails the firing as a raising body does.
                try:
                    await on_launch()
                except Exception as error:
                    raise LaunchNotificationError(transition.name, error) from error
            # The clock starts after the notification: ``duration_ms`` is the body's.
            started = time.perf_counter()
            output_place_names_to_tokens = await ExecutableGraphOperations.stage_2_call_transition_function(
                transition=transition,
                tokens_kwargs=input_args_to_tokens,
                transition_names_to_outgoing_edges=transition_names_to_outgoing_edges,
                place_names_to_nodes=place_names_to_nodes,
                allow_token_copying=allow_token_copying,
                executor=executor,
                net_params=executable_graph.params,
                firing_ids=firing_ids,
            )
            # The body returned: what the rule on closed nets refused while it held
            # a net, or found changed there, fails the firing (``ClosedNets``).
            ClosedNets.settle(firing_ids)
        except Exception as exception:
            ClosedNets.release(firing_ids)
            # Raises TransitionFailedError carrying the consumed tokens;
            # restores them first when restore_tokens_on_failure is set.
            ExecutableGraphOperations.handle_failed_firing(
                transition=transition,
                exception=exception,
                input_edge_names_to_tokens=input_args_to_tokens,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                place_names_to_nodes=place_names_to_nodes,
                restore_tokens_on_failure=restore_tokens_on_failure,
                executable_graph=executable_graph,
                firing_ids=firing_ids,
            )
        finally:
            ClosedNets.release(firing_ids)
            executable_graph.in_flight = set()
        duration_ms = (time.perf_counter() - started) * 1000.0
        updated_places_dict = ExecutableGraphOperations.deposit_and_record(
            executable_graph=executable_graph,
            transition=transition,
            output_place_names_to_tokens=output_place_names_to_tokens,
            place_names_to_nodes=place_names_to_nodes,
            firing_ids=firing_ids,
            duration_ms=duration_ms,
            allow_token_copying=allow_token_copying,
        )
        output_places: Sequence[ListPlaceNode] = list(updated_places_dict.values())

        # Authoritative monotonic counter — never trimmed. See class
        # docstring for the idempotency / replay use case.
        executable_graph.step_count += 1
        # Authoritative "what just fired" — independent of history config.
        executable_graph.last_fired = transition.name
        # Cumulative per-transition tally — monotonic, never trimmed.
        executable_graph.fired_counts[transition.name] = (
            executable_graph.fired_counts.get(transition.name, 0) + 1
        )
        # Update transition history.
        if transition_history_length == 1:
            executable_graph.transition_history = [transition]
        elif transition_history_length > 1:
            executable_graph.transition_history.append(transition)
            if len(executable_graph.transition_history) > transition_history_length:
                executable_graph.transition_history.pop(0)
        # Update place history.
        if place_history_length == 1:
            executable_graph.input_place_history = [input_places]
            executable_graph.output_place_history = [output_places]
        elif place_history_length > 1:
            executable_graph.input_place_history.append(input_places)
            executable_graph.output_place_history.append(output_places)
            if len(executable_graph.input_place_history) > place_history_length:
                executable_graph.input_place_history.pop(0)
                executable_graph.output_place_history.pop(0)
        return transition.name

    async def execute_graph(
        executable_graph: ExecutableGraph,
        stop_after_n_firings: Optional[int] = _UNSET,
        allow_token_copying: Optional[bool] = None,
        restore_tokens_on_failure: Optional[bool] = None,
        verbose=False,
        transition_history_length=1,
        place_history_length=1,
        token_history_length=0,
        transition_selector: Optional[Callable[[ExecutableGraph, list[FunctionTransitionNode]], Optional[FunctionTransitionNode]]] = None,
        executor=None,
        max_transitions: Optional[int] = _UNSET,
    ) -> tuple[ExecutableGraph, int]:
        """Execute the Petri net graph.

        Token copying allows the same token to be output to multiple places via deepcopy.
        By default this uses the graph's allow_token_copying field (which defaults to False),
        but can be overridden by passing an explicit value here.

        Args:
            executable_graph: The graph to execute
            stop_after_n_firings: Stop — without error — after this many firings
                (default 1); None means run until no transition is fireable.
                Disambiguation idiom: a return with fired < stop_after_n_firings
                proves the net quiesced; fired == stop_after_n_firings means it
                may have been cut off mid-pipeline. Resolve by calling again (0
                further firings = it was quiescence), or drive the net through
                ``Runner.run``, whose ``RunSummary.quiesced`` states it
                explicitly.
            allow_token_copying: Whether to allow copying tokens. If None, uses the graph's setting.
            restore_tokens_on_failure: Whether a failed firing puts its consumed tokens
                back (best effort) before TransitionFailedError is raised. If None, uses
                the graph's setting (default False — see the field's comment on
                ExecutableGraph for why restoring is opt-in).
            verbose: Whether to print verbose output
            transition_history_length: Length of transition history to maintain
            place_history_length: Length of place history to maintain
            token_history_length: Length of token history to maintain
            transition_selector: Optional function to select which transition to fire.
                Signature: (graph, fireable_transitions) -> transition_to_fire
                (called positionally). If None, uses graph.transition_selector or default behavior.
            max_transitions: Deprecated alias of ``stop_after_n_firings`` (kept for
                legacy callers; passing it warns).

        Returns:
            Tuple of (updated_graph, transitions_fired_count)
        """
        if max_transitions is not _UNSET:
            if stop_after_n_firings is not _UNSET:
                raise TypeError(
                    "pass stop_after_n_firings only — max_transitions is its deprecated alias"
                )
            warnings.warn(
                "max_transitions is deprecated: use stop_after_n_firings (same meaning — "
                "stop without error after this many firings; None = run to quiescence).",
                DeprecationWarning,
                stacklevel=2,
            )
            stop_after_n_firings = max_transitions
        elif stop_after_n_firings is _UNSET:
            stop_after_n_firings = 1

        if allow_token_copying is None:
            allow_token_copying = executable_graph.allow_token_copying

        if restore_tokens_on_failure is None:
            restore_tokens_on_failure = executable_graph.restore_tokens_on_failure

        if token_history_length > 0 and not allow_token_copying:
            raise ValueError(
                "Token history can only be recorded when token copying is allowed. Adding tokens to the history list "
                "without making a copy means that history will be altered when the tokens are modified by subsequent "
                "transitions."
            )

        transitions_fired = 0
        # Reset per call so it reflects only this invocation (None if nothing fires).
        executable_graph.last_fired = None
        ExecutableGraphCheck.ensure_all_token_types_match_place_types(executable_graph)
        place_names_to_nodes: dict[str, ListPlaceNode] = MapPlaceNames.to_list_place_nodes(executable_graph)
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]] = \
            MapTransitionNames.to_incoming_edges(executable_graph)
        transition_names_to_outgoing_edges: dict[str, tuple[ReturnedEdgeFromTransition, ...]] = \
            MapTransitionNames.to_outgoing_edges(executable_graph)
        transition_names_to_read_edges = MapTransitionNames.to_read_edges(executable_graph)

        while True:
            # stop_after_n_firings=None means run until no transition is fireable.
            if stop_after_n_firings is not None and transitions_fired >= stop_after_n_firings:
                if verbose:
                    print(f"Performed {transitions_fired} transitions, stop_after_n_firings reached.")
                return executable_graph, transitions_fired

            fired_name = await ExecutableGraphOperations.fire_one_transition(
                executable_graph=executable_graph,
                place_names_to_nodes=place_names_to_nodes,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                transition_names_to_outgoing_edges=transition_names_to_outgoing_edges,
                transition_names_to_read_edges=transition_names_to_read_edges,
                transition_selector=transition_selector,
                allow_token_copying=allow_token_copying,
                restore_tokens_on_failure=restore_tokens_on_failure,
                executor=executor,
                transition_history_length=transition_history_length,
                place_history_length=place_history_length,
                token_history_length=token_history_length,
            )
            if fired_name is None:
                if verbose:
                    print(f"Performed {transitions_fired} transitions, no more valid transitions remaining.")
                return executable_graph, transitions_fired
            transitions_fired += 1
