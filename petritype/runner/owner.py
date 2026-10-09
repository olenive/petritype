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

A firing is handed to the caller, not run here. ``handle`` for ``step`` or
``start`` (and ``completed`` or ``tick`` while running) may return an
``Outgoing.launch``: a ``Launch`` whose coroutine the caller awaits (as a task,
so it can keep handling requests meanwhile) and whose ``Landing`` it passes back
through ``completed``. One firing is airborne at a time. While it is, the reads
(``get_state``, ``get_place_tokens``, ``get_token``, ``get_log``), ``stop``,
``describe`` and ``shutdown`` are answered at once and every other request waits,
in arrival order, for the landing; a second ``step`` therefore waits for the
first.

Nested nets (PROTOCOL.md §12)
-----------------------------
Since 2026-10-09 the owner serves a tree of nets: the outer net the host loaded
and, inside the tokens of its ``nested`` places, inner nets at any depth, each
addressed by a ``path`` or a ``nested_id`` and each with its own
``marking_version``, ``fireable`` set, topology and log view. One ``_Net``
record per net, built when the net is first addressed or fired, holds what the
protocol keeps about it; the outer one is ``_Loaded.outer`` and every method
below takes the net it acts on. A firing may be in any net: the driver picks the
outer net's choice first and, when it has none, descends in the order the
library's own driver uses (place, token, net order; a net's own inner nets
before the next token). An inner net's notifications go out wrapped in
``nested`` with the inner version inside and the outer version, unmoved, on the
wrapper; the engine delivers inner events to the outer log already wrapped as
``Nested`` records with the path as it stood.

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
from petritype.core.executable_graph_components import (
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    MapPlaceNames,
    MapTransitionNames,
    NestedNets,
    TokenIds,
    TransitionFailedError,
    default_transition_selector,
)
from petritype.runner import protocol as wire
from petritype.runner.protocol import ErrorCode, Lines, Malformed, ProtocolError, Request, Requests
from petritype.runner.registry import NetEntries, NetEntry, Registry
from petritype.runner.translate import JSON_SCALARS, Serializable, StrictJson, Tokens, Topologies, TypeNames
from petritype.runtime import Disable, Enable, Extend, RunContext, Runner, SetNetParam, SetParam


# How many entries ``get_log`` keeps (PROTOCOL.md §8).
LOG_CAPACITY = 1000
# ``get_state``'s and ``get_place_tokens``'s default page, and the largest.
DEFAULT_TOKEN_LIMIT = 200
MAX_TOKEN_LIMIT = 1000

# Answered at once while a body is airborne; every other request waits for the
# landing. ``shutdown`` abandons the body, ``describe`` reads nothing of the net.
ANSWERED_WHILE_AIRBORNE = frozenset({
    "get_state", "get_place_tokens", "get_token", "get_log", "stop", "shutdown", "describe",
})
READS = frozenset({"get_state", "get_place_tokens", "get_token", "get_log"})

METHODS = frozenset({
    "describe", "load", "get_state", "get_place_tokens", "get_token", "step", "start", "stop",
    "reset", "inject", "enable", "disable", "set_param", "get_log", "shutdown",
})

# Methods that act on the outer net alone (§12.3); a ``path`` or ``nested_id`` on them is refused.
OUTER_ONLY = frozenset({"enable", "disable", "set_param", "start", "stop", "reset", "step"})

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
    places render each token's ``nets`` under ``prefix``, the wire path of the net
    the place belongs to."""

    name: str
    type_name: str
    ids: list[int]
    tokens: list[Any]
    eager: dict[int, wire.Token] = field(default_factory=dict)
    nested: bool = False
    prefix: str = ""

    @property
    def count(self) -> int:
        return len(self.tokens)

    def entry(self, index: int) -> wire.Token:
        cached = self.eager.get(index)
        if cached is not None:
            return cached
        token_id, token = self.ids[index], self.tokens[index]
        nets = Tokens.nets(token, self.prefix, self.name, token_id) if self.nested else None
        return Tokens.entry(token_id, token, self.type_name, nets=nets)

    def entries(self, offset: int, limit: int) -> list[wire.Token]:
        end = min(offset + limit, len(self.ids), len(self.tokens))
        return [self.entry(index) for index in range(offset, end)]


@dataclass
class _Snapshot:
    marking_version: int
    transitions: dict[str, wire.TransitionState]
    places: dict[str, _PlaceView]
    progress: wire.Progress


@dataclass
class _Net:
    """One net, outer or inner, and what the protocol keeps about it. The outer
    net's ``nested_id`` is ``None``."""

    graph: ExecutableGraph
    ctx: RunContext
    topology: wire.Topology
    type_names: dict[str, str]
    serializable: dict[str, bool]
    nested_places: set[str]
    place_nodes: dict
    incoming: dict
    outgoing: dict
    read: dict
    marking_version: int
    last_fireable: list[str]
    nested_id: Optional[int] = None
    # The last path this inner net was found at, for a line written while its token is airborne.
    last_path: tuple[str, ...] = ()
    last_fired: Optional[str] = None
    last_progress_at: Optional[datetime] = None

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
    consumed: list[tuple[str, int]] = field(default_factory=list)
    # Inner nets, at any depth, inside the tokens an outer firing took or holds: read after the landing.
    held_nets: set[int] = field(default_factory=set)
    # The tokens an outer firing took or holds, as ``(place, id)``, for a path that names one.
    held_tokens: set[tuple[str, int]] = field(default_factory=set)


@dataclass
class _Handover:
    """What stage 1 will do to the marking as it stands: ``consumed``, the
    ``(place, id)`` pairs its argument edges take, in the order taken; ``taken``,
    per place, how many tokens leave from the front; ``mutated``, per place, the
    indices (in the place as it stands) of the tokens handed to the body through
    mutate edges. Snapshot edges hand over deep copies and appear in none."""

    consumed: list[tuple[str, int]]
    taken: dict[str, int]
    mutated: dict[str, set[int]]


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
    def status(self) -> Optional[tuple[str, datetime]]:
        """The transition whose body runs and when it started, for ``heartbeat``;
        ``None`` when no body runs."""
        if self._loaded is None or self._loaded.in_flight is None:
            return None
        return self._loaded.in_flight.name, self._loaded.in_flight.started_at

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
        except Exception as error:
            raise ProtocolError(ErrorCode.LOAD_FAILED, f"{type(error).__name__}: {error}") from error
        # A new generation starts from its built marking: whatever the factory did
        # on the way is part of it, so nothing recorded so far is reported.
        graph.record_events = True
        TokenIds.resync(graph)
        graph.drain_events()
        self._carried_version = outer.marking_version
        self._loaded = _Loaded(entry=entry, params=raw, outer=outer)
        self.generation += 1
        # Recorded silently: ``reloaded`` already means "refetch".
        outer.last_fireable = self._fireable_names(outer)
        out.lines.append(wire.Reloaded(
            generation=self.generation, reason=reason,
            marking_version=outer.marking_version, running=False,
        ).line())

    def _new_net(self, graph: ExecutableGraph, marking_version: int, nested_id: Optional[int] = None) -> _Net:
        """What the protocol keeps about a net, outer or inner. Every synchronous
        body is moved to a thread here, so user code never blocks the loop the
        owner is driven from."""
        topology = Topologies.of(graph)
        for transition in graph.transitions:
            if not inspect.iscoroutinefunction(transition.function):
                transition.execution = "thread"
        return _Net(
            graph=graph,
            ctx=RunContext(graph=graph, inbox=asyncio.Queue(), strict=True),
            topology=topology,
            type_names={place.name: place.type_name for place in topology.places},
            serializable={place.name: place.serializable for place in topology.places},
            nested_places={place.name for place in graph.places if place.nested},
            place_nodes=MapPlaceNames.to_list_place_nodes(graph),
            incoming=MapTransitionNames.to_incoming_edges(graph),
            outgoing=MapTransitionNames.to_outgoing_edges(graph),
            read=MapTransitionNames.to_read_edges(graph),
            marking_version=marking_version,
            last_fireable=[],
            nested_id=nested_id,
        )

    # --- addressing nets (§12.1) ---

    def _net_for(self, graph: ExecutableGraph, path: tuple[str, ...] = ()) -> _Net:
        """The ``_Net`` of a graph found inside the outer net, built on first sight."""
        loaded = self._loaded
        if graph is loaded.outer.graph:
            return loaded.outer
        net = loaded.inner.get(graph.net_id)
        if net is None:
            net = self._new_net(graph, 0, nested_id=graph.net_id)
            net.last_fireable = self._fireable_names(net)
            loaded.inner[graph.net_id] = net
        if path:
            net.last_path = path
        return net

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
        return self._net_for(found[1], found[0])

    def _segments(self, path: str) -> tuple[str, ...]:
        """A wire path as the library's segments (without the net's name)."""
        parts = path.split("/")
        name = self._loaded.entry.name
        if parts[0] != name:
            raise ProtocolError(ErrorCode.UNKNOWN_NET, f"path {path!r} does not start with the loaded net {name!r}")
        rest = tuple(parts[1:])
        if len(rest) % 3 != 0:
            raise ProtocolError(
                ErrorCode.UNKNOWN_NET, f"path {path!r}: a path has three segments per level (place, #id, net)",
            )
        return rest

    def _target(self, params: dict) -> _Net:
        """The net a request addresses: the outer net, or the inner net its
        ``path`` or ``nested_id`` names (``unknown_net`` when none is live)."""
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
            return self._net_for(found[1], found[0])
        if has_path:
            path = _string(params, "path")
            segments = self._segments(path)
            if not segments:
                return loaded.outer
            try:
                graph = NestedNets.resolve(loaded.outer.graph, segments)
            except LookupError as error:
                raise ProtocolError(ErrorCode.UNKNOWN_NET, f"path {path!r}: {error}") from error
            return self._net_for(graph, segments)
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
        path = params.get("path")
        if isinstance(path, str):
            parts = path.split("/")
            if len(parts) >= 3 and parts[2].startswith("#") and (token_id := _token_id(parts[2][1:])) is not None:
                if (parts[1], token_id) in flight.held_tokens:
                    return _HELD
        return None

    def _path_of(self, net: _Net) -> str:
        """The wire path of a net as the marking stands (its last known path while
        the token holding it is airborne)."""
        name = self._loaded.entry.name
        if net.is_outer:
            return name
        found = NestedNets.find(self._loaded.outer.graph, net.nested_id)
        if found is not None:
            net.last_path = found[0]
        return "/".join((name, *net.last_path))

    def _wire_path(self, segments: tuple[str, ...]) -> str:
        return "/".join((self._loaded.entry.name, *segments))

    def _net_name(self, net: _Net) -> str:
        """The name a state names: the loaded net's for the outer net, the net's
        name within its token for an inner one."""
        if net.is_outer:
            return self._loaded.entry.name
        return net.last_path[-1] if net.last_path else ""

    def _stamp(self, net: _Net, model: wire.Wire) -> wire.Wire:
        """``path`` and ``nested_id`` on a result about an inner net."""
        if not net.is_outer:
            model.path = self._path_of(net)
            model.nested_id = str(net.nested_id)
        return model

    # --- reads ---

    def _get_state(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        limit = _page_limit(params, "token_limit")
        net = self._target(params)
        snapshot = self._snapshot_of(net) or self._capture(net)
        state = wire.State(
            net=self._net_name(net),
            generation=self.generation if net.is_outer else 1,
            marking_version=snapshot.marking_version,
            running=self._loaded.running,
            topology=net.topology,
            transitions=snapshot.transitions,
            marking=wire.Marking(places={
                name: wire.PlaceMarking(count=view.count, tokens=view.entries(0, limit))
                for name, view in snapshot.places.items()
            }),
            progress=snapshot.progress,
            template=net.graph.template if not net.is_outer else None,
        )
        out.lines.append(Lines.result(request_id, self._stamp(net, state)))

    def _get_place_tokens(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        place = _string(params, "place")
        offset = _count(params, "offset") or 0
        limit = _page_limit(params, "limit")
        net = self._target(params)
        if place not in net.place_nodes:
            raise ProtocolError(ErrorCode.UNKNOWN_PLACE, f"no place named {place!r}")
        snapshot = self._snapshot_of(net)
        if snapshot is not None:
            view, version = snapshot.places[place], snapshot.marking_version
        else:
            view, version = self._view(net, net.place_nodes[place]), net.marking_version
        tokens = view.entries(offset, limit)
        out.lines.append(Lines.result(request_id, self._stamp(net, wire.PlaceTokens(
            place=place, marking_version=version, count=view.count, offset=offset, limit=limit,
            tokens=tokens, has_more=offset + len(tokens) < view.count,
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
        if found is None:
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
        for view in snapshot.places.values():
            for index, candidate in enumerate(view.ids):
                if candidate == token_id:
                    return index in view.eager
        return False

    def _view(self, net: _Net, place) -> _PlaceView:
        return _PlaceView(
            name=place.name,
            type_name=net.type_names[place.name],
            ids=list(place.token_ids),
            tokens=list(place.tokens),
            nested=place.name in net.nested_places,
            prefix=self._path_of(net) if place.name in net.nested_places else "",
        )

    def _capture(
        self, net: _Net, airborne: Optional[FunctionTransitionNode] = None, handover: _Handover | None = None,
        eager: Optional[dict[str, set[int]]] = None,
    ) -> _Snapshot:
        """The marking, transition states and progress of ``net`` as of now. With
        ``airborne``, the transition about to fire in it, and its ``handover``:
        the tokens it takes are left out (and the transition states are those of
        the marking without them), the tokens its mutate edges hand over are
        rendered at once, and it is shown ``in_flight``. ``eager`` names further
        tokens (per place, by index) to render at once: those holding a net an
        airborne body is changing."""
        graph = net.graph
        places = {place.name: self._view(net, place) for place in graph.places}
        for place_name, indices in (eager or {}).items():
            view = places[place_name]
            for index in indices:
                if index < view.count:
                    view.eager[index] = view.entry(index)
        if airborne is None:
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
            transitions = self._states_without(net, handover.taken)
            transitions[airborne.name] = transitions[airborne.name].model_copy(
                update={"in_flight": True, "fireable": False}
            )
        return _Snapshot(
            marking_version=net.marking_version,
            transitions=transitions,
            places=places,
            progress=wire.Progress(
                step_count=graph.step_count,
                last_fired=net.last_fired,
                fired_counts=dict(graph.fired_counts),
                last_progress_at=_iso(net.last_progress_at) if net.last_progress_at else None,
            ),
        )

    def _handed_over(self, net: _Net, transition: FunctionTransitionNode) -> _Handover:
        """What firing ``transition`` now will take and hand over, by stage 1's own
        rules: argument edges in order, each taking the oldest remaining token of
        its place, or every remaining one for a ``list[...]`` argument; then the
        mutate edges, which take nothing and hand over the oldest remaining token
        (or all of them) where they are."""
        consumed: list[tuple[str, int]] = []
        taken: dict[str, int] = {}
        for edge in net.incoming.get(transition.name, ()):
            place = net.place_nodes[edge.place_node_name]
            ids = place.token_ids
            start = taken.get(place.name, 0)
            takes_all = Topologies.takes_all(transition.function, edge.argument, place)
            end = len(ids) if takes_all else min(start + 1, len(ids))
            consumed.extend((place.name, token_id) for token_id in ids[start:end])
            taken[place.name] = max(start, end)
        mutated: dict[str, set[int]] = {}
        for edge in net.graph.mutate_edges:
            if edge.transition_node_name != transition.name:
                continue
            place = net.place_nodes[edge.place_node_name]
            count = len(place.token_ids)
            start = taken.get(place.name, 0)
            takes_all = Topologies.takes_all(transition.function, edge.argument, place)
            end = count if takes_all else min(start + 1, count)
            mutated.setdefault(place.name, set()).update(range(start, end))
        return _Handover(consumed=consumed, taken=taken, mutated=mutated)

    def _held(self, net: _Net, handover: _Handover) -> tuple[set[int], set[tuple[str, int]]]:
        """The inner nets (at any depth) and the tokens an outer firing takes or
        holds through mutate edges: read after the landing."""
        nets: set[int] = set()
        tokens: set[tuple[str, int]] = set()
        for place_name, token_id in handover.consumed:
            tokens.add((place_name, token_id))
        for place_name, indices in handover.mutated.items():
            ids = net.place_nodes[place_name].token_ids
            tokens.update((place_name, ids[index]) for index in indices if index < len(ids))
        for place_name, token_id in tokens:
            if place_name not in net.nested_places:
                continue
            place = net.place_nodes[place_name]
            ids = place.token_ids
            if token_id in ids:
                nets.update(_nets_within(place.tokens[ids.index(token_id)]))
        return nets, tokens

    def _states_without(self, net: _Net, taken: dict[str, int]) -> dict[str, wire.TransitionState]:
        """The transition states of the marking with ``taken`` tokens gone from
        the front of their places: the marking the body runs against. The live
        lists are cut and put back in place (nothing else runs on them meanwhile,
        as the body has not started), so guards and priorities see exactly what
        they will see once stage 1 has run."""
        removed = []
        try:
            for place_name, count in taken.items():
                place = net.place_nodes[place_name]
                tokens, ids = place.tokens[:count], place._token_ids[:count]
                del place.tokens[:count]
                del place._token_ids[:count]
                removed.append((place, tokens, ids))
            return self._transition_states(net)
        finally:
            for place, tokens, ids in removed:
                place.tokens[:0] = tokens
                place._token_ids[:0] = ids

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

    def _select_inner(self, net: _Net) -> Optional[tuple[_Net, FunctionTransitionNode]]:
        """The first inner firing below ``net`` in the library driver's order:
        place, token, net order, a net's own inner nets before the next token."""
        for segments, _token, _name, graph in NestedNets.walk(net.graph, passive=False):
            inner = self._net_for(graph, (*net.last_path, *segments))
            chosen = self._select_in(inner)
            if chosen is not None:
                return inner, chosen
            deeper = self._select_inner(inner)
            if deeper is not None:
                return deeper
        return None

    def _select(self) -> Optional[tuple[_Net, FunctionTransitionNode]]:
        """The next firing anywhere: the outer net's choice, else the first inner
        net's (§12.3)."""
        outer = self._loaded.outer
        chosen = self._select_in(outer)
        if chosen is not None:
            return outer, chosen
        if not NestedNets.any_nested(outer.graph):
            return None
        return self._select_inner(outer)

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
                out: Outgoing) -> None:
        """Write ``launched`` for ``transition`` in ``net`` and hand its firing
        out. The fireable set is compared when the firing lands, not now: the
        launch's own change is transient, and a ``fireable`` between ``launched``
        and ``distributed`` would split the pair a viewer folds into ``fired``."""
        loaded = self._loaded
        # The engine resyncs ids before it takes anything; do it first, so the
        # ids ``launched`` names are the ones it will take.
        TokenIds.resync(net.graph)
        self._flush(now, out)
        handover = self._handed_over(net, transition)
        net.marking_version += 1
        launched = wire.Launched(
            transition=transition.name, consumed=_pairs(handover.consumed),
            marking_version=net.marking_version, running=loaded.running,
        )
        out.lines.append(self._wrap(net, launched.line()))
        snapshots = {net.nested_id: self._capture(net, airborne=transition, handover=handover)}
        held_nets: set[int] = set()
        held_tokens: set[tuple[str, int]] = set()
        if net.is_outer:
            held_nets, held_tokens = self._held(net, handover)
        else:
            # The tokens holding this net, up to the outer net, render it in their labels.
            for ancestor, place_name, index in self._chain(net):
                snapshots[ancestor.nested_id] = self._capture(ancestor, eager={place_name: {index}})
        loaded.in_flight = _InFlight(
            net.nested_id, transition.name, now, step_id, snapshots, handover.consumed, held_nets, held_tokens,
        )
        loaded.idle = False
        out.launch = Launch(transition.name, _fire(net, transition, self.generation))

    def _chain(self, net: _Net) -> list[tuple[_Net, str, int]]:
        """From the outer net down to the parent of ``net``: each ancestor with
        the place and index of the token that holds the next level."""
        loaded = self._loaded
        found = NestedNets.find(loaded.outer.graph, net.nested_id)
        segments = found[0] if found is not None else net.last_path
        chain = []
        graph = loaded.outer.graph
        for level in range(0, len(segments), 3):
            place_name, token_segment, name = segments[level:level + 3]
            ancestor = self._net_for(graph, segments[:level])
            place = ancestor.place_nodes.get(place_name)
            if place is None or not token_segment.startswith("#"):
                break
            token_id = _token_id(token_segment[1:])
            ids = place.token_ids
            if token_id not in ids:
                break
            index = ids.index(token_id)
            chain.append((ancestor, place_name, index))
            graph = NestedNets.of(place.tokens[index]).get(name)
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
        fired, failed, path = self._firing_of(events, flight)
        if fired is not None or failed is not None:
            announced, landed = _pairs(flight.consumed), _pairs((fired or failed).consumed)
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
                path=self._wire_path(path) if not net.is_outer else None,
                nested_id=str(net.nested_id) if not net.is_outer else None,
            ), chain))
        if fired is not None:
            net.last_fired = flight.name
            net.last_progress_at = now
            if not net.is_outer:
                # The outer net counts an inner firing under its path, as the library's driver does.
                outer = loaded.outer
                key = "/".join((*path, flight.name))
                outer.graph.step_count += 1
                outer.graph.fired_counts[key] = outer.graph.fired_counts.get(key, 0) + 1
                outer.last_fired = key
                outer.last_progress_at = now

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
            path=self._wire_path(path) if landed and not net.is_outer else None,
            nested_id=str(net.nested_id) if landed and not net.is_outer else None,
            marking_version=self.marking_version,
            running=loaded.running,
        )))

    def _firing_of(self, events: list, flight: _InFlight):
        """The ``Fired`` or ``Failed`` of this flight among the events just
        drained, and, for an inner firing, the path it was recorded under."""
        fired = failed = None
        path: tuple[str, ...] = ()
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
                path = event.path
        if flight.net_id is not None and not path:
            net = self._loaded.inner.get(flight.net_id)
            path = net.last_path if net is not None else ()
        return fired, failed, path

    def _id_chain(self, segments: tuple[str, ...]) -> tuple[int, ...]:
        """The nested ids down a path, outermost first, for the log's filter."""
        chain = []
        graph = self._loaded.outer.graph
        for level in range(0, len(segments), 3):
            try:
                graph = NestedNets.resolve(graph, segments[level:level + 3])
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
        self._emit(net, self._notification(net, core_events.Failed(
            transition=flight.name, consumed=tuple(flight.consumed), restored=restored, error=error,
        )), out)
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
        place = _string(params, "place")
        if "token" not in params:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, 'inject needs a "token"')
        net = self._target(params)
        node = net.place_nodes.get(place)
        if node is None:
            raise ProtocolError(ErrorCode.UNKNOWN_PLACE, f"no place named {place!r}")
        if not net.serializable[place]:
            raise ProtocolError(
                ErrorCode.NOT_SERIALIZABLE, f"place {place!r} ({net.type_names[place]}) has no JSON form",
            )
        try:
            token = Serializable.decode(node.type, params["token"])
        except ValueError as error:
            raise ProtocolError(ErrorCode.INVALID_TOKEN, f"not a {net.type_names[place]}: {error}") from error
        try:
            self._apply(net, Extend(place=place, tokens=[token]))
        except ValueError as error:  # the strict context's own type check
            raise ProtocolError(ErrorCode.INVALID_TOKEN, str(error)) from error
        events = self._flush(now, out)
        added = next((
            e for e in _events_in(events, net) if isinstance(e, core_events.Added) and e.place == place
        ), None)
        if added is None:
            raise ProtocolError(ErrorCode.INTERNAL, "the token was added without an event")
        out.lines.append(Lines.result(request_id, {
            "status": "injected", "id": str(added.token_id), "marking_version": net.marking_version,
        }))

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
        ``SetNetParam``, which must be declared and keeps its JSON type."""
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
        """A net parameter keeps the JSON type of its current value: a scalar is
        validated strictly against that type; anything else is taken as given."""
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
        anything happened and the set changed. Returns the events."""
        loaded = self._loaded
        outer = loaded.outer
        events = outer.graph.drain_events()
        touched: list[_Net] = []
        for event in events:
            if isinstance(event, core_events.Nested):
                inner = self._net_by_id(event.nested_id)
                notification = self._notification(inner, event.event)
                if inner is not None:
                    if inner.last_path != event.path:
                        inner.last_path = event.path
                    if notification.moves_version:
                        inner.marking_version += 1
                    notification.marking_version = inner.marking_version
                    if inner not in touched:
                        touched.append(inner)
                out.lines.append(wire.NestedEvent(
                    path=self._wire_path(event.path), nested_id=str(event.nested_id), event=notification.line(),
                    marking_version=outer.marking_version, running=loaded.running,
                ).line())
            else:
                notification = self._notification(outer, event)
                if notification.moves_version:
                    outer.marking_version += 1
                    notification.marking_version = outer.marking_version
                out.lines.append(notification.line())
        if events:
            self._report_fireable(outer, out)
            for inner in touched:
                self._report_fireable(inner, out)
        return events

    def _report_fireable(self, net: _Net, out: Outgoing) -> None:
        fireable = self._fireable_names(net)
        if fireable != net.last_fireable:
            net.last_fireable = fireable
            self._emit(net, wire.Fireable(
                transitions=fireable, marking_version=net.marking_version, running=self._loaded.running,
            ), out)

    def _emit(self, net: _Net, notification: wire.Notification, out: Outgoing) -> None:
        """Write a notification of ``net``, raising its version if the
        notification moves one, wrapped in ``nested`` for an inner net."""
        if notification.moves_version:
            net.marking_version += 1
            notification.marking_version = net.marking_version
        out.lines.append(self._wrap(net, notification.line()))

    def _wrap(self, net: _Net, line: dict) -> dict:
        """An inner net's line inside a ``nested`` wrapper; an outer net's as is."""
        if net.is_outer:
            return line
        loaded = self._loaded
        return wire.NestedEvent(
            path=self._path_of(net), nested_id=str(net.nested_id), event=line,
            marking_version=loaded.outer.marking_version, running=loaded.running,
        ).line()

    def _notification(self, net: Optional[_Net], event) -> wire.Notification:
        """One event as its notification, stamped with the net's current version
        (the caller raises it for a state notification). ``net`` is ``None`` for
        an inner net that no longer exists, whose tokens cannot be rendered."""
        version = net.marking_version if net is not None else 0
        stamp = {"marking_version": version, "running": self._loaded.running}
        if isinstance(event, core_events.Fired):
            # The second piece: ``launched`` already named what it consumed.
            return wire.Distributed(
                transition=event.transition,
                produced=[self._produced(net, place, token_id) for place, token_id in event.produced],
                duration_ms=event.duration_ms,
                **stamp,
            )
        if isinstance(event, core_events.Failed):
            return wire.Failed(
                transition=event.transition, error=event.error, restored=event.restored,
                consumed=_pairs(event.consumed), **stamp,
            )
        if isinstance(event, core_events.Added):
            token = self._live(net, event.place, event.token_id)
            entry = Tokens.entry(event.token_id, token, "") if token is not _MISSING else None
            return wire.Added(
                place=event.place, id=str(event.token_id),
                label=entry.label if entry else "", color=entry.color if entry else None, **stamp,
            )
        if isinstance(event, core_events.Removed):
            return wire.Removed(place=event.place, id=str(event.token_id), **stamp)
        if isinstance(event, core_events.Mutated):
            token = self._live(net, event.place, event.token_id)
            label = Tokens.entry(event.token_id, token, "").label if token is not _MISSING else None
            return wire.Mutated(place=event.place, id=str(event.token_id), label=label, **stamp)
        if isinstance(event, core_events.Toggled):
            return wire.Toggled(transition=event.transition, enabled=event.enabled, **stamp)
        if isinstance(event, core_events.ParamChanged):
            return wire.ParamChanged(param=event.param, transition=event.transition, **stamp)
        # Resynced, or an event this runner does not know: the marking moved in a
        # way it cannot describe.
        generation = self.generation if net is None or net.is_outer else 1
        return wire.Reloaded(generation=generation, reason="resync", **stamp)

    def _produced(self, net: Optional[_Net], place: str, token_id: int) -> list[str]:
        token = self._live(net, place, token_id)
        if token is _MISSING:
            return [place, str(token_id), ""]
        return Tokens.produced(place, token_id, token)

    def _live(self, net: Optional[_Net], place: str, token_id: int) -> Any:
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


def _find(snapshot: _Snapshot, token_id: int) -> Optional[tuple[str, Any]]:
    for view in snapshot.places.values():
        for index, candidate in enumerate(view.ids):
            if candidate == token_id and index < len(view.tokens):
                return view.name, view.tokens[index]
    return None


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
        ids.add(inner.net_id)
        for place in inner.places:
            if place.nested:
                for held in place.tokens:
                    ids |= _nets_within(held)
    return ids


def _repr(token: Any) -> str:
    try:
        return repr(token)
    except Exception as error:
        return f"<{type(token).__name__}: repr raised {type(error).__name__}>"


def _pairs(pairs) -> list[list[str]]:
    return [[place, str(token_id)] for place, token_id in pairs]


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
