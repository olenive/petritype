"""The runner's state machine: one net and the nets inside its tokens, the
protocol's bookkeeping, no I/O.

``Owner`` turns each request, each landed firing and each clock tick into the
lines the protocol says to write, in order, as plain dicts. It creates no tasks
or threads, reads no clock but the ``now`` it is handed, and writes nothing, so
every behaviour can be tested by feeding it inputs and reading what comes out.
It mirrors the Rust runner's ``Owner`` (petritype-rs DESIGN §19.3) and its
firing shape: every firing is reported in two pieces, ``launched`` (inputs
taken, body started) when it is launched and ``distributed`` (outputs placed)
or ``failed`` when it lands, a fast body's two lines in the same batch. Before
2026-10-04 this runner reported a firing once, as ``fired``, when it landed.

A firing is handed to the caller, not run here. ``handle`` for ``step``,
``start`` or a ``press`` of a ``fire`` control (and ``completed`` or ``tick``
while running) may return an ``Outgoing.launch``: a ``Launch`` whose coroutine
the caller awaits (as a task, so it can keep handling requests meanwhile) and
whose ``Landing`` it passes back through ``completed``. One firing is airborne
at a time. While it is, the reads (``get_state``, ``get_place_tokens``,
``get_token``, ``get_log``, ``query``, ``distinct``), ``stop``, ``describe`` and
``shutdown`` are answered at once and every other request waits, in arrival
order, for the landing; a second ``step`` therefore waits for the first.

Nested nets (PROTOCOL.md §12)
-----------------------------
Since 2026-10-09 the owner serves a tree of nets: the outer net the host loaded
and, inside the tokens of its ``nested`` places, inner nets at any depth, each
addressed by a ``path`` or a ``nested_id`` and each with its own
``marking_version``, ``fireable`` set, topology and log view. One ``_Net``
record per net holds what the protocol keeps about it, built when the owner first
knows of the net: at the load for the nets the factory seeded, at the ``inject``
that carried its token, at the landing of the firing that produced it (since
2026-10-12; until then when first addressed or fired), which is when its
``fireable`` baseline is taken. The outer one is ``_Loaded.outer`` and every
method below takes the net it acts on. A firing may be in any net: the driver
chooses it by one deep step of the loaded net (§12.3), in the order the stepping
rules of its places give (``_choose``: the inner-first places, the ring of the net
and its take-turns places from after the ring's cursor, then the outer-first
places, each member's turn a deep step of its own), the library's own order and
cursors, and launches what it chose. An inner net's notifications go out wrapped in
``nested`` with the inner version inside and the outer version, unmoved, on the
wrapper; the engine delivers inner events to the outer log already wrapped as
``Nested`` records with the path as it stood.

A path is a list of steps on the wire (§12.1, since 2026-11-21, when the
protocol became version 2), one per level from the loaded net down, and inside
the owner a tuple of ``wire.PathStep``s: a request's path is checked by
``Paths.parse`` and resolved one step at a time (``_descend``), and the
library's own path, a tuple of its ``PathStep``s with the token id an integer,
becomes the wire's through ``Paths.of_steps``. Progress is per net (§5.4): each
net counts the firings of its own transitions under their own names, and the
outer net's progress says nothing of the firings inside it.

Subnet places (PROTOCOL.md §12.7)
---------------------------------
Since 2026-11-21 a place may hold a net of its own, a subnet, reached through
its port places. A subnet is one more inner net in the owner's records, with a
``_Net`` built at the load or reset that builds it and kept until the next,
whose path ends in the step ``{place}``; that record knows the net around it
(``_Net.parent``) and the subnet place it stands in (``_Net.subnet_place``). A
net names its own places by name and the ports of its subnet places by the
library's ``Port`` pair, which every per-place table of a ``_Net`` is keyed by
(``place_nodes``, ``type_names``, ``serializable``, ``nested_places``, a
snapshot's views); a port's node is its subnet's own place, the one list of
tokens both nets read, and its filter, ids and visible set are the subnet's
(``_owner_of``). A change of a port place is in the markings of two nets, so
the state notification that tells it raises the version of the net across the
port as well and carries that version (``_cross``), and the net across it has
its ``fireable`` compared too, a net's before the nets inside it. While a
firing touches a port, the net across the port is read consistently: a firing
of the outer net leaves the subnet's reads waiting for its landing
(``_InFlight.held_nets``), and a firing of the subnet that takes from or hands
over a token of its own port leaves the outer net a snapshot with that port as
the firing left it (``_lifted``).

Reads while a body runs
-----------------------
The live graph is **not** safe to read while a body runs, for the reason the
server gave: synchronous bodies run on a thread (the owner marks every sync
transition ``execution="thread"`` when it first sees its net, as the server's
engine did, so user code never blocks the event loop), and a thread is not
bound by the event loop's await points. A body handed a token through a mutate
edge, or a whole place's live list through a ``list[...]`` mutate edge, can
change it while a read is iterating it. The marking mid-firing is also
inconsistent by definition (inputs taken, outputs not yet placed).

So every launch writes ``launched`` and then captures a ``_Snapshot`` of the
firing net's marking as of that ``launched``'s ``marking_version``: per place,
a shallow copy of the token and id lists (pointer copies, cheap) without the
tokens the firing takes (predicted from the edges exactly as stage 1 will take
them, and the same ones ``launched`` names), with the labels of the tokens it
hands to its body through mutate edges rendered at once, before the body can
touch them; the transition states as of that marking, with the airborne one
``in_flight``; and the progress. While the body runs, reads of that net are
served from the snapshot: counts are the true counts at that version, any page
can be served (labels of untouched tokens are rendered on demand from objects
no one else is changing), and the response says which version it reflects. A
taken token is in no place, so ``get_token`` for it answers ``unknown_token``,
as the Rust runner does. A ``get_token`` for a token handed to the body through
a mutate edge is the one read that waits for the landing: its full value is
exactly what the body may be changing.

With nested nets the same rule has two more cases. An outer body may reach into
the inner nets of the tokens it took or holds through a mutate edge, so a read
addressed to one of those nets waits for the landing (``_InFlight.held_nets``);
every other inner net is unchanged by that body and is read live. An inner
body changes only its own net, but the tokens that hold it, up to the outer
net, render it in their labels, so at an inner launch each net on the path
down to the firing net is snapshotted with its holding token rendered eagerly,
and a ``get_token`` on such a token waits for the landing.

The prediction is checked on landing against the engine's own ``Fired`` or
``Failed``; a difference would be a bug, and is reported on stderr (the one
place the owner writes anything itself).
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Coroutine, Optional, Union

from pydantic import TypeAdapter

from petritype.core import events as core_events
from petritype.core.events import PlaceKey, Port
from petritype.core.executable_graph_components import (
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    KeyedEdges,
    ListPlaceNode,
    MapPlaceNames,
    MapTransitionNames,
    MutateWhereEdge,
    NestedNets,
    SubnetPlaceNode,
    TakeWhereEdge,
    TokenIds,
    TransitionFailedError,
    default_transition_selector,
)
from petritype.core.keys import Filter, Query
from petritype.runner import protocol as wire
from petritype.runner.protocol import ErrorCode, Lines, Malformed, ProtocolError, Request, Requests
from petritype.runner.registry import NetEntries, NetEntry, Registry
from petritype.runner.translate import (
    JSON_SCALARS, Controls, Filters, Paths, Places, Serializable, StrictJson, Tokens, Topologies, TypeNames,
)
from petritype.runtime import (
    ControlSpec, Disable, Enable, Extend, Fire, RunContext, Runner, SetNetParam, SetParam,
)


# How many entries ``get_log`` keeps (PROTOCOL.md §8).
LOG_CAPACITY = 1000
# ``get_state``'s and ``get_place_tokens``'s default page, and the largest.
DEFAULT_TOKEN_LIMIT = 200
MAX_TOKEN_LIMIT = 1000

# Answered at once while a body is airborne; every other request waits for the
# landing. ``shutdown`` abandons the body, ``describe`` reads nothing of the net.
ANSWERED_WHILE_AIRBORNE = frozenset({
    "get_state", "get_place_tokens", "get_token", "get_log", "query", "distinct", "stop", "shutdown", "describe",
})
READS = frozenset({"get_state", "get_place_tokens", "get_token", "get_log", "query", "distinct"})

METHODS = frozenset({
    "describe", "load", "get_state", "get_place_tokens", "get_token", "step", "start", "stop",
    "reset", "inject", "enable", "disable", "set_param", "get_log", "shutdown",
    "query", "distinct", "set_filter", "press",
})

# Methods that act on the outer net alone (§12.3); a ``path`` or ``nested_id`` on them is refused.
OUTER_ONLY = frozenset({"enable", "disable", "set_param", "start", "stop", "reset", "step", "press"})

CONTINUOUS_MODE = "24/7"


# --- what goes out -------------------------------------------------------------------------


@dataclass(frozen=True)
class Landing:
    """What a ``Launch`` coroutine resolves to: the transition, the net it was in
    (``None`` for the outer net), the generation it was launched in, and the
    exception it ended with, if any. Whether the firing fired or failed is read
    off the graph's events by ``Owner.completed``."""

    transition: str
    generation: int
    net_id: Optional[int] = None
    error: Optional[BaseException] = None


@dataclass
class Launch:
    """A firing for the caller to run: await ``coroutine`` and hand the
    ``Landing`` it returns to ``Owner.completed``."""

    transition: str
    coroutine: Coroutine[Any, Any, Landing]


@dataclass
class Outgoing:
    """What one call to the owner produced: ``lines`` to write in order, at most
    one ``launch`` to run, and ``exit`` once ``shutdown`` was answered."""

    lines: list[dict] = field(default_factory=list)
    launch: Optional[Launch] = None
    exit: bool = False


# --- what the owner keeps ------------------------------------------------------------------


@dataclass
class _PlaceView:
    """One place as of a snapshot. ``eager`` holds the entries rendered at capture
    (the tokens handed to an airborne body), ``at_risk`` their indices. ``nested``
    places render each token's ``nets`` under ``prefix``, the path of the net the
    place belongs to (empty for the loaded net); ``place`` is the node itself, for the keys it derives;
    ``filter`` and ``params`` the connection's projection (§13.4), under which
    only the kept tokens are listed and ``matching`` counts them. A port seen
    from the net around its subnet is viewed as its subnet's place: ``name`` is
    its own name, and ``prefix``, ``filter`` and ``params`` are the subnet's."""

    name: str
    type_name: str
    ids: list[int]
    tokens: list[Any]
    eager: dict[int, wire.Token] = field(default_factory=dict)
    nested: bool = False
    prefix: tuple[wire.PathStep, ...] = ()
    place: Optional[ListPlaceNode] = None
    filter: Optional[Filter] = None
    params: dict = field(default_factory=dict)
    _kept: Optional[list[int]] = None

    @property
    def count(self) -> int:
        return len(self.tokens)

    def kept(self) -> list[int]:
        """The indices the filter keeps (every index without a filter)."""
        if self._kept is None:
            if self.filter is None or self.place is None:
                self._kept = list(range(min(len(self.ids), len(self.tokens))))
            else:
                self._kept = [
                    index for index in range(min(len(self.ids), len(self.tokens)))
                    if self.filter.keeps(self.place, self.tokens[index], self.params)
                ]
        return self._kept

    @property
    def matching(self) -> Optional[int]:
        return len(self.kept()) if self.filter is not None else None

    def keeps(self, index: int) -> bool:
        return self.filter is None or index in self.kept()

    def entry(self, index: int) -> wire.Token:
        cached = self.eager.get(index)
        if cached is not None:
            return cached
        token_id, token = self.ids[index], self.tokens[index]
        nets = Tokens.nets(token, self.prefix, self.name, token_id) if self.nested else None
        keys = Tokens.keys(self.place, token) if self.place is not None else None
        return Tokens.entry(token_id, token, self.type_name, nets=nets, keys=keys)

    def entries(self, offset: int, limit: int) -> list[wire.Token]:
        """A page of the kept tokens (all of them without a filter)."""
        return [self.entry(index) for index in self.kept()[offset:offset + limit]]

    def total(self) -> int:
        """What a page is relative to: the kept tokens under a filter, else all."""
        return len(self.kept()) if self.filter is not None else self.count


@dataclass
class _Snapshot:
    """A net's marking as of one version. ``places`` is keyed as the net names
    its places, a port by its ``Port`` pair, in declaration order with a subnet
    place's ports at its position; ``subnets`` holds each subnet place's entry."""

    marking_version: int
    transitions: dict[str, wire.TransitionState]
    places: dict[PlaceKey, _PlaceView]
    progress: wire.Progress
    subnets: dict[str, wire.SubnetEntry] = field(default_factory=dict)


@dataclass
class _Net:
    """One net, outer or inner, and what the protocol keeps about it. The outer
    net's ``nested_id`` is ``None``. Per-place tables are keyed as the net names
    its places: its own by name, the ports of its subnet places by ``Port``. A
    subnet knows the net around it, ``parent``, and its ``subnet_place`` there."""

    graph: ExecutableGraph
    ctx: RunContext
    topology: wire.Topology
    type_names: dict[PlaceKey, str]
    serializable: dict[PlaceKey, bool]
    nested_places: set[PlaceKey]
    place_nodes: dict
    incoming: dict
    outgoing: dict
    read: dict
    marking_version: int
    last_fireable: list[str]
    nested_id: Optional[int] = None
    parent: Optional["_Net"] = None
    subnet_place: Optional[str] = None
    # The last path this inner net was found at, for a line written while its token is airborne.
    last_path: tuple[wire.PathStep, ...] = ()
    last_fired: Optional[str] = None
    last_progress_at: Optional[datetime] = None
    # The connection's projection of this net (§13.4), and the ids it has been shown.
    filter: Optional[Filter] = None
    visible: set[int] = field(default_factory=set)
    # What the last ``launched`` in this net listed under the filter, which its
    # ``failed`` lists too (§13.4).
    launched_shown: set[tuple[PlaceKey, int]] = field(default_factory=set)

    @property
    def is_outer(self) -> bool:
        return self.nested_id is None


@dataclass
class _InFlight:
    # The net the firing is in: ``None`` for the outer net.
    net_id: Optional[int]
    name: str
    started_at: datetime
    # The ``step`` request waiting for this firing, if a step launched it.
    step_id: Optional[str]
    # Per net (``None`` for the outer), the marking it is read from while the body runs.
    snapshots: dict[Optional[int], _Snapshot]
    # What ``launched`` said the firing takes, in the order taken.
    consumed: list[tuple[PlaceKey, int]] = field(default_factory=list)
    # Inner nets, at any depth, inside the tokens the firing took or holds, and the
    # subnets whose ports it touches: read after the landing.
    held_nets: set[int] = field(default_factory=set)
    # The tokens the firing took or holds, as ``(nested id of the net whose place
    # holds it, that net's name for the place, id)``, for a path that names one.
    held_tokens: set[tuple[Optional[int], str, int]] = field(default_factory=set)
    # The ``press`` request waiting for this firing, if a ``fire`` control launched it.
    press_id: Optional[str] = None
    # The path of the net the firing is in, taken at the launch (empty for the
    # outer net), for ``heartbeat``. Nothing else fires while this firing is in
    # flight, so the path stays true until the landing.
    path: tuple[wire.PathStep, ...] = ()


@dataclass(frozen=True)
class Executing:
    """The body in flight, as ``heartbeat`` names it (PROTOCOL.md §7.2): its
    transition and when it started, and, for a body inside an inner net, that
    net's ``path`` and ``nested_id``, as a ``step`` result names them. A value,
    so the heartbeat thread can keep it."""

    transition: str
    started_at: datetime
    path: tuple[wire.PathStep, ...] = ()
    nested_id: Optional[int] = None


@dataclass
class _Handover:
    """What stage 1 will do to the marking as it stands: ``consumed``, the
    ``(place, id)`` pairs its argument edges take, in the order taken; ``taken``,
    per place, how many tokens leave from the front; ``mutated``, per place, the
    indices (in the place as it stands) of the tokens handed to the body through
    mutate edges. Snapshot edges hand over deep copies and appear in none.
    Places are keyed as the firing net names them, a port by its ``Port`` pair."""

    consumed: list[tuple[PlaceKey, int]]
    taken: dict[PlaceKey, int]
    mutated: dict[PlaceKey, set[int]]
    # Per place, the indices (in the place as it stands) a keyed take removes, which
    # need not be at the front.
    keyed_taken: dict[PlaceKey, list[int]] = field(default_factory=dict)


@dataclass
class _Entry:
    """A log entry and the nested ids from the outer net down to the net it was
    in (empty for the outer net), for ``get_log`` with a path."""

    line: wire.LogEntry
    chain: tuple[int, ...]


@dataclass
class _Loaded:
    """One generation of one net, the inner nets seen so far, and what the
    protocol keeps about them."""

    entry: NetEntry
    params: dict
    outer: _Net
    inner: dict[int, _Net] = field(default_factory=dict)
    # The controls of this generation, ``{name: ControlSpec}`` (the entry's, resolved at the build).
    controls: dict = field(default_factory=dict)
    running: bool = False
    in_flight: Optional[_InFlight] = None
    deferred: deque = field(default_factory=deque)
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_CAPACITY))
    log_seq: int = 0
    idle: bool = False


_HELD = object()  # a read's target is inside a token the airborne firing holds


# --- the owner -----------------------------------------------------------------------------


class Owner:
    """The runner's state machine (see the module docstring). Feed it requests
    with ``handle``, landings with ``completed`` and clock ticks with ``tick``;
    write the ``lines`` each returns, run its ``launch``, stop on ``exit``."""

    def __init__(self, registry: Registry):
        self.registry = registry
        # Loads and resets so far: 0 until the first load.
        self.generation = 0
        # The outer net's version carries across load and reset (PROTOCOL.md §4).
        self._carried_version = 0
        self._loaded: Optional[_Loaded] = None

    # --- read-only views for the io layer ---

    @property
    def marking_version(self) -> int:
        return self._loaded.outer.marking_version if self._loaded is not None else self._carried_version

    @property
    def running(self) -> bool:
        return self._loaded is not None and self._loaded.running

    @property
    def loaded(self) -> bool:
        return self._loaded is not None

    @property
    def airborne(self) -> bool:
        return self._loaded is not None and self._loaded.in_flight is not None

    @property
    def status(self) -> Optional[Executing]:
        """The body that runs, for ``heartbeat``: its transition, when it
        started, and the path and nested id of the inner net it is in, if it is
        in one; ``None`` when no body runs."""
        if self._loaded is None or self._loaded.in_flight is None:
            return None
        flight = self._loaded.in_flight
        return Executing(flight.name, flight.started_at, flight.path, flight.net_id)

    def describe(self) -> dict:
        return self.registry.describe().wire()

    # --- inputs ---

    async def handle(self, request: Union[dict, Request, Malformed], now: datetime) -> Outgoing:
        """One request (a decoded JSON object, or one already parsed)."""
        out = Outgoing()
        parsed = Requests.parse(request) if isinstance(request, dict) else request
        if isinstance(parsed, Malformed):
            out.lines.append(Lines.error(parsed.id, ErrorCode.INVALID_PARAMS, parsed.message))
            return out
        loaded = self._loaded
        if loaded is not None and loaded.in_flight is not None and not self._answers_now(parsed):
            loaded.deferred.append(parsed)
            return out
        if self._serve(parsed, now, out):
            return out
        self._drive(now, out)
        return out

    async def completed(self, landing: Landing, now: datetime) -> Outgoing:
        """A launched firing came back: report it, answer what waited for it, and
        keep a running net going."""
        out = Outgoing()
        loaded = self._loaded
        if (
            loaded is None
            or loaded.in_flight is None
            or landing.generation != self.generation
            or landing.transition != loaded.in_flight.name
            or landing.net_id != loaded.in_flight.net_id
        ):
            return out  # nothing of ours is airborne: a stale landing
        self._land(landing, now, out)
        if self._serve_deferred(now, out):
            return out
        self._drive(now, out)
        return out

    async def tick(self, now: datetime) -> Outgoing:
        """The clock: a running net that is free looks for its next firing, and an
        idle ``"24/7"`` net checks again."""
        out = Outgoing()
        self._drive(now, out)
        return out

    # --- dispatch ---

    def _answers_now(self, request: Request) -> bool:
        if request.method not in ANSWERED_WHILE_AIRBORNE:
            return False
        if request.method not in READS or not isinstance(request.params, dict):
            return True
        target = self._target_quietly(request.params)
        if target is _HELD:
            return False
        if request.method == "get_token":
            return not self._token_at_risk(target, request.params)
        return True

    def _serve(self, request: Request, now: datetime, out: Outgoing) -> bool:
        """Answer one request. True when it was ``shutdown``."""
        request_id, method, params = request.id, request.method, request.params
        if method not in METHODS:
            out.lines.append(Lines.error(request_id, ErrorCode.UNKNOWN_METHOD, f"no such method: {method!r}"))
            return False
        if not isinstance(params, dict):
            out.lines.append(Lines.error(request_id, ErrorCode.INVALID_PARAMS, "params must be an object"))
            return False
        try:
            if method == "describe":
                out.lines.append(Lines.result(request_id, self.describe()))
            elif method == "shutdown":
                out.lines.append(Lines.result(request_id, {"status": "ok"}))
                out.exit = True
                return True
            elif method == "load":
                self._load(request_id, params, out)
            elif self._loaded is None:
                raise ProtocolError(ErrorCode.NOT_LOADED, f"{method} needs a loaded net")
            else:
                if method in OUTER_ONLY and ("path" in params or "nested_id" in params):
                    raise ProtocolError(ErrorCode.INVALID_PARAMS, f"{method} acts on the outer net: no path or nested_id")
                getattr(self, f"_{method}")(request_id, params, now, out)
        except ProtocolError as error:
            out.lines.append(Lines.error(request_id, error.code, error.message))
        except Exception as error:  # the owner must survive anything a handler meets
            out.lines.append(Lines.error(request_id, ErrorCode.INTERNAL, f"{type(error).__name__}: {error}"))
        return False

    def _serve_deferred(self, now: datetime, out: Outgoing) -> bool:
        """Serve the requests that waited for a landing, in order, until one of
        them launches again (the rest wait for that one). True on ``shutdown``."""
        queue = self._loaded.deferred
        self._loaded.deferred = deque()
        while queue:
            if self._serve(queue.popleft(), now, out):
                return True
            loaded = self._loaded
            if loaded is not None and loaded.in_flight is not None:
                queue.extend(loaded.deferred)
                loaded.deferred = queue
                return False
        return False

    # --- load and reset ---

    def _load(self, request_id: str, params: dict, out: Outgoing) -> None:
        if "net" not in params and any(key in params for key in ("module", "function", "factory_params")):
            module = _string(params, "module")
            function = _string(params, "function")
            raw = _object(params, "factory_params")
            entry = self.registry.resolve_v0(module, function)
        else:
            name = _string(params, "net")
            entry = self.registry.get(name)
            if entry is None:
                raise ProtocolError(ErrorCode.UNKNOWN_NET, f"no net named {name!r}")
            raw = _object(params, "params")
        kwargs = NetEntries.bind(entry, raw)
        out.lines.append(wire.LoadProgress(
            step="build", message=f"building {entry.name}",
            marking_version=self.marking_version, running=self.running,
        ).line())
        self._build(entry, raw, kwargs, "load", out)
        out.lines.append(Lines.result(request_id, {
            "status": "loaded", "generation": self.generation, "marking_version": self.marking_version,
        }))

    def _reset(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        loaded = self._loaded
        kwargs = NetEntries.bind(loaded.entry, loaded.params)
        self._build(loaded.entry, loaded.params, kwargs, "reset", out)
        out.lines.append(Lines.result(request_id, {
            "status": "reset", "generation": self.generation, "marking_version": self.marking_version,
        }))

    def _build(self, entry: NetEntry, raw: dict, kwargs: dict, reason: str, out: Outgoing) -> None:
        """Run the factory and install its net as the next generation, writing
        ``reloaded``. On failure raise ``load_failed`` and keep the previous net."""
        try:
            graph = entry.factory(**kwargs)
        except Exception as error:
            raise ProtocolError(ErrorCode.LOAD_FAILED, f"{type(error).__name__}: {error}") from error
        if not isinstance(graph, ExecutableGraph):
            raise ProtocolError(
                ErrorCode.LOAD_FAILED,
                f"factory {entry.name!r} returned {type(graph).__name__}, not an ExecutableGraph",
            )
        try:
            ExecutableGraphCheck.ensure_all_token_types_match_place_types(graph)
            outer = self._new_net(graph, self.marking_version)
            # What the entry declares beside the net: its controls and place styles (§5.1).
            controls = NetEntries.controls(entry)
            Topologies.declare(outer.topology, Controls.wire(controls, graph), entry.place_styles)
        except Exception as error:
            raise ProtocolError(ErrorCode.LOAD_FAILED, f"{type(error).__name__}: {error}") from error
        # A new generation starts from its built marking: whatever the factory did
        # on the way is part of it, so nothing recorded so far is reported.
        graph.record_events = True
        TokenIds.resync(graph)
        graph.drain_events()
        self._carried_version = outer.marking_version
        self._loaded = _Loaded(entry=entry, params=raw, outer=outer, controls=controls)
        self.generation += 1
        # Recorded silently: ``reloaded`` already means "refetch".
        outer.last_fireable = self._fireable_names(outer)
        # The inner nets the factory seeded, and every subnet, are known from the
        # load: their baselines too.
        self._know_new()
        out.lines.append(wire.Reloaded(
            generation=self.generation, reason=reason,
            marking_version=outer.marking_version, running=False,
        ).line())

    def _new_net(
        self, graph: ExecutableGraph, marking_version: int, nested_id: Optional[int] = None, subnet: bool = False,
    ) -> _Net:
        """What the protocol keeps about a net, outer or inner; ``subnet`` for the
        subnet of a subnet place, whose topology shows its ports. Every synchronous
        body is moved to a thread here, so user code never blocks the loop the
        owner is driven from."""
        topology = Topologies.of(graph, subnet=subnet)
        for transition in graph.transitions:
            if not inspect.iscoroutinefunction(transition.function):
                transition.execution = "thread"
        # Every place the net's transitions reach, its subnets' ports by their pairs.
        place_nodes = MapPlaceNames.to_list_place_nodes(graph)
        return _Net(
            graph=graph,
            ctx=RunContext(graph=graph, inbox=asyncio.Queue(), strict=True),
            topology=topology,
            type_names={key: TypeNames.of(node.type) for key, node in place_nodes.items()},
            serializable={key: Serializable.place_type(node.type) for key, node in place_nodes.items()},
            nested_places={key for key, node in place_nodes.items() if node.nested},
            place_nodes=place_nodes,
            incoming=MapTransitionNames.to_incoming_edges(graph),
            outgoing=MapTransitionNames.to_outgoing_edges(graph),
            read=MapTransitionNames.to_read_edges(graph),
            marking_version=marking_version,
            last_fireable=[],
            nested_id=nested_id,
        )

    # --- addressing nets (§12.1) ---

    def _net_for(self, graph: ExecutableGraph, path: tuple[wire.PathStep, ...] = ()) -> _Net:
        """The ``_Net`` of a graph found inside the outer net, built on first sight;
        ``path`` is where it was found, kept as its last known path. A net whose
        path ends in a subnet step is a subnet: its record is linked to the net
        around it, which holds its subnet place for as long as both live."""
        loaded = self._loaded
        if graph is loaded.outer.graph:
            return loaded.outer
        net = loaded.inner.get(graph.net_id)
        if net is None:
            if not path:
                found = NestedNets.find(loaded.outer.graph, graph.net_id)
                path = Paths.of_steps(found[0]) if found is not None else ()
            subnet = bool(path) and path[-1].token is None
            net = self._new_net(graph, 0, nested_id=graph.net_id, subnet=subnet)
            loaded.inner[graph.net_id] = net
            if subnet:
                net.parent = self._net_for(self._graph_at(path[:-1]), path[:-1])
                net.subnet_place = path[-1].place
            net.last_fireable = self._fireable_names(net)
        if path:
            net.last_path = path
        return net

    def _graph_at(self, steps: tuple[wire.PathStep, ...]) -> ExecutableGraph:
        """The net a path names, from the loaded net down; ``LookupError`` if a
        step names none."""
        graph = self._loaded.outer.graph
        for step in steps:
            graph = _descend(graph, step)
        return graph

    def _subnet_of(self, net: _Net, place: str) -> _Net:
        """The record of the subnet that ``net``'s subnet place ``place`` holds."""
        graph = net.graph.subnet(place)
        if graph is None:
            raise KeyError(f"no subnet place {place!r}")
        known = self._loaded.inner.get(graph.net_id)
        if known is not None:
            return known
        return self._net_for(graph, (*self._path_of(net), Paths.subnet_step(place)))

    def _owner_of(self, net: _Net, key: PlaceKey) -> tuple[_Net, str]:
        """The net whose own place ``key`` names, and its name there: ``net`` and
        the name for a place of its own; for a port, the subnet that holds it,
        whose ids, filter and keys the port has, and the port's own name."""
        if isinstance(key, Port):
            return self._subnet_of(net, key.place), key.port
        return net, key

    def _keeps(self, net: _Net, key: PlaceKey, token: Any) -> bool:
        """Whether the connection's filter lets a live token of the place ``net``
        names ``key`` through: a port is filtered in its subnet (§12.7)."""
        owner, own = self._owner_of(net, key)
        if owner.filter is None:
            return True
        return owner.filter.keeps(owner.place_nodes[own], token, owner.graph.params)

    def _place_key(self, net: _Net, place: str, port: Optional[str]) -> PlaceKey:
        """The place a request names by ``place`` and ``port`` (§12.7): a place of
        ``net`` by its name, or a port of one of its subnet places by the pair. A
        subnet place named without a ``port`` is ``invalid_params``, since it holds
        no tokens of its own; a place the net does not have, a ``port`` its subnet
        place does not have, and a ``port`` beside a place that is not a subnet
        place are ``unknown_place``."""
        node = net.graph.place_named(place)
        if port is None:
            if isinstance(node, SubnetPlaceNode):
                raise ProtocolError(
                    ErrorCode.INVALID_PARAMS,
                    f"{place!r} is a subnet place, which holds no tokens of its own: name one of its ports as "
                    f'"port"',
                )
            if place not in net.place_nodes:
                raise ProtocolError(ErrorCode.UNKNOWN_PLACE, f"no place named {place!r}")
            return place
        if node is None:
            raise ProtocolError(ErrorCode.UNKNOWN_PLACE, f"no place named {place!r}")
        if not isinstance(node, SubnetPlaceNode):
            raise ProtocolError(ErrorCode.UNKNOWN_PLACE, f"{place!r} is not a subnet place, so it has no port {port!r}")
        key = Port(place, port)
        if key not in net.place_nodes:
            raise ProtocolError(ErrorCode.UNKNOWN_PLACE, f"subnet place {place!r} has no port {port!r}")
        return key

    def _place_names(self, net: _Net) -> set[PlaceKey]:
        """What a filter's or ``distinct``'s ``places`` may name in ``net``: its
        places, subnet places among them, and the ports of its subnet places."""
        return set(net.place_nodes) | {place.name for place in net.graph.places}

    def _know_new(self) -> None:
        """Take the ``fireable`` baseline of every inner net, at any depth, that the
        owner does not know yet (§7.2, §12.3): a net is known from when the runtime
        first knows of it, at the load for the nets the factory seeded, at the
        ``inject`` that carried its token, or at the landing of the outer firing that
        produced its token; no ``fireable`` is sent for the baseline itself, and the
        first ``nested`` ``fireable`` reports the first change from it. Every net,
        passive places' included."""
        loaded = self._loaded
        if not NestedNets.any_nested(loaded.outer.graph):
            return

        def visit(graph: ExecutableGraph, prefix: tuple[wire.PathStep, ...]) -> None:
            for step, inner in NestedNets.walk(graph):
                path = (*prefix, Paths.of_step(step))
                if inner.net_id not in loaded.inner:
                    self._net_for(inner, path)
                visit(inner, path)

        visit(loaded.outer.graph, ())

    def _net_by_id(self, net_id: Optional[int]) -> Optional[_Net]:
        """The net with this id if it is the outer one, already known, or live
        somewhere inside; ``None`` for an id that names no net any more."""
        loaded = self._loaded
        if net_id is None:
            return loaded.outer
        known = loaded.inner.get(net_id)
        if known is not None:
            return known
        found = NestedNets.find(loaded.outer.graph, net_id)
        if found is None:
            return None
        return self._net_for(found[1], Paths.of_steps(found[0]))

    def _target(self, params: dict) -> _Net:
        """The net a request addresses: the outer net, or the inner net its
        ``path`` or ``nested_id`` names. A path of the wrong shape is
        ``invalid_params`` (``Paths.parse``), the empty path the loaded net, and a
        path or id that names no live net ``unknown_net``, a path's error naming
        the step that failed by its index from 0."""
        loaded = self._loaded
        has_path, has_id = "path" in params, "nested_id" in params
        if has_path and has_id:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, "give a path or a nested_id, not both")
        if has_id:
            spelt = _string(params, "nested_id")
            net_id = _token_id(spelt)
            found = NestedNets.find(loaded.outer.graph, net_id) if net_id is not None else None
            if found is None:
                raise ProtocolError(ErrorCode.UNKNOWN_NET, f"no live inner net {spelt!r}")
            return self._net_for(found[1], Paths.of_steps(found[0]))
        if has_path:
            steps = Paths.parse(params["path"])
            graph = loaded.outer.graph
            for index, step in enumerate(steps):
                try:
                    graph = _descend(graph, step)
                except LookupError as error:
                    raise ProtocolError(ErrorCode.UNKNOWN_NET, f"step {index} of the path: {error}") from error
            return self._net_for(graph, steps)
        return loaded.outer

    def _target_quietly(self, params: dict):
        """For ``_answers_now``: the target's nested id (``None`` for the outer
        net), ``_HELD`` when it lies inside a token the airborne outer firing
        holds (so the read waits), or ``None`` when it cannot be resolved (so the
        read is answered now, with its error)."""
        flight = self._loaded.in_flight
        try:
            net = self._target(params)
        except ProtocolError:
            net = None
        if net is not None:
            return _HELD if net.nested_id in flight.held_nets else net.nested_id
        spelt = params.get("nested_id")
        if isinstance(spelt, str) and (net_id := _token_id(spelt)) in flight.held_nets:
            return _HELD
        try:
            steps = Paths.parse(params["path"]) if "path" in params else ()
        except ProtocolError:
            steps = ()
        # A path that no longer resolves may pass through a token the firing took:
        # it waits, to be answered as the landing leaves the marking.
        graph, net_id = self._loaded.outer.graph, None
        for step in steps:
            if step.token is not None and (token_id := _token_id(step.token)) is not None:
                if (net_id, step.place, token_id) in flight.held_tokens:
                    return _HELD
            try:
                graph = _descend(graph, step)
            except LookupError:
                break
            net_id = graph.net_id
        return None

    def _path_of(self, net: _Net) -> tuple[wire.PathStep, ...]:
        """The path of a net as the marking stands (its last known path while the
        token holding it is airborne); the empty path for the loaded net."""
        if net.is_outer:
            return ()
        found = NestedNets.find(self._loaded.outer.graph, net.nested_id)
        if found is not None:
            net.last_path = Paths.of_steps(found[0])
        return net.last_path

    def _net_name(self, net: _Net) -> str:
        """The name a state names: the loaded net's for the outer net, the net's
        name within its token for an inner one, and its subnet place's for a
        subnet (§12.3)."""
        if net.is_outer:
            return self._loaded.entry.name
        if net.subnet_place is not None:
            return net.subnet_place
        return (net.last_path[-1].net or "") if net.last_path else ""

    def _stamp(self, net: _Net, model: wire.Wire) -> wire.Wire:
        """``path`` and ``nested_id`` on a result about an inner net."""
        if not net.is_outer:
            model.path = list(self._path_of(net))
            model.nested_id = str(net.nested_id)
        return model

    # --- reads ---

    def _get_state(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        limit = _page_limit(params, "token_limit")
        net = self._target(params)
        snapshot = self._snapshot_of(net) or self._capture(net)

        def marking_of(view: _PlaceView) -> wire.PlaceMarking:
            return wire.PlaceMarking(count=view.count, matching=view.matching, tokens=view.entries(0, limit))

        places: dict[str, Union[wire.PlaceMarking, wire.SubnetMarking]] = {}
        for place in net.graph.places:
            if isinstance(place, SubnetPlaceNode):
                # The place holds no tokens of its own: its subnet, and its ports (§12.7).
                places[place.name] = wire.SubnetMarking(
                    subnet=snapshot.subnets[place.name],
                    ports={port.name: marking_of(snapshot.places[Port(place.name, port.name)]) for port in place.ports},
                )
            else:
                places[place.name] = marking_of(snapshot.places[place.name])
        state = wire.State(
            net=self._net_name(net),
            generation=self.generation if net.is_outer else 1,
            marking_version=snapshot.marking_version,
            running=self._loaded.running,
            topology=net.topology,
            transitions=snapshot.transitions,
            marking=wire.Marking(places=places),
            progress=snapshot.progress,
            template=net.graph.template if not net.is_outer else None,
        )
        out.lines.append(Lines.result(request_id, self._stamp(net, state)))

    def _get_place_tokens(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        place = _string(params, "place")
        port = _optional_string(params, "port")
        offset = _count(params, "offset") or 0
        limit = _page_limit(params, "limit")
        net = self._target(params)
        key = self._place_key(net, place, port)
        snapshot = self._snapshot_of(net)
        if snapshot is not None:
            view, version = snapshot.places[key], snapshot.marking_version
        else:
            view, version = self._view(net, key), net.marking_version
        tokens = view.entries(offset, limit)
        out.lines.append(Lines.result(request_id, self._stamp(net, wire.PlaceTokens(
            place=place, port=port, marking_version=version, count=view.count, matching=view.matching,
            offset=offset, limit=limit, tokens=tokens, has_more=offset + len(tokens) < view.total(),
        ))))

    def _get_token(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        wanted = _string(params, "id")
        token_id = _token_id(wanted)
        net = self._target(params)
        found = None
        if token_id is not None:
            snapshot = self._snapshot_of(net)
            if snapshot is not None:
                found = _find(snapshot, token_id)
            else:
                found = TokenIds.locate(net.graph, token_id)
                self._flush(now, out)  # locate resyncs: report any repair first
        # A port's tokens carry their subnet's ids, so only the net's own places
        # are searched (``_find``, ``TokenIds.locate``): one is read through its subnet.
        if found is None or not self._keeps(net, found[0], found[1]):
            raise ProtocolError(ErrorCode.UNKNOWN_TOKEN, f"no live token {wanted!r}")
        place, token = found
        common = {"id": wanted, "place": place, "type_name": type(token).__name__}
        if place in net.nested_places:
            common["nets"] = Tokens.nets(token, self._path_of(net), place, token_id)
        value = None
        if net.serializable[place]:
            try:
                value = wire.TokenValue(kind="json", value=Tokens.json_value(token), **common)
            except Exception:
                value = None  # a token of a serializable type that will not dump: show its text
        if value is None:
            value = wire.TokenValue(kind="text", text=_repr(token), **common)
        out.lines.append(Lines.result(request_id, self._stamp(net, value)))

    def _get_log(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        after = _count(params, "after") or 0
        limit = _count(params, "limit")
        limit = LOG_CAPACITY if limit is None else max(1, min(limit, LOG_CAPACITY))
        net = self._target(params)
        entries = [
            entry.line for entry in self._loaded.log
            if entry.line.seq > after and (net.is_outer or net.nested_id in entry.chain)
        ][:limit]
        out.lines.append(Lines.result(request_id, {"entries": [entry.wire() for entry in entries]}))

    def _query(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        """``query`` (§13.3): the tokens a filter keeps, from the same marking
        ``get_state`` reads, in place order then token order, a subnet place's
        ports at its position. Without a ``filter`` each place is projected by its
        connection filter, a port by its subnet's (§12.7); a ``filter`` of the
        request's own applies to every place, ports included."""
        limit = _page_limit(params, "token_limit")
        net = self._target(params)
        chosen: Optional[Filter] = None
        if "filter" in params:
            chosen = Filters.from_wire(params["filter"], self._place_names(net))
        snapshot = self._snapshot_of(net) or self._capture(net)
        matches: list[dict] = []
        for key, view in snapshot.places.items():
            if chosen is not None and not chosen.covers(key):
                continue
            place = net.place_nodes[key]
            for index in range(min(len(view.ids), len(view.tokens))):
                token = view.tokens[index]
                if chosen is not None:
                    if not chosen.keeps(place, token, net.graph.params, key):
                        continue
                elif not view.keeps(index):
                    continue
                if len(matches) < limit:
                    where, port = Places.split(key)
                    match: dict = {"place": where}
                    if port is not None:
                        match["port"] = port
                    match["token"] = view.entry(index).wire()
                    if net.serializable[key]:
                        try:
                            match["json"] = Tokens.json_value(token)
                        except Exception:
                            pass
                    matches.append(match)
                else:
                    matches.append(None)  # counted, not listed
        out.lines.append(Lines.result(request_id, wire.QueryResult(
            marking_version=snapshot.marking_version, count=len(matches),
            matches=[match for match in matches if match is not None],
        )))

    def _distinct(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        """``distinct`` (§13.3): the values of a key in order of first appearance."""
        key = _string(params, "key")
        net = self._target(params)
        places = params.get("places")
        if places is not None:
            # A place by name or a port by {"place", "port"}; a subnet place named
            # alone selects nothing, since it holds no tokens (§12.7).
            places = Filters.places(places, self._place_names(net), '"places"')
        snapshot = self._snapshot_of(net)
        if snapshot is None:
            values = Query.distinct(net.graph, key, places)
            version = net.marking_version
        else:
            from petritype.core.keys import Keys, same_value
            values = []
            for name, view in snapshot.places.items():
                place = net.place_nodes[name]
                if (places is not None and name not in places) or not place.keys or key not in place.keys:
                    continue
                for token in view.tokens:
                    value = Keys.of(place, token)[key]
                    if not any(same_value(value, known) for known in values):
                        values.append(value)
            version = snapshot.marking_version
        out.lines.append(Lines.result(request_id, wire.DistinctResult(
            marking_version=version, key=key, values=values,
        )))

    def _set_filter(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        """``set_filter`` (§13.4): the connection's projection of one net; without
        ``filter`` it is cleared."""
        net = self._target(params)
        if "filter" in params and params["filter"] is not None:
            net.filter = Filters.from_wire(params["filter"], self._place_names(net))
        else:
            net.filter = None
        self._sync_visible(net)
        out.lines.append(Lines.result(request_id, {"marking_version": net.marking_version}))

    def _sync_visible(self, net: _Net, out: Optional[Outgoing] = None) -> None:
        """Recompute the ids of the live tokens the connection's filter keeps.
        With ``out``, announce the difference from what the connection was shown:
        ``added`` for a token now kept that it did not have, ``removed`` for one
        it had that is kept no more. The token events of a batch keep ``visible``
        in step as they go, so what is left here is what no token event told of:
        a net parameter a clause names has changed (§13.4). Only the net's own
        places, its ports among them: a subnet's ports are its subnet's to project."""
        if net.filter is None:
            net.visible = set()
            return
        visible: dict[int, tuple[str, Any]] = {}
        for place in net.graph.places:
            if isinstance(place, SubnetPlaceNode) or not net.filter.covers(place.name):
                continue
            ids = place.token_ids
            for index, token in enumerate(place.tokens):
                if index < len(ids) and net.filter.keeps(place, token, net.graph.params):
                    visible[ids[index]] = (place.name, token)
        if out is not None:
            stamp = {"marking_version": net.marking_version, "running": self._loaded.running}
            for token_id in sorted(net.visible - set(visible)):
                found = TokenIds.locate(net.graph, token_id)
                if found is not None:  # still live, just no longer kept
                    self._emit(net, wire.Removed(place=found[0], id=str(token_id), **stamp), out, (found[0],))
            for token_id in sorted(set(visible) - net.visible):
                place_name, token = visible[token_id]
                self._emit(net, self._added(net, place_name, token_id, token, stamp), out, (place_name,))
        net.visible = set(visible)

    def _snapshot_of(self, net: _Net) -> Optional[_Snapshot]:
        flight = self._loaded.in_flight
        if flight is None:
            return None
        return flight.snapshots.get(net.nested_id)

    def _token_at_risk(self, net_id: Optional[int], params: Any) -> bool:
        """Whether a ``get_token`` names a token the airborne body was handed, or
        one that renders a net the airborne body is changing."""
        if not isinstance(params, dict) or not isinstance(params.get("id"), str):
            return False
        token_id = _token_id(params["id"])
        snapshot = self._loaded.in_flight.snapshots.get(net_id)
        if token_id is None or snapshot is None:
            return False
        for key, view in snapshot.places.items():
            if isinstance(key, Port):
                continue  # its subnet's ids: read through the subnet
            for index, candidate in enumerate(view.ids):
                if candidate == token_id:
                    return index in view.eager
        return False

    def _view(self, net: _Net, key: PlaceKey) -> _PlaceView:
        """The place ``net`` names ``key`` as it stands, seen as its owner sees it:
        a port as a place of its subnet (``_owner_of``)."""
        place = net.place_nodes[key]
        owner, own = self._owner_of(net, key)
        nested = own in owner.nested_places
        return _PlaceView(
            name=own,
            type_name=owner.type_names[own],
            ids=list(place.token_ids),
            tokens=list(place.tokens),
            nested=nested,
            prefix=self._path_of(owner) if nested else (),
            place=place,
            filter=owner.filter,
            params=dict(owner.graph.params),
        )

    def _subnet_entry(self, net: _Net, place: SubnetPlaceNode, views: dict[PlaceKey, _PlaceView]) -> wire.SubnetEntry:
        """A subnet place's ``subnet`` entry (§12.7). ``count`` is the tokens in the
        subnet's own places, its ports counted as ``views`` show them, so that a
        snapshot's count agrees with its ports."""
        subnet = self._subnet_of(net, place.name)
        inside = sum(len(node.tokens) for node in subnet.graph.places if isinstance(node, ListPlaceNode) and not node.port)
        at_ports = sum(views[Port(place.name, port.name)].count for port in place.ports)
        return wire.SubnetEntry(
            nested_id=str(subnet.nested_id), path=list(self._path_of(subnet)), template=subnet.graph.template,
            count=inside + at_ports,
        )

    def _capture(
        self, net: _Net, airborne: Optional[FunctionTransitionNode] = None, handover: _Handover | None = None,
        eager: Optional[dict[str, set[int]]] = None,
    ) -> _Snapshot:
        """The marking, transition states and progress of ``net`` as of now. With
        ``handover``, what a firing about to start will take and hand over: the
        tokens it takes are left out (and the transition states are those of the
        marking without them) and the tokens its mutate edges hand over are
        rendered at once; with ``airborne`` too, the firing is ``net``'s and its
        transition is shown ``in_flight``, and without, the firing is in a subnet
        of ``net`` and ``handover`` names that subnet's ports by their pairs.
        ``eager`` names further tokens (per place, by index) to render at once:
        those holding a net an airborne body is changing. Progress counts the
        firings of the net's own transitions (§5.4), under their names: a body
        that steps an inner net of its own through the library's deep step leaves
        counts under an ``InnerFiring`` on that net's graph, which are not the
        net's own."""
        graph = net.graph
        own = {name: count for name, count in graph.fired_counts.items() if isinstance(name, str)}
        places = {key: self._view(net, key) for key in net.place_nodes}
        for place_name, indices in (eager or {}).items():
            view = places[place_name]
            for index in indices:
                if index < view.count:
                    view.eager[index] = view.entry(index)
        if handover is None:
            transitions = self._transition_states(net)
        else:
            for place_name, indices in handover.mutated.items():
                view = places[place_name]
                for index in indices:
                    if index < view.count:
                        view.eager[index] = view.entry(index)
            for place_name, count in handover.taken.items():
                view = places[place_name]
                view.ids, view.tokens = view.ids[count:], view.tokens[count:]
                view.eager = {index - count: entry for index, entry in view.eager.items() if index >= count}
            for place_name, indices in handover.keyed_taken.items():
                view = places[place_name]
                gone = set(indices)
                view.ids = [token_id for index, token_id in enumerate(view.ids) if index not in gone]
                view.tokens = [token for index, token in enumerate(view.tokens) if index not in gone]
                view.eager = {}
                view._kept = None
            transitions = self._states_without(net, handover)
            if airborne is not None:
                transitions[airborne.name] = transitions[airborne.name].model_copy(
                    update={"in_flight": True, "fireable": False}
                )
        return _Snapshot(
            marking_version=net.marking_version,
            transitions=transitions,
            places=places,
            progress=wire.Progress(
                step_count=sum(own.values()),
                last_fired=net.last_fired,
                fired_counts=own,
                last_progress_at=_iso(net.last_progress_at) if net.last_progress_at else None,
            ),
            subnets={
                place.name: self._subnet_entry(net, place, places)
                for place in graph.places if isinstance(place, SubnetPlaceNode)
            },
        )

    def _handed_over(self, net: _Net, transition: FunctionTransitionNode) -> _Handover:
        """What firing ``transition`` now will take and hand over, by stage 1's own
        rules: argument edges in order, each taking the oldest remaining token of
        its place, or every remaining one for a ``list[...]`` argument or a drain
        (possibly none); then the mutate edges, which take nothing and hand over the
        oldest remaining token (or all of them) where they are. Places are keyed
        as the edges name them, so a port by its ``Port`` pair."""
        consumed: list[tuple[PlaceKey, int]] = []
        taken: dict[PlaceKey, int] = {}
        keyed_taken: dict[PlaceKey, list[int]] = {}  # per place, the indices a keyed take removes
        params = net.graph.params
        for edge in net.incoming.get(transition.name, ()):
            key = edge.place_node_name
            place = net.place_nodes[key]
            ids = place.token_ids
            if isinstance(edge, TakeWhereEdge):
                index = KeyedEdges.first_kept(edge, place, params)
                if index is not None:
                    consumed.append((key, ids[index]))
                    keyed_taken.setdefault(key, []).append(index)
                continue
            start = taken.get(key, 0)
            takes_all = isinstance(edge, DrainEdge) or Topologies.takes_all(transition.function, edge.argument, place)
            end = len(ids) if takes_all else min(start + 1, len(ids))
            consumed.extend((key, token_id) for token_id in ids[start:end])
            taken[key] = max(start, end)
        mutated: dict[PlaceKey, set[int]] = {}
        for edge in net.graph.mutate_edges:
            if edge.transition_node_name != transition.name:
                continue
            key = edge.place_node_name
            place = net.place_nodes[key]
            if isinstance(edge, MutateWhereEdge):
                index = KeyedEdges.first_kept(edge, place, params)
                if index is not None:
                    mutated.setdefault(key, set()).add(index)
                continue
            count = len(place.token_ids)
            start = taken.get(key, 0)
            takes_all = Topologies.takes_all(transition.function, edge.argument, place)
            end = count if takes_all else min(start + 1, count)
            mutated.setdefault(key, set()).update(range(start, end))
        return _Handover(consumed=consumed, taken=taken, mutated=mutated, keyed_taken=keyed_taken)

    def _held(
        self, net: _Net, transition: FunctionTransitionNode, handover: _Handover,
    ) -> tuple[set[int], set[tuple[Optional[int], str, int]]]:
        """What a firing of ``transition`` in ``net`` holds until it lands, for the
        reads that wait (§12.3): the tokens it takes or holds through mutate edges,
        each named by the net whose place holds it, and the inner nets inside
        them, at any depth; and the subnet of every subnet place one of whose
        ports the transition has an edge on, any edge (§12.7)."""
        nets: set[int] = set()
        tokens: set[tuple[Optional[int], str, int]] = set()
        pairs: list[tuple[PlaceKey, int]] = list(handover.consumed)
        for key, indices in handover.mutated.items():
            ids = net.place_nodes[key].token_ids
            pairs.extend((key, ids[index]) for index in indices if index < len(ids))
        for key, token_id in pairs:
            owner, own = self._owner_of(net, key)
            tokens.add((owner.nested_id, own, token_id))
            if key not in net.nested_places:
                continue
            place = net.place_nodes[key]
            ids = place.token_ids
            if token_id in ids:
                nets.update(_nets_within(place.tokens[ids.index(token_id)]))
        for place in _subnets_reached(net, transition.name):
            nets.add(self._subnet_of(net, place).nested_id)
        return nets, tokens

    def _states_without(self, net: _Net, handover: _Handover) -> dict[str, wire.TransitionState]:
        """The transition states of the marking with the tokens the firing takes
        gone from their places (from the front, or wherever a keyed take finds
        them): the marking the body runs against. The live lists are cut and put
        back in place (nothing else runs on them meanwhile, as the body has not
        started), so guards and priorities see exactly what they will see once
        stage 1 has run."""
        saved = []
        try:
            for place_name in set(handover.taken) | set(handover.keyed_taken):
                place = net.place_nodes[place_name]
                saved.append((place, list(place.tokens), list(place._token_ids)))
                count = handover.taken.get(place_name, 0)
                gone = set(range(count)) | set(handover.keyed_taken.get(place_name, ()))
                place.tokens = [token for index, token in enumerate(place.tokens) if index not in gone]
                place._token_ids = [token_id for index, token_id in enumerate(place._token_ids) if index not in gone]
            return self._transition_states(net)
        finally:
            for place, tokens, ids in saved:
                place.tokens = tokens
                place._token_ids = ids

    # --- transition states and selection ---

    def _is_fireable(self, net: _Net, transition: FunctionTransitionNode) -> bool:
        if transition.name in net.ctx.disabled or transition.name in net.graph.in_flight:
            return False
        try:
            return ExecutableGraphCheck.transition_is_fireable(
                transition=transition,
                executable_graph=net.graph,
                transition_names_to_incoming_edges=net.incoming,
                place_names_to_nodes=net.place_nodes,
                transition_names_to_read_edges=net.read,
            )
        except Exception:
            return False  # a guard that raises cannot let its transition fire

    def _fireable_names(self, net: _Net) -> list[str]:
        return [t.name for t in net.graph.transitions if self._is_fireable(net, t)]

    def _transition_states(self, net: _Net) -> dict[str, wire.TransitionState]:
        graph = net.graph
        states = {}
        for transition in graph.transitions:
            priority = None
            try:
                priority = float(transition.priority(graph)) if transition.priority is not None else 0.0
            except Exception:
                priority = None
            states[transition.name] = wire.TransitionState(
                enabled=transition.name not in net.ctx.disabled and transition.name not in graph.disabled,
                fireable=self._is_fireable(net, transition),
                in_flight=transition.name in graph.in_flight,
                priority=priority,
            )
        return states

    def _select_in(self, net: _Net) -> Optional[FunctionTransitionNode]:
        """The transition ``net``'s own selector would fire now, among its
        fireable and enabled ones; ``None`` if there is none. A selector or
        priority that raises propagates."""
        graph = net.graph
        fireable = [t for t in graph.transitions if self._is_fireable(net, t)]
        if not fireable:
            return None
        return (graph.transition_selector or default_transition_selector)(graph, fireable)

    def _select(self) -> Optional[tuple[_Net, FunctionTransitionNode]]:
        """The next firing anywhere: one deep step of the loaded net (§12.3),
        chosen rather than fired (``_choose``)."""
        return self._choose(self._loaded.outer)

    def _choose(self, net: _Net) -> Optional[tuple[_Net, FunctionTransitionNode]]:
        """One deep step of ``net`` in the order the stepping rules of its places
        give, as the library's ``fire_one_transition`` with ``descend`` takes it,
        except that what it comes to is chosen and not fired: the turns ahead of
        ``net``'s own (its inner-first places, then the ring's members after its
        cursor), its own selector's choice, then the turns behind it (the rest of
        the ring, then its outer-first places; nothing is in flight while the
        owner chooses, one firing being airborne at a time). A member's turn is
        that net's deep step, chosen the same way, so every level follows the
        rules of its own places. Each net chooses by its own selector, so a
        round-robin tie-break moves only the cursor of the net that fires. The
        choice is launched at once, so it is the turn: each ring's cursor moves
        as the library moves it (``NestedNets.took_own_turn``, ``took_turn``)."""
        graph = net.graph
        nested = NestedNets.any_nested(graph)
        if nested:
            found = self._choose_among(net, "ahead")
            if found is not None:
                return found
        chosen = self._select_in(net)
        if chosen is not None:
            NestedNets.took_own_turn(graph)
            return net, chosen
        return self._choose_among(net, "behind") if nested else None

    def _choose_among(self, net: _Net, part: str) -> Optional[tuple[_Net, FunctionTransitionNode]]:
        """The first member of one part of ``net``'s round (``NestedNets.turns``)
        whose deep step chooses something, and what it chose."""
        for member in NestedNets.turns(net.graph, part):
            inner = self._net_for(member.net, (*net.last_path, Paths.of_step(member.step)))
            found = self._choose(inner)
            if found is not None:
                NestedNets.took_turn(net.graph, member)
                return found
        return None

    # --- the firing cycle ---

    def _step(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        try:
            choice = self._select()
        except Exception as error:
            raise ProtocolError(
                ErrorCode.INTERNAL, f"selecting a transition raised {type(error).__name__}: {error}"
            ) from error
        if choice is None:
            out.lines.append(Lines.result(request_id, wire.StepResult(
                fired=False, marking_version=self.marking_version, running=self._loaded.running,
            )))
            return
        net, transition = choice
        self._launch(net, transition, request_id, now, out)

    def _start(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        self._loaded.running = True
        self._loaded.idle = False
        out.lines.append(Lines.result(request_id, {
            "status": "started", "marking_version": self.marking_version, "running": True,
        }))

    def _stop(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        self._loaded.running = False
        out.lines.append(Lines.result(request_id, {
            "status": "stopped", "marking_version": self.marking_version, "running": False,
        }))

    def _launch(self, net: _Net, transition: FunctionTransitionNode, step_id: Optional[str], now: datetime,
                out: Outgoing, press_id: Optional[str] = None) -> None:
        """Write ``launched`` for ``transition`` in ``net`` and hand its firing
        out; the ``step`` (``step_id``) or ``press`` (``press_id``) that launched it
        is answered when it lands. The fireable set is compared when the firing lands, not now: the
        launch's own change is transient, and a ``fireable`` between ``launched``
        and ``distributed`` would split the pair a viewer folds into ``fired``."""
        loaded = self._loaded
        # The engine resyncs ids before it takes anything; do it first, so the
        # ids ``launched`` names are the ones it will take.
        TokenIds.resync(net.graph)
        self._flush(now, out)
        handover = self._handed_over(net, transition)
        net.marking_version += 1
        shown = self._shown(net, handover.consumed)
        net.launched_shown = set(shown)
        launched = wire.Launched(
            transition=transition.name, consumed=_pairs(shown),
            marking_version=net.marking_version, running=loaded.running,
        )
        # What it takes from a port moves the version of the net across the port too.
        self._cross(net, launched, [key for key, _token_id in handover.consumed])
        for key, token_id in handover.consumed:
            owner, _own = self._owner_of(net, key)
            owner.visible.discard(token_id)
        out.lines.append(self._wrap(net, launched.line()))
        snapshots = {net.nested_id: self._capture(net, airborne=transition, handover=handover)}
        held_nets, held_tokens = self._held(net, transition, handover)
        if net.parent is not None:
            # A subnet's firing that takes from or hands over a token of its own
            # port changes what the net around it reads there: that net is read
            # from a snapshot of the port as the firing leaves it.
            lifted = _lifted(net, handover)
            if lifted is not None:
                snapshots[net.parent.nested_id] = self._capture(net.parent, handover=lifted)
        if not net.is_outer:
            # The tokens holding this net, up to the outer net, render it in their labels.
            for ancestor, place_name, index in self._chain(net):
                if ancestor.nested_id not in snapshots:
                    snapshots[ancestor.nested_id] = self._capture(ancestor, eager={place_name: {index}})
        loaded.in_flight = _InFlight(
            net.nested_id, transition.name, now, step_id, snapshots, handover.consumed, held_nets, held_tokens,
            press_id=press_id, path=self._path_of(net),
        )
        loaded.idle = False
        out.launch = Launch(transition.name, _fire(net, transition, self.generation))

    def _shown(self, net: _Net, pairs: list[tuple[PlaceKey, int]]) -> list[tuple[PlaceKey, int]]:
        """The ``(place, id)`` pairs of live tokens the connection's filter keeps
        (all of them without a filter), a port's by its subnet's filter."""
        kept = []
        for key, token_id in pairs:
            owner, _own = self._owner_of(net, key)
            if owner.filter is None:
                kept.append((key, token_id))
                continue
            token = self._live(net, key, token_id)
            if token is not _MISSING and self._keeps(net, key, token):
                kept.append((key, token_id))
        return kept

    def _chain(self, net: _Net) -> list[tuple[_Net, str, int]]:
        """From the outer net down to the parent of ``net``: each ancestor with
        the place and index of the token that holds the next level. A step into
        a subnet has no token, so it adds nothing and the chain goes on below it."""
        loaded = self._loaded
        found = NestedNets.find(loaded.outer.graph, net.nested_id)
        steps = Paths.of_steps(found[0]) if found is not None else net.last_path
        chain = []
        graph = loaded.outer.graph
        for level, step in enumerate(steps):
            ancestor = self._net_for(graph, steps[:level])
            if step.token is None:
                graph = graph.subnet(step.place)
                if graph is None:
                    break
                continue
            place = ancestor.place_nodes.get(step.place)
            token_id = _token_id(step.token) if step.token is not None else None
            if place is None or token_id is None:
                break
            ids = place.token_ids
            if token_id not in ids:
                break
            index = ids.index(token_id)
            chain.append((ancestor, step.place, index))
            graph = NestedNets.of(place.tokens[index]).get(step.net)
            if graph is None:
                break
        return chain

    def _land(self, landing: Landing, now: datetime, out: Outgoing) -> None:
        """Report a landed firing: its events, the log entry, progress, a stop on
        failure, and the ``step`` that waited for it."""
        loaded = self._loaded
        flight = loaded.in_flight
        loaded.in_flight = None
        net = self._net_by_id(flight.net_id) or loaded.outer
        # A body that resized a place through a whole-list mutate edge is repaired
        # here, so the landing's lines already say so.
        TokenIds.resync(net.graph)
        events = self._flush(now, out)
        # The nets inside the tokens this firing produced are known from its landing.
        self._know_new()
        fired, failed, path = self._firing_of(events, flight)
        if fired is not None or failed is not None:
            announced, landed = list(flight.consumed), list((fired or failed).consumed)
            if announced != landed:
                print(
                    f"petritype runner: {flight.name} was launched as consuming {announced} "
                    f"but the engine took {landed}",
                    file=sys.stderr, flush=True,
                )
        else:
            self._never_took(net, flight, landing, out)

        if fired is not None or failed is not None:
            loaded.log_seq += 1
            consumed = (fired or failed).consumed
            chain = () if net.is_outer else self._id_chain(path)
            loaded.log.append(_Entry(wire.LogEntry(
                seq=loaded.log_seq,
                transition=flight.name,
                started_at=_iso(flight.started_at),
                duration_ms=fired.duration_ms if fired is not None else _ms(flight.started_at, now),
                outcome="fired" if fired is not None else "failed",
                consumed=_pairs(consumed),
                produced=_pairs(fired.produced) if fired is not None else [],
                error=failed.error if failed is not None else None,
                restored=failed.restored if failed is not None else None,
                path=list(path) if not net.is_outer else None,
                nested_id=str(net.nested_id) if not net.is_outer else None,
            ), chain))
        if fired is not None:
            # Progress is per net (§5.4): the net that fired counts it, under the
            # transition's own name (the engine's ``step_count`` and
            # ``fired_counts`` of that graph), and no net around it does.
            net.last_fired = flight.name
            net.last_progress_at = now

        if failed is not None or (fired is None and landing.error is not None):
            if loaded.running:
                cause = _cause(landing.error) if landing.error is not None else None
                loaded.running = False
                out.lines.append(wire.ExecutionStopped(
                    reason="transition_failed",
                    error_type=type(cause).__name__ if cause is not None else "Error",
                    message=str(cause) if cause is not None else failed.error,
                    marking_version=self.marking_version, running=False,
                ).line())

        if flight.press_id is not None:
            self._pressed(flight, fired is not None or failed is not None, landing, out)
            return
        if flight.step_id is None:
            return
        if fired is None and failed is None and landing.error is not None:
            # It raised before taking anything (a guard or selector inside the
            # engine): no firing landed, so there is nothing to call fired.
            out.lines.append(Lines.error(
                flight.step_id, ErrorCode.INTERNAL,
                f"firing {flight.name} raised {type(landing.error).__name__}: {landing.error}",
            ))
            return
        landed = fired is not None or failed is not None
        out.lines.append(Lines.result(flight.step_id, wire.StepResult(
            fired=landed,
            transition=flight.name if landed else None,
            failed=True if failed is not None else None,
            path=list(path) if landed and not net.is_outer else None,
            nested_id=str(net.nested_id) if landed and not net.is_outer else None,
            marking_version=self.marking_version,
            running=loaded.running,
        )))

    def _firing_of(self, events: list, flight: _InFlight):
        """The ``Fired`` or ``Failed`` of this flight among the events just
        drained, and, for an inner firing, the path it was recorded under."""
        fired = failed = None
        path: tuple[wire.PathStep, ...] = ()
        for event in events:
            inner = None
            if flight.net_id is None:
                inner = event if isinstance(event, (core_events.Fired, core_events.Failed)) else None
            elif isinstance(event, core_events.Nested) and event.nested_id == flight.net_id:
                inner = event.event
            if inner is None or inner.transition != flight.name:
                continue
            if isinstance(inner, core_events.Fired) and fired is None:
                fired = inner
            elif isinstance(inner, core_events.Failed) and failed is None:
                failed = inner
            else:
                continue
            if isinstance(event, core_events.Nested):
                path = Paths.of_steps(event.path)
        if flight.net_id is not None and not path:
            net = self._loaded.inner.get(flight.net_id)
            path = net.last_path if net is not None else ()
        return fired, failed, path

    def _id_chain(self, steps: tuple[wire.PathStep, ...]) -> tuple[int, ...]:
        """The nested ids down a path, outermost first, for the log's filter."""
        chain = []
        graph = self._loaded.outer.graph
        for step in steps:
            try:
                graph = _descend(graph, step)
            except LookupError:
                break
            chain.append(graph.net_id)
        return tuple(chain)

    def _never_took(self, net: _Net, flight: _InFlight, landing: Landing, out: Outgoing) -> None:
        """Close a ``launched`` whose firing landed without the engine reporting
        one: it raised before taking anything (a guard or selector inside the
        engine), or found the transition no longer fireable. The viewer is told
        ``failed``, ``restored`` when every announced token is where it was; the
        rest of the landing (no log entry, the ``step`` answer) goes on as for
        a firing that never happened."""
        restored = all(
            token_id in net.place_nodes[place].token_ids for place, token_id in flight.consumed
        )
        error = (
            f"{type(landing.error).__name__}: {landing.error}" if landing.error is not None
            else f"{flight.name} was no longer fireable when its firing ran"
        )
        failed = core_events.Failed(
            transition=flight.name, consumed=tuple(flight.consumed), restored=restored, error=error,
        )
        self._emit(net, self._notification(net, failed), out, _changed_by(failed))
        if not restored:
            self._emit(net, wire.Reloaded(
                generation=self.generation if net.is_outer else 1, reason="resync",
                marking_version=net.marking_version, running=self._loaded.running,
            ), out)

    def _drive(self, now: datetime, out: Outgoing) -> None:
        """Keep a running net going: launch the next firing if nothing is
        airborne, or handle quiescence (``idle`` once per spell for a ``"24/7"``
        net, ``execution_stopped`` for any other)."""
        loaded = self._loaded
        if loaded is None or not loaded.running or loaded.in_flight is not None:
            return
        try:
            choice = self._select()
        except Exception as error:
            loaded.running = False
            out.lines.append(wire.ExecutionStopped(
                reason="transition_failed", error_type=type(error).__name__, message=str(error),
                marking_version=self.marking_version, running=False,
            ).line())
            return
        if choice is not None:
            net, transition = choice
            self._launch(net, transition, None, now, out)
        elif loaded.entry.mode == CONTINUOUS_MODE:
            if not loaded.idle:
                loaded.idle = True
                out.lines.append(wire.Idle(
                    idle_since=_iso(now), marking_version=self.marking_version, running=True,
                ).line())
        else:
            loaded.running = False
            out.lines.append(wire.ExecutionStopped(
                reason="no_fireable_transitions", marking_version=self.marking_version, running=False,
            ).line())

    # --- input ---

    def _inject(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        """``inject {place, port?, token}``: a port by its pair on the net around
        its subnet, which tells the change, or by its own name on the subnet,
        which does (§12.7); the port's subnet mints the id either way."""
        place = _string(params, "place")
        port = _optional_string(params, "port")
        if "token" not in params:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, 'inject needs a "token"')
        net = self._target(params)
        key = self._place_key(net, place, port)
        node = net.place_nodes[key]
        if not net.serializable[key]:
            raise ProtocolError(
                ErrorCode.NOT_SERIALIZABLE,
                f"{'' if isinstance(key, Port) else 'place '}{Places.words(key)} ({net.type_names[key]}) has no JSON form",
            )
        try:
            token = Serializable.decode(node.type, params["token"])
        except ValueError as error:
            raise ProtocolError(ErrorCode.INVALID_TOKEN, f"not a {net.type_names[key]}: {error}") from error
        try:
            self._apply(net, Extend(place=key, tokens=[token]))
        except ValueError as error:  # the strict context's own type check
            raise ProtocolError(ErrorCode.INVALID_TOKEN, str(error)) from error
        events = self._flush(now, out)
        added = next((
            e for e in _events_in(events, net) if isinstance(e, core_events.Added) and e.place == key
        ), None)
        if added is None:
            raise ProtocolError(ErrorCode.INTERNAL, "the token was added without an event")
        # The nets the token carried are known from here: their fireable baselines (§12.3).
        self._know_new()
        injected = {"status": "injected", "id": str(added.token_id), "marking_version": net.marking_version}
        if not net.is_outer:
            # A result about an inner net names it (§12.3), the version its own.
            injected["path"] = Paths.to_wire(self._path_of(net))
            injected["nested_id"] = str(net.nested_id)
        out.lines.append(Lines.result(request_id, injected))

    def _press(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        """``press {control, value?}`` (§6): run one of the net's declared controls.
        The control is looked up (``unknown_control``) and the value checked by its
        kind (``Controls.value``, ``invalid_params``); its ``to_command`` gives the
        command, handed the net when it takes it. ``None`` answers ``nothing``. A
        ``Fire`` is a pinned ``step``: launched as a step launches, so ``fired``,
        progress and ``fireable`` follow as for a step, and answered when it lands
        (``_pressed``), or ``nothing`` at once when the transition is not fireable;
        being no deep step, it leaves every ring's cursor where it is. Any other command goes through the engine's command path (``_apply``) and
        its events out as an ``inject``'s do. A ``to_command`` that raises, or a
        command the net refuses, is ``internal``."""
        loaded = self._loaded
        name = _string(params, "control")
        spec = loaded.controls.get(name)
        if spec is None:
            raise ProtocolError(ErrorCode.UNKNOWN_CONTROL, f"no control named {name!r}")
        value = Controls.value(spec, params)
        outer = loaded.outer
        try:
            command = ControlSpec.command(spec, value, outer.graph)
        except Exception as error:
            raise ProtocolError(
                ErrorCode.INTERNAL, f"control {name!r} raised {type(error).__name__}: {error}",
            ) from error
        if command is None:
            self._nothing(request_id, f"{name} has nothing to do", out)
            return
        if isinstance(command, Fire):
            transition = next((t for t in outer.graph.transitions if t.name == command.transition), None)
            if transition is None:
                raise ProtocolError(
                    ErrorCode.INTERNAL, f"control {name!r} fires {command.transition!r}, no transition of the net",
                )
            if not self._is_fireable(outer, transition):
                self._nothing(request_id, f"{transition.name} is not fireable", out)
                return
            # A plain firing of the outer net, not a deep step: no ring's cursor moves.
            self._launch(outer, transition, None, now, out, press_id=request_id)
            return
        self._apply(outer, command)
        self._flush(now, out)
        # The nets a token it added carried are known from here, as for an ``inject``.
        self._know_new()
        out.lines.append(Lines.result(request_id, wire.PressResult(
            status="pressed", marking_version=self.marking_version,
        )))

    def _nothing(self, request_id: str, reason: str, out: Outgoing) -> None:
        out.lines.append(Lines.result(request_id, wire.PressResult(
            status="nothing", reason=reason, marking_version=self.marking_version,
        )))

    def _pressed(self, flight: _InFlight, landed: bool, landing: Landing, out: Outgoing) -> None:
        """Answer the ``press`` whose ``Fire`` just landed: ``pressed`` when it fired
        or failed (the ``failed`` already says which), ``internal`` when it raised
        before taking anything, ``nothing`` when it found the transition no longer
        fireable."""
        if landed:
            out.lines.append(Lines.result(flight.press_id, wire.PressResult(
                status="pressed", marking_version=self.marking_version,
            )))
        elif landing.error is not None:
            out.lines.append(Lines.error(
                flight.press_id, ErrorCode.INTERNAL,
                f"firing {flight.name} raised {type(landing.error).__name__}: {landing.error}",
            ))
        else:
            self._nothing(flight.press_id, f"{flight.name} is not fireable", out)

    def _enable(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        self._toggle(Enable, request_id, params, now, out)

    def _disable(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        self._toggle(Disable, request_id, params, now, out)

    def _toggle(self, command, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        transition = _string(params, "transition")
        self._transition(transition)
        self._apply(self._loaded.outer, command(transition=transition))
        self._flush(now, out)
        out.lines.append(Lines.result(request_id, {"marking_version": self.marking_version}))

    def _set_param(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        """``set_param``: with ``transition``, one keyword argument of one
        transition, through ``SetParam`` (the parameter must be one the function
        takes and no edge supplies; an annotated one is validated); without,
        one of the net's own parameters (``graph.params``), through
        ``SetNetParam``, which must be declared (``invalid_params`` otherwise,
        §6.4) and decodes by its declared type (``_net_param_value``)."""
        param = _string(params, "param")
        if "value" not in params:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, 'set_param needs a "value"')
        outer = self._loaded.outer
        if "transition" not in params:
            if param not in outer.graph.params:
                raise ProtocolError(ErrorCode.INVALID_PARAMS, f"the net declares no parameter {param!r}")
            value = self._net_param_value(outer.graph.params[param], param, params["value"])
            self._apply(outer, SetNetParam(param=param, value=value))
        else:
            if not isinstance(params.get("transition"), str):
                raise ProtocolError(ErrorCode.INVALID_PARAMS, 'set_param needs a string "transition" or none')
            transition = self._transition(params["transition"])
            value = self._param_value(transition, param, params["value"])
            self._apply(outer, SetParam(transition=transition.name, kwargs={param: value}))
        self._flush(now, out)
        out.lines.append(Lines.result(request_id, {"marking_version": self.marking_version}))

    def _net_param_value(self, current: Any, param: str, value: Any) -> Any:
        """A net parameter decodes by its declared type (§6.4, since 2026-10-12):
        the annotation of the factory parameter of the same name, validated
        strictly (``NetEntries.decode``), so ``destination: str | None = None``
        takes a string or ``null`` in either order and refuses a number. A
        parameter the factory does not annotate keeps the JSON type of its current
        value: a scalar is validated strictly against that type; anything else is
        taken as given."""
        spec = next((spec for spec in self._loaded.entry.params if spec.name == param), None)
        if spec is not None and NetEntries.adapter(spec) is not None:
            return NetEntries.decode(spec, value)
        if isinstance(current, JSON_SCALARS):
            try:
                return StrictJson.validate(TypeAdapter(type(current)), value)
            except ValueError as error:
                raise ProtocolError(
                    ErrorCode.INVALID_PARAMS,
                    f"{param!r} must be {type(current).__name__}: {StrictJson.message(error)}",
                ) from error
        return value

    def _param_value(self, transition: FunctionTransitionNode, param: str, value: Any) -> Any:
        outer = self._loaded.outer
        supplied = {
            edge.argument
            for edges in (outer.incoming.get(transition.name, ()), outer.read.get(transition.name, ()))
            for edge in edges
        }
        if param in supplied:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS, f"{param!r} of {transition.name!r} is supplied by an edge, not a parameter",
            )
        try:
            parameters = inspect.signature(transition.function).parameters
        except (TypeError, ValueError):
            return value
        parameter = parameters.get(param)
        if parameter is None:
            if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                return value
            raise ProtocolError(ErrorCode.INVALID_PARAMS, f"{transition.name!r} takes no parameter {param!r}")
        annotation = parameter.annotation
        if annotation is inspect.Parameter.empty or isinstance(annotation, str):
            return value
        try:
            adapter = TypeAdapter(annotation)
        except Exception:
            return value
        try:
            return StrictJson.validate(adapter, value)
        except ValueError as error:
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS, f"{param!r} must be {TypeNames.of(annotation)}: {StrictJson.message(error)}",
            ) from error

    def _transition(self, name: str) -> FunctionTransitionNode:
        for transition in self._loaded.outer.graph.transitions:
            if transition.name == name:
                return transition
        raise ProtocolError(ErrorCode.UNKNOWN_TRANSITION, f"no transition named {name!r}")

    def _apply(self, net: _Net, command) -> None:
        """Through the engine's command path: the net's inbox, drained at once."""
        net.ctx.inbox.put_nowait(command)
        Runner.drain_inbox(net.ctx)

    # --- events ---

    def _flush(self, now: datetime, out: Outgoing) -> list:
        """Drain the outer graph's events (inner nets' included, wrapped as
        ``Nested``) and write one notification per event, each state
        notification raising its net's ``marking_version`` by one; then
        ``fireable`` for the outer net and for each inner net touched, if
        anything happened and the set changed. A change of a port place raises
        the version of the net across the port too (``_cross``), and touches it,
        so that its ``fireable`` is compared as well; a net's ``fireable`` comes
        before those of the nets inside it (§12.4, §12.7). Returns the events."""
        loaded = self._loaded
        outer = loaded.outer
        events = outer.graph.drain_events()
        touched: list[_Net] = []

        def touch(net: _Net) -> None:
            if net is not outer and net not in touched:
                touched.append(net)

        for event in events:
            if isinstance(event, core_events.Nested):
                inner = self._net_by_id(event.nested_id)
                notification = self._notification(inner, event.event)
                path = Paths.of_steps(event.path)
                if inner is not None:
                    inner.last_path = path
                    if notification.moves_version:
                        inner.marking_version += 1
                        for across in self._cross(inner, notification, _changed_by(event.event)):
                            touch(across)
                    notification.marking_version = inner.marking_version
                    touch(inner)
                # The wrapper carries the loaded net's version, moved when the event
                # changed one of its port places.
                out.lines.append(wire.NestedEvent(
                    path=list(path), nested_id=str(event.nested_id), event=notification.line(),
                    marking_version=outer.marking_version, running=loaded.running,
                ).line())
            else:
                notification = self._notification(outer, event)
                if notification.moves_version:
                    outer.marking_version += 1
                    notification.marking_version = outer.marking_version
                    for across in self._cross(outer, notification, _changed_by(event)):
                        touch(across)
                out.lines.append(notification.line())
        if events:
            # Shallower nets first, so a net's ``fireable`` precedes those inside it.
            touched.sort(key=lambda net: len(self._path_of(net)))
            for net in (outer, *touched):
                if net.filter is not None:
                    self._sync_visible(net, out)
            self._report_fireable(outer, out)
            for inner in touched:
                self._report_fireable(inner, out)
        return events

    def _cross(self, net: _Net, notification: wire.Notification, changed) -> list[_Net]:
        """A state notification of ``net`` that changed the places ``changed``
        (keyed as ``net`` names them), whose own version the caller has raised: a
        port place is in the markings of two nets, so for each port it changed the
        version of the net across the port rises by one too, and the notification
        carries it (§12.7). A port of one of ``net``'s subnet places, named by its
        pair, moves that subnet, given as ``subnet_marking_versions``; a port place
        of ``net``'s own, when ``net`` is a subnet, moves the net around it, given
        as ``outer_marking_version``. Each net moves once per notification, however
        many of its tokens changed. Returns the nets moved, the net around first."""
        across: list[_Net] = []
        versions: dict[str, int] = {}
        outer_version: Optional[int] = None
        for key in changed:
            if isinstance(key, Port):
                if key.place not in versions:
                    subnet = self._subnet_of(net, key.place)
                    subnet.marking_version += 1
                    versions[key.place] = subnet.marking_version
                    across.append(subnet)
            elif outer_version is None and net.parent is not None:
                node = net.place_nodes.get(key)
                if node is not None and node.port:
                    net.parent.marking_version += 1
                    outer_version = net.parent.marking_version
                    across.insert(0, net.parent)
        if versions:
            notification.subnet_marking_versions = versions
        if outer_version is not None:
            notification.outer_marking_version = outer_version
        return across

    def _report_fireable(self, net: _Net, out: Outgoing) -> None:
        fireable = self._fireable_names(net)
        if fireable != net.last_fireable:
            net.last_fireable = fireable
            self._emit(net, wire.Fireable(
                transitions=fireable, marking_version=net.marking_version, running=self._loaded.running,
            ), out)

    def _emit(self, net: _Net, notification: wire.Notification, out: Outgoing, changed=()) -> None:
        """Write a notification of ``net``, raising its version if the
        notification moves one, and the versions across the ports among the
        places ``changed`` (``_cross``), wrapped in ``nested`` for an inner net."""
        if notification.moves_version:
            net.marking_version += 1
            notification.marking_version = net.marking_version
            self._cross(net, notification, changed)
        out.lines.append(self._wrap(net, notification.line()))

    def _wrap(self, net: _Net, line: dict) -> dict:
        """An inner net's line inside a ``nested`` wrapper; an outer net's as is."""
        if net.is_outer:
            return line
        loaded = self._loaded
        return wire.NestedEvent(
            path=list(self._path_of(net)), nested_id=str(net.nested_id), event=line,
            marking_version=loaded.outer.marking_version, running=loaded.running,
        ).line()

    def _notification(self, net: Optional[_Net], event) -> wire.Notification:
        """One event as its notification, stamped with the net's current version
        (the caller raises it for a state notification). ``net`` is ``None`` for
        an inner net that no longer exists, whose tokens cannot be rendered. A
        place the event names by a ``Port`` pair is told with ``port`` beside
        ``place``, and a port's token is projected by its subnet's filter and
        counted in its subnet's visible set (§12.7)."""
        version = net.marking_version if net is not None else 0
        stamp = {"marking_version": version, "running": self._loaded.running}

        def owner(key: PlaceKey) -> _Net:
            return self._owner_of(net, key)[0]

        if isinstance(event, core_events.Fired):
            # The second piece: ``launched`` already named what it consumed.
            produced = event.produced if net is None else self._shown(net, list(event.produced))
            if net is not None:
                for key, token_id in produced:
                    if owner(key).filter is not None:
                        owner(key).visible.add(token_id)
            return wire.Distributed(
                transition=event.transition,
                produced=[self._produced(net, place, token_id) for place, token_id in produced],
                duration_ms=event.duration_ms,
                **stamp,
            )
        if isinstance(event, core_events.Failed):
            consumed = list(event.consumed)
            if net is not None:
                # Projected as its ``launched`` was: the tokens it showed (§13.4).
                consumed = [(key, token_id) for key, token_id in consumed
                            if owner(key).filter is None or (key, token_id) in net.launched_shown]
                if event.restored:
                    # Back where they were, ids and all: shown again, and no ``added`` for them.
                    for key, token_id in consumed:
                        if owner(key).filter is not None:
                            owner(key).visible.add(token_id)
            return wire.Failed(
                transition=event.transition, error=event.error, restored=event.restored,
                consumed=_pairs(consumed), **stamp,
            )
        if isinstance(event, core_events.Added):
            token = self._live(net, event.place, event.token_id)
            if net is not None and owner(event.place).filter is not None:
                if token is _MISSING or not self._keeps(net, event.place, token):
                    return wire.Hidden(kind="added", **_where(event.place), **stamp)
                owner(event.place).visible.add(event.token_id)
            return self._added(net, event.place, event.token_id, token, stamp)
        if isinstance(event, core_events.Removed):
            if net is not None and owner(event.place).filter is not None:
                if event.token_id not in owner(event.place).visible:
                    return wire.Hidden(kind="removed", **_where(event.place), **stamp)
                owner(event.place).visible.discard(event.token_id)
            return wire.Removed(**_where(event.place), id=str(event.token_id), **stamp)
        if isinstance(event, core_events.Mutated):
            token = self._live(net, event.place, event.token_id)
            if net is not None and owner(event.place).filter is not None:
                visible = owner(event.place).visible
                kept = token is not _MISSING and self._keeps(net, event.place, token)
                was_shown = event.token_id in visible
                if kept and not was_shown:
                    visible.add(event.token_id)
                    return self._added(net, event.place, event.token_id, token, stamp)
                if not kept and was_shown:
                    visible.discard(event.token_id)
                    return wire.Removed(**_where(event.place), id=str(event.token_id), **stamp)
                if not kept:
                    return wire.Hidden(kind="mutated", **_where(event.place), **stamp)
            label = Tokens.entry(event.token_id, token, "").label if token is not _MISSING else None
            keys = self._keys(net, event.place, token)
            return wire.Mutated(**_where(event.place), id=str(event.token_id), label=label, keys=keys, **stamp)
        if isinstance(event, core_events.Toggled):
            return wire.Toggled(transition=event.transition, enabled=event.enabled, **stamp)
        if isinstance(event, core_events.ParamChanged):
            return wire.ParamChanged(param=event.param, transition=event.transition, **stamp)
        # Resynced, or an event this runner does not know: the marking moved in a
        # way it cannot describe.
        generation = self.generation if net is None or net.is_outer else 1
        return wire.Reloaded(generation=generation, reason="resync", **stamp)

    def _added(self, net: Optional[_Net], place: PlaceKey, token_id: int, token: Any, stamp: dict) -> wire.Added:
        entry = Tokens.entry(token_id, token, "") if token is not _MISSING else None
        return wire.Added(
            **_where(place), id=str(token_id),
            label=entry.label if entry else "", color=entry.color if entry else None,
            keys=self._keys(net, place, token), **stamp,
        )

    def _keys(self, net: Optional[_Net], place: PlaceKey, token: Any) -> Optional[dict]:
        """A live token's keys when its place declares some."""
        if net is None or token is _MISSING:
            return None
        node = net.place_nodes.get(place)
        return Tokens.keys(node, token) if node is not None else None

    def _produced(self, net: Optional[_Net], place: PlaceKey, token_id: int) -> list[Any]:
        token = self._live(net, place, token_id)
        if token is _MISSING:
            return [Places.ref(place), str(token_id), ""]
        return Tokens.produced(place, token_id, token, keys=self._keys(net, place, token))

    def _live(self, net: Optional[_Net], place: PlaceKey, token_id: int) -> Any:
        node = net.place_nodes.get(place) if net is not None else None
        if node is None:
            return _MISSING
        ids = node.token_ids
        for index in range(len(ids) - 1, -1, -1):  # new tokens are at the back
            if ids[index] == token_id:
                return node.tokens[index] if index < len(node.tokens) else _MISSING
        return _MISSING


# --- the firing coroutine ------------------------------------------------------------------


async def _fire(net: _Net, transition: FunctionTransitionNode, generation: int) -> Landing:
    """Fire ``transition`` in ``net`` through the engine's one-firing primitive,
    with a selector pinned to it (the owner already ran the net's own selector)
    and no descent into inner nets (the owner chose the net). The engine records
    the firing's events; the owner reads them on landing."""
    graph = net.graph
    graph.last_fired = None

    def pinned(_graph, fireable):
        return transition if any(t is transition for t in fireable) else None

    try:
        await ExecutableGraphOperations.fire_one_transition(
            executable_graph=graph,
            place_names_to_nodes=net.place_nodes,
            transition_names_to_incoming_edges=net.incoming,
            transition_names_to_outgoing_edges=net.outgoing,
            transition_names_to_read_edges=net.read,
            transition_selector=pinned,
            executor=net.ctx.thread_pool,
            descend=False,
        )
    except Exception as error:
        return Landing(transition.name, generation, net.nested_id, error)
    return Landing(transition.name, generation, net.nested_id)


# --- helpers -------------------------------------------------------------------------------


_MISSING = object()


def _cause(error: BaseException) -> BaseException:
    """The user's exception behind a ``TransitionFailedError``."""
    if isinstance(error, TransitionFailedError) and error.__cause__ is not None:
        return error.__cause__
    return error


def _descend(graph: ExecutableGraph, step: wire.PathStep) -> ExecutableGraph:
    """The net one step of a path below ``graph`` (§12.1), or ``LookupError``
    saying why the step names none. A step of the form ``{place, token, net}``
    names the net ``net`` in the token ``token`` of the nested place ``place``; a
    step of the form ``{place}`` names the subnet of the subnet place ``place``.
    Names are compared exactly as given."""
    place = graph.place_named(step.place)
    if place is None:
        raise LookupError(f"no place named {step.place!r}")
    if step.token is None:
        if not isinstance(place, SubnetPlaceNode):
            raise LookupError(f"place {step.place!r} is not a subnet place")
        return place.graph
    if isinstance(place, SubnetPlaceNode):
        raise LookupError(f"place {step.place!r} is a subnet place, whose step names the place alone")
    if not place.nested:
        raise LookupError(f"place {step.place!r} holds no nets")
    TokenIds.resync(graph)
    ids = place.token_ids
    token_id = _token_id(step.token)
    if token_id is None or token_id not in ids:
        raise LookupError(f"no token {step.token!r} in {step.place!r}")
    nets = NestedNets.of(place.tokens[ids.index(token_id)])
    if step.net not in nets:
        raise LookupError(f"token {step.token!r} in {step.place!r} holds no net named {step.net!r}")
    return nets[step.net]


def _find(snapshot: _Snapshot, token_id: int) -> Optional[tuple[str, Any]]:
    """The live token with this id in a snapshot's own places; a port's tokens
    carry their subnet's ids and are found through the subnet."""
    for key, view in snapshot.places.items():
        if isinstance(key, Port):
            continue
        for index, candidate in enumerate(view.ids):
            if candidate == token_id and index < len(view.tokens):
                return view.name, view.tokens[index]
    return None


def _where(key: PlaceKey) -> dict:
    """The fields that name a place in an object (§7.1, §12.7): ``place``, and
    ``port`` beside it for a port of one of the net's subnet places."""
    place, port = Places.split(key)
    return {"place": place} if port is None else {"place": place, "port": port}


def _changed_by(event) -> tuple[PlaceKey, ...]:
    """The places a state notification made of ``event`` changes, keyed as the
    event's net names them: a ``Fired``, told as ``distributed``, the places it
    produced into (its ``launched`` told what it took); a ``Failed`` the places it
    put back, when it restored; an ``Added``, ``Removed`` or ``Mutated`` its place."""
    if isinstance(event, core_events.Fired):
        return tuple(place for place, _token_id in event.produced)
    if isinstance(event, core_events.Failed):
        return tuple(place for place, _token_id in event.consumed) if event.restored else ()
    if isinstance(event, (core_events.Added, core_events.Removed, core_events.Mutated)):
        return (event.place,)
    return ()


def _subnets_reached(net: _Net, transition: str) -> list[str]:
    """The subnet places one of whose ports ``transition`` of ``net`` has an edge
    on, of any kind, in the order its edges name them."""
    places: list[str] = []
    graph = net.graph
    for edge in (*graph.argument_edges, *graph.return_edges, *graph.snapshot_edges, *graph.mutate_edges):
        key = edge.place_node_name
        if edge.transition_node_name == transition and isinstance(key, Port) and key.place not in places:
            places.append(key.place)
    return places


def _lifted(net: _Net, handover: _Handover) -> Optional[_Handover]:
    """A subnet's handover as the net around it names the same tokens: what it
    takes from and hands over in the subnet's port places, each by its pair;
    ``None`` when it takes and hands over nothing there."""
    place = net.subnet_place

    def at_port(key: PlaceKey) -> bool:
        node = net.place_nodes.get(key)
        return isinstance(key, str) and node is not None and node.port

    lifted = _Handover(
        consumed=[(Port(place, key), token_id) for key, token_id in handover.consumed if at_port(key)],
        taken={Port(place, key): count for key, count in handover.taken.items() if at_port(key) and count},
        mutated={Port(place, key): indices for key, indices in handover.mutated.items() if at_port(key) and indices},
        keyed_taken={Port(place, key): indices for key, indices in handover.keyed_taken.items() if at_port(key)},
    )
    if not (lifted.consumed or lifted.taken or lifted.mutated or lifted.keyed_taken):
        return None
    return lifted


def _events_in(events: list, net: _Net):
    """The events of ``net`` among a drained batch: the outer net's own records,
    or the records wrapped for the inner net."""
    if net.is_outer:
        return [e for e in events if not isinstance(e, core_events.Nested)]
    return [e.event for e in events if isinstance(e, core_events.Nested) and e.nested_id == net.nested_id]


def _nets_within(token: Any) -> set[int]:
    """The ids of every net inside ``token``, at any depth."""
    ids: set[int] = set()
    try:
        nets = NestedNets.of(token)
    except TypeError:
        return ids
    for inner in nets.values():
        ids |= _nets_of(inner)
    return ids


def _nets_of(graph: ExecutableGraph) -> set[int]:
    """The id of ``graph`` and of every net inside it, at any depth: the nets of
    its tokens and its subnets."""
    ids = {graph.net_id}
    for place in graph.places:
        if isinstance(place, SubnetPlaceNode):
            ids |= _nets_of(place.graph)
        elif place.nested:
            for held in place.tokens:
                ids |= _nets_within(held)
    return ids


def _repr(token: Any) -> str:
    try:
        return repr(token)
    except Exception as error:
        return f"<{type(token).__name__}: repr raised {type(error).__name__}>"


def _pairs(pairs) -> list[list[Any]]:
    """``(place, id)`` pairs as the wire names them by position, a port by
    ``{"place", "port"}`` (§7.1, §8, §12.7)."""
    return [[Places.ref(place), str(token_id)] for place, token_id in pairs]


def _iso(moment: datetime) -> str:
    """ISO 8601 in UTC with milliseconds and ``Z``; a naive time is taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() * 1000.0)


def _token_id(text: str) -> Optional[int]:
    """A protocol id as the graph's integer, or ``None`` if it is not one (only
    the canonical decimal spelling names a token)."""
    if not text.isdecimal() or not text.isascii() or str(int(text)) != text:
        return None
    return int(text)


def _string(params: dict, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, f"needs a string {key!r}")
    return value


def _optional_string(params: dict, key: str) -> Optional[str]:
    """A string field a request may leave out (``None`` then); anything else is
    ``invalid_params``."""
    if key not in params:
        return None
    return _string(params, key)


def _object(params: dict, key: str) -> dict:
    value = params.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProtocolError(ErrorCode.INVALID_PARAMS, f"{key!r} must be an object")
    return value


def _count(params: dict, key: str) -> Optional[int]:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(ErrorCode.INVALID_PARAMS, f"{key!r} must be a non-negative integer")
    return value


def _page_limit(params: dict, key: str) -> int:
    value = _count(params, key)
    return DEFAULT_TOKEN_LIMIT if value is None else max(1, min(value, MAX_TOKEN_LIMIT))
