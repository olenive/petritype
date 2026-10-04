"""The runner's state machine: one net, the protocol's bookkeeping, no I/O.

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

Reads while a body runs
-----------------------
The live graph is **not** safe to read while a body runs, for the reason the
server gave: synchronous bodies run on a thread (the owner marks every sync
transition ``execution="thread"`` at load, as the server's engine did, so user
code never blocks the event loop), and a thread is not bound by the event
loop's await points. A body handed a token through a mutate edge, or a whole
place's live list through a ``list[...]`` mutate edge, can change it while a
read is iterating it. The marking mid-firing is also inconsistent by definition
(inputs taken, outputs not yet placed).

So every launch writes ``launched`` and then captures a ``_Snapshot`` of the
marking as of that ``launched``'s ``marking_version``: per place, a shallow copy
of the token and id lists (pointer copies, cheap) without the tokens the firing
takes (predicted from the edges exactly as stage 1 will take them, and the same
ones ``launched`` names), with the labels of the tokens it hands to its body
through mutate edges rendered at once, before the body can touch them; the
transition states as of that marking, with the airborne one ``in_flight``; and
the progress. While the body runs, ``get_state`` and ``get_place_tokens`` are
served from that snapshot: counts are the true counts at that version, any page
can be served (labels of untouched tokens are rendered on demand from objects no
one else is changing), and the response says which version it reflects. A
taken token is in no place, so ``get_token`` for it answers ``unknown_token``,
as the Rust runner does. A ``get_token`` for a token handed to the body through
a mutate edge is the one read that waits for the landing: its full value is
exactly what the body may be changing.

The prediction is checked on landing against the engine's own ``Fired`` or
``Failed``; a difference would be a bug, and is reported on stderr (the one
place the owner writes anything itself).
This differs from the server's approach (a capped snapshot refreshed after
every mutation, and a complete one kept while wanted), which paid for labels on
every change whether or not a read came; here the cost is paid per firing and
only for the tokens at risk.
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
    TokenIds,
    TransitionFailedError,
    default_transition_selector,
)
from petritype.runner import protocol as wire
from petritype.runner.protocol import ErrorCode, Lines, Malformed, ProtocolError, Request, Requests
from petritype.runner.registry import NetEntries, NetEntry, Registry
from petritype.runner.translate import Serializable, StrictJson, Tokens, Topologies, TypeNames
from petritype.runtime import Disable, Enable, Extend, RunContext, Runner, SetParam


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

METHODS = frozenset({
    "describe", "load", "get_state", "get_place_tokens", "get_token", "step", "start", "stop",
    "reset", "inject", "enable", "disable", "set_param", "get_log", "shutdown",
})

CONTINUOUS_MODE = "24/7"


# --- what goes out -------------------------------------------------------------------------


@dataclass(frozen=True)
class Landing:
    """What a ``Launch`` coroutine resolves to: the transition, the generation it
    was launched in, and the exception it ended with, if any. Whether the firing
    fired or failed is read off the graph's events by ``Owner.completed``."""

    transition: str
    generation: int
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
    (the tokens handed to an airborne body), ``at_risk`` their indices."""

    name: str
    type_name: str
    ids: list[int]
    tokens: list[Any]
    eager: dict[int, wire.Token] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.tokens)

    def entry(self, index: int) -> wire.Token:
        cached = self.eager.get(index)
        if cached is not None:
            return cached
        return Tokens.entry(self.ids[index], self.tokens[index], self.type_name)

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
class _InFlight:
    name: str
    started_at: datetime
    # The ``step`` request waiting for this firing, if a step launched it.
    step_id: Optional[str]
    snapshot: _Snapshot
    # What ``launched`` said the firing takes, in the order taken.
    consumed: list[tuple[str, int]] = field(default_factory=list)


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
class _Loaded:
    """One generation of one net and what the protocol keeps about it."""

    entry: NetEntry
    params: dict
    graph: ExecutableGraph
    ctx: RunContext
    topology: wire.Topology
    type_names: dict[str, str]
    serializable: dict[str, bool]
    place_nodes: dict
    incoming: dict
    outgoing: dict
    read: dict
    last_fireable: list[str]
    running: bool = False
    in_flight: Optional[_InFlight] = None
    deferred: deque = field(default_factory=deque)
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_CAPACITY))
    log_seq: int = 0
    last_fired: Optional[str] = None
    last_progress_at: Optional[datetime] = None
    idle: bool = False


# --- the owner -----------------------------------------------------------------------------


class Owner:
    """The runner's state machine (see the module docstring). Feed it requests
    with ``handle``, landings with ``completed`` and clock ticks with ``tick``;
    write the ``lines`` each returns, run its ``launch``, stop on ``exit``."""

    def __init__(self, registry: Registry):
        self.registry = registry
        # Loads and resets so far: 0 until the first load.
        self.generation = 0
        # Never decreases, and continues across load and reset (PROTOCOL.md §4).
        self._marking_version = 0
        self._loaded: Optional[_Loaded] = None

    # --- read-only views for the io layer ---

    @property
    def marking_version(self) -> int:
        return self._marking_version

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
        if request.method == "get_token":
            return not self._token_at_risk(request.params)
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
            marking_version=self._marking_version, running=self.running,
        ).line())
        self._build(entry, raw, kwargs, "load", out)
        out.lines.append(Lines.result(request_id, {
            "status": "loaded", "generation": self.generation, "marking_version": self._marking_version,
        }))

    def _reset(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        loaded = self._loaded
        kwargs = NetEntries.bind(loaded.entry, loaded.params)
        self._build(loaded.entry, loaded.params, kwargs, "reset", out)
        out.lines.append(Lines.result(request_id, {
            "status": "reset", "generation": self.generation, "marking_version": self._marking_version,
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
            topology = Topologies.of(graph)
        except Exception as error:
            raise ProtocolError(ErrorCode.LOAD_FAILED, f"{type(error).__name__}: {error}") from error
        # A new generation starts from its built marking: whatever the factory did
        # on the way is part of it, so nothing recorded so far is reported.
        graph.record_events = True
        TokenIds.resync(graph)
        graph.drain_events()
        # User code must never block the loop the owner is driven from.
        for transition in graph.transitions:
            if not inspect.iscoroutinefunction(transition.function):
                transition.execution = "thread"
        loaded = _Loaded(
            entry=entry,
            params=raw,
            graph=graph,
            ctx=RunContext(graph=graph, inbox=asyncio.Queue(), strict=True),
            topology=topology,
            type_names={place.name: place.type_name for place in topology.places},
            serializable={place.name: place.serializable for place in topology.places},
            place_nodes=MapPlaceNames.to_list_place_nodes(graph),
            incoming=MapTransitionNames.to_incoming_edges(graph),
            outgoing=MapTransitionNames.to_outgoing_edges(graph),
            read=MapTransitionNames.to_read_edges(graph),
            last_fireable=[],
        )
        self._loaded = loaded
        self.generation += 1
        # Recorded silently: ``reloaded`` already means "refetch".
        loaded.last_fireable = self._fireable_names()
        out.lines.append(wire.Reloaded(
            generation=self.generation, reason=reason,
            marking_version=self._marking_version, running=False,
        ).line())

    # --- reads ---

    def _get_state(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        limit = _page_limit(params, "token_limit")
        loaded = self._loaded
        snapshot = loaded.in_flight.snapshot if loaded.in_flight is not None else self._capture()
        state = wire.State(
            net=loaded.entry.name,
            generation=self.generation,
            marking_version=snapshot.marking_version,
            running=loaded.running,
            topology=loaded.topology,
            transitions=snapshot.transitions,
            marking=wire.Marking(places={
                name: wire.PlaceMarking(count=view.count, tokens=view.entries(0, limit))
                for name, view in snapshot.places.items()
            }),
            progress=snapshot.progress,
        )
        out.lines.append(Lines.result(request_id, state))

    def _get_place_tokens(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        place = _string(params, "place")
        offset = _count(params, "offset") or 0
        limit = _page_limit(params, "limit")
        loaded = self._loaded
        if place not in loaded.place_nodes:
            raise ProtocolError(ErrorCode.UNKNOWN_PLACE, f"no place named {place!r}")
        if loaded.in_flight is not None:
            snapshot = loaded.in_flight.snapshot
            view, version = snapshot.places[place], snapshot.marking_version
        else:
            view, version = self._view(loaded.place_nodes[place]), self._marking_version
        tokens = view.entries(offset, limit)
        out.lines.append(Lines.result(request_id, wire.PlaceTokens(
            place=place, marking_version=version, count=view.count, offset=offset, limit=limit,
            tokens=tokens, has_more=offset + len(tokens) < view.count,
        )))

    def _get_token(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        wanted = _string(params, "id")
        token_id = _token_id(wanted)
        loaded = self._loaded
        found = None
        if token_id is not None:
            if loaded.in_flight is not None:
                found = _find(loaded.in_flight.snapshot, token_id)
            else:
                found = TokenIds.locate(loaded.graph, token_id)
                self._flush(now, out)  # locate resyncs: report any repair first
        if found is None:
            raise ProtocolError(ErrorCode.UNKNOWN_TOKEN, f"no live token {wanted!r}")
        place, token = found
        common = {"id": wanted, "place": place, "type_name": type(token).__name__}
        value = None
        if loaded.serializable[place]:
            try:
                value = wire.TokenValue(kind="json", value=Tokens.json_value(token), **common)
            except Exception:
                value = None  # a token of a serializable type that will not dump: show its text
        if value is None:
            value = wire.TokenValue(kind="text", text=_repr(token), **common)
        out.lines.append(Lines.result(request_id, value))

    def _get_log(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        after = _count(params, "after") or 0
        limit = _count(params, "limit")
        limit = LOG_CAPACITY if limit is None else max(1, min(limit, LOG_CAPACITY))
        entries = [entry for entry in self._loaded.log if entry.seq > after][:limit]
        out.lines.append(Lines.result(request_id, {"entries": [entry.wire() for entry in entries]}))

    def _token_at_risk(self, params: Any) -> bool:
        """Whether a ``get_token`` names a token the airborne body was handed."""
        if not isinstance(params, dict) or not isinstance(params.get("id"), str):
            return False
        token_id = _token_id(params["id"])
        if token_id is None:
            return False
        for view in self._loaded.in_flight.snapshot.places.values():
            for index, candidate in enumerate(view.ids):
                if candidate == token_id:
                    return index in view.eager
        return False

    def _view(self, place) -> _PlaceView:
        return _PlaceView(
            name=place.name,
            type_name=self._loaded.type_names[place.name],
            ids=list(place.token_ids),
            tokens=list(place.tokens),
        )

    def _capture(
        self, airborne: Optional[FunctionTransitionNode] = None, handover: _Handover | None = None,
    ) -> _Snapshot:
        """The marking, transition states and progress as of now. With
        ``airborne``, the transition about to fire, and its ``handover``: the
        tokens it takes are left out (and the transition states are those of the
        marking without them), the tokens its mutate edges hand over are rendered
        at once, and it is shown ``in_flight``."""
        loaded = self._loaded
        graph = loaded.graph
        places = {place.name: self._view(place) for place in graph.places}
        if airborne is None:
            transitions = self._transition_states()
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
            transitions = self._states_without(handover.taken)
            transitions[airborne.name] = transitions[airborne.name].model_copy(
                update={"in_flight": True, "fireable": False}
            )
        return _Snapshot(
            marking_version=self._marking_version,
            transitions=transitions,
            places=places,
            progress=wire.Progress(
                step_count=graph.step_count,
                last_fired=loaded.last_fired,
                fired_counts=dict(graph.fired_counts),
                last_progress_at=_iso(loaded.last_progress_at) if loaded.last_progress_at else None,
            ),
        )

    def _handed_over(self, transition: FunctionTransitionNode) -> _Handover:
        """What firing ``transition`` now will take and hand over, by stage 1's own
        rules: argument edges in order, each taking the oldest remaining token of
        its place, or every remaining one for a ``list[...]`` argument; then the
        mutate edges, which take nothing and hand over the oldest remaining token
        (or all of them) where they are."""
        loaded = self._loaded
        consumed: list[tuple[str, int]] = []
        taken: dict[str, int] = {}
        for edge in loaded.incoming.get(transition.name, ()):
            place = loaded.place_nodes[edge.place_node_name]
            ids = place.token_ids
            start = taken.get(place.name, 0)
            takes_all = Topologies.takes_all(transition.function, edge.argument, place)
            end = len(ids) if takes_all else min(start + 1, len(ids))
            consumed.extend((place.name, token_id) for token_id in ids[start:end])
            taken[place.name] = max(start, end)
        mutated: dict[str, set[int]] = {}
        for edge in loaded.graph.mutate_edges:
            if edge.transition_node_name != transition.name:
                continue
            place = loaded.place_nodes[edge.place_node_name]
            count = len(place.token_ids)
            start = taken.get(place.name, 0)
            takes_all = Topologies.takes_all(transition.function, edge.argument, place)
            end = count if takes_all else min(start + 1, count)
            mutated.setdefault(place.name, set()).update(range(start, end))
        return _Handover(consumed=consumed, taken=taken, mutated=mutated)

    def _states_without(self, taken: dict[str, int]) -> dict[str, wire.TransitionState]:
        """The transition states of the marking with ``taken`` tokens gone from
        the front of their places: the marking the body runs against. The live
        lists are cut and put back in place (nothing else runs on them meanwhile,
        as the body has not started), so guards and priorities see exactly what
        they will see once stage 1 has run."""
        place_nodes = self._loaded.place_nodes
        removed = []
        try:
            for place_name, count in taken.items():
                place = place_nodes[place_name]
                tokens, ids = place.tokens[:count], place._token_ids[:count]
                del place.tokens[:count]
                del place._token_ids[:count]
                removed.append((place, tokens, ids))
            return self._transition_states()
        finally:
            for place, tokens, ids in removed:
                place.tokens[:0] = tokens
                place._token_ids[:0] = ids

    # --- transition states and selection ---

    def _is_fireable(self, transition: FunctionTransitionNode) -> bool:
        loaded = self._loaded
        if transition.name in loaded.ctx.disabled or transition.name in loaded.graph.in_flight:
            return False
        try:
            return ExecutableGraphCheck.transition_is_fireable(
                transition=transition,
                executable_graph=loaded.graph,
                transition_names_to_incoming_edges=loaded.incoming,
                place_names_to_nodes=loaded.place_nodes,
                transition_names_to_read_edges=loaded.read,
            )
        except Exception:
            return False  # a guard that raises cannot let its transition fire

    def _fireable_names(self) -> list[str]:
        return [t.name for t in self._loaded.graph.transitions if self._is_fireable(t)]

    def _transition_states(self) -> dict[str, wire.TransitionState]:
        loaded = self._loaded
        graph = loaded.graph
        states = {}
        for transition in graph.transitions:
            priority = None
            try:
                priority = float(transition.priority(graph)) if transition.priority is not None else 0.0
            except Exception:
                priority = None
            states[transition.name] = wire.TransitionState(
                enabled=transition.name not in loaded.ctx.disabled,
                fireable=self._is_fireable(transition),
                in_flight=transition.name in graph.in_flight,
                priority=priority,
            )
        return states

    def _select(self) -> Optional[FunctionTransitionNode]:
        """The transition the net's own selector would fire now, among the
        fireable and enabled ones; ``None`` if there is none. A selector or
        priority that raises propagates."""
        graph = self._loaded.graph
        fireable = [t for t in graph.transitions if self._is_fireable(t)]
        if not fireable:
            return None
        return (graph.transition_selector or default_transition_selector)(graph, fireable)

    # --- the firing cycle ---

    def _step(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        try:
            transition = self._select()
        except Exception as error:
            raise ProtocolError(
                ErrorCode.INTERNAL, f"selecting a transition raised {type(error).__name__}: {error}"
            ) from error
        if transition is None:
            out.lines.append(Lines.result(request_id, wire.StepResult(
                fired=False, marking_version=self._marking_version, running=self._loaded.running,
            )))
            return
        self._launch(transition, request_id, now, out)

    def _start(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        self._loaded.running = True
        self._loaded.idle = False
        out.lines.append(Lines.result(request_id, {
            "status": "started", "marking_version": self._marking_version, "running": True,
        }))

    def _stop(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        self._loaded.running = False
        out.lines.append(Lines.result(request_id, {
            "status": "stopped", "marking_version": self._marking_version, "running": False,
        }))

    def _launch(self, transition: FunctionTransitionNode, step_id: Optional[str], now: datetime,
                out: Outgoing) -> None:
        """Write ``launched`` for ``transition`` and hand its firing out. The
        fireable set is compared when the firing lands, not now: the launch's own
        change is transient, and a ``fireable`` between ``launched`` and
        ``distributed`` would split the pair a viewer folds into ``fired``."""
        loaded = self._loaded
        # The engine resyncs ids before it takes anything; do it first, so the
        # ids ``launched`` names are the ones it will take.
        TokenIds.resync(loaded.graph)
        self._flush(now, out)
        handover = self._handed_over(transition)
        self._marking_version += 1
        out.lines.append(wire.Launched(
            transition=transition.name, consumed=_pairs(handover.consumed),
            marking_version=self._marking_version, running=loaded.running,
        ).line())
        snapshot = self._capture(airborne=transition, handover=handover)
        loaded.in_flight = _InFlight(transition.name, now, step_id, snapshot, handover.consumed)
        loaded.idle = False
        out.launch = Launch(transition.name, _fire(loaded, transition, self.generation))

    def _land(self, landing: Landing, now: datetime, out: Outgoing) -> None:
        """Report a landed firing: its events, the log entry, progress, a stop on
        failure, and the ``step`` that waited for it."""
        loaded = self._loaded
        flight = loaded.in_flight
        loaded.in_flight = None
        # A body that resized a place through a whole-list mutate edge is repaired
        # here, so the landing's lines already say so.
        TokenIds.resync(loaded.graph)
        events = self._flush(now, out)
        fired = next((e for e in events if isinstance(e, core_events.Fired) and e.transition == flight.name), None)
        failed = next((e for e in events if isinstance(e, core_events.Failed) and e.transition == flight.name), None)
        if fired is not None or failed is not None:
            announced, landed = _pairs(flight.consumed), _pairs((fired or failed).consumed)
            if announced != landed:
                print(
                    f"petritype runner: {flight.name} was launched as consuming {announced} "
                    f"but the engine took {landed}",
                    file=sys.stderr, flush=True,
                )
        else:
            self._never_took(flight, landing, out)

        if fired is not None or failed is not None:
            loaded.log_seq += 1
            consumed = (fired or failed).consumed
            loaded.log.append(wire.LogEntry(
                seq=loaded.log_seq,
                transition=flight.name,
                started_at=_iso(flight.started_at),
                duration_ms=fired.duration_ms if fired is not None else _ms(flight.started_at, now),
                outcome="fired" if fired is not None else "failed",
                consumed=_pairs(consumed),
                produced=_pairs(fired.produced) if fired is not None else [],
                error=failed.error if failed is not None else None,
                restored=failed.restored if failed is not None else None,
            ))
        if fired is not None:
            loaded.last_fired = flight.name
            loaded.last_progress_at = now

        if failed is not None or (fired is None and landing.error is not None):
            if loaded.running:
                cause = _cause(landing.error) if landing.error is not None else None
                loaded.running = False
                out.lines.append(wire.ExecutionStopped(
                    reason="transition_failed",
                    error_type=type(cause).__name__ if cause is not None else "Error",
                    message=str(cause) if cause is not None else failed.error,
                    marking_version=self._marking_version, running=False,
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
        out.lines.append(Lines.result(flight.step_id, wire.StepResult(
            fired=fired is not None or failed is not None,
            transition=flight.name if (fired is not None or failed is not None) else None,
            failed=True if failed is not None else None,
            marking_version=self._marking_version,
            running=loaded.running,
        )))

    def _never_took(self, flight: _InFlight, landing: Landing, out: Outgoing) -> None:
        """Close a ``launched`` whose firing landed without the engine reporting
        one: it raised before taking anything (a guard or selector inside the
        engine), or found the transition no longer fireable. The viewer is told
        ``failed``, ``restored`` when every announced token is where it was; the
        rest of the landing (no log entry, the ``step`` answer) goes on as for
        a firing that never happened."""
        loaded = self._loaded
        restored = all(
            token_id in loaded.place_nodes[place].token_ids for place, token_id in flight.consumed
        )
        error = (
            f"{type(landing.error).__name__}: {landing.error}" if landing.error is not None
            else f"{flight.name} was no longer fireable when its firing ran"
        )
        self._marking_version += 1
        notification = self._notification(core_events.Failed(
            transition=flight.name, consumed=tuple(flight.consumed), restored=restored, error=error,
        ))
        notification.marking_version = self._marking_version
        out.lines.append(notification.line())
        if not restored:
            out.lines.append(wire.Reloaded(
                generation=self.generation, reason="resync",
                marking_version=self._marking_version, running=loaded.running,
            ).line())

    def _drive(self, now: datetime, out: Outgoing) -> None:
        """Keep a running net going: launch the next firing if nothing is
        airborne, or handle quiescence (``idle`` once per spell for a ``"24/7"``
        net, ``execution_stopped`` for any other)."""
        loaded = self._loaded
        if loaded is None or not loaded.running or loaded.in_flight is not None:
            return
        try:
            transition = self._select()
        except Exception as error:
            loaded.running = False
            out.lines.append(wire.ExecutionStopped(
                reason="transition_failed", error_type=type(error).__name__, message=str(error),
                marking_version=self._marking_version, running=False,
            ).line())
            return
        if transition is not None:
            self._launch(transition, None, now, out)
        elif loaded.entry.mode == CONTINUOUS_MODE:
            if not loaded.idle:
                loaded.idle = True
                out.lines.append(wire.Idle(
                    idle_since=_iso(now), marking_version=self._marking_version, running=True,
                ).line())
        else:
            loaded.running = False
            out.lines.append(wire.ExecutionStopped(
                reason="no_fireable_transitions", marking_version=self._marking_version, running=False,
            ).line())

    # --- input ---

    def _inject(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        place = _string(params, "place")
        if "token" not in params:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, 'inject needs a "token"')
        loaded = self._loaded
        node = loaded.place_nodes.get(place)
        if node is None:
            raise ProtocolError(ErrorCode.UNKNOWN_PLACE, f"no place named {place!r}")
        if not loaded.serializable[place]:
            raise ProtocolError(
                ErrorCode.NOT_SERIALIZABLE, f"place {place!r} ({loaded.type_names[place]}) has no JSON form",
            )
        try:
            token = Serializable.decode(node.type, params["token"])
        except ValueError as error:
            raise ProtocolError(ErrorCode.INVALID_TOKEN, f"not a {loaded.type_names[place]}: {error}") from error
        try:
            self._apply(Extend(place=place, tokens=[token]))
        except ValueError as error:  # the strict context's own type check
            raise ProtocolError(ErrorCode.INVALID_TOKEN, str(error)) from error
        events = self._flush(now, out)
        added = next((e for e in events if isinstance(e, core_events.Added) and e.place == place), None)
        if added is None:
            raise ProtocolError(ErrorCode.INTERNAL, "the token was added without an event")
        out.lines.append(Lines.result(request_id, {
            "status": "injected", "id": str(added.token_id), "marking_version": self._marking_version,
        }))

    def _enable(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        self._toggle(Enable, request_id, params, now, out)

    def _disable(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        self._toggle(Disable, request_id, params, now, out)

    def _toggle(self, command, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        transition = _string(params, "transition")
        self._transition(transition)
        self._apply(command(transition=transition))
        self._flush(now, out)
        out.lines.append(Lines.result(request_id, {"marking_version": self._marking_version}))

    def _set_param(self, request_id: str, params: dict, now: datetime, out: Outgoing) -> None:
        """Python's ``set_param``: one keyword argument of one transition, through
        ``SetParam``. ``transition`` is required; the parameter must be one the
        function takes and no edge supplies; an annotated one is validated."""
        param = _string(params, "param")
        if not isinstance(params.get("transition"), str):
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS, 'the Python runner\'s set_param needs a string "transition"',
            )
        if "value" not in params:
            raise ProtocolError(ErrorCode.INVALID_PARAMS, 'set_param needs a "value"')
        transition = self._transition(params["transition"])
        value = self._param_value(transition, param, params["value"])
        self._apply(SetParam(transition=transition.name, kwargs={param: value}))
        self._flush(now, out)
        out.lines.append(Lines.result(request_id, {"marking_version": self._marking_version}))

    def _param_value(self, transition: FunctionTransitionNode, param: str, value: Any) -> Any:
        loaded = self._loaded
        supplied = {
            edge.argument
            for edges in (loaded.incoming.get(transition.name, ()), loaded.read.get(transition.name, ()))
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
        for transition in self._loaded.graph.transitions:
            if transition.name == name:
                return transition
        raise ProtocolError(ErrorCode.UNKNOWN_TRANSITION, f"no transition named {name!r}")

    def _apply(self, command) -> None:
        """Through the engine's command path: the inbox, drained at once."""
        ctx = self._loaded.ctx
        ctx.inbox.put_nowait(command)
        Runner.drain_inbox(ctx)

    # --- events ---

    def _flush(self, now: datetime, out: Outgoing) -> list:
        """Drain the graph's events and write one notification per event, each
        state notification raising ``marking_version`` by one; then ``fireable``
        if anything happened and the set changed. Returns the events."""
        loaded = self._loaded
        events = loaded.graph.drain_events()
        for event in events:
            notification = self._notification(event)
            if notification.moves_version:
                self._marking_version += 1
                notification.marking_version = self._marking_version
            out.lines.append(notification.line())
        if events:
            fireable = self._fireable_names()
            if fireable != loaded.last_fireable:
                loaded.last_fireable = fireable
                out.lines.append(wire.Fireable(
                    transitions=fireable, marking_version=self._marking_version, running=loaded.running,
                ).line())
        return events

    def _notification(self, event) -> wire.Notification:
        """One event as its notification, stamped with the current version (the
        caller raises it for a state notification)."""
        stamp = {"marking_version": self._marking_version, "running": self._loaded.running}
        if isinstance(event, core_events.Fired):
            # The second piece: ``launched`` already named what it consumed.
            return wire.Distributed(
                transition=event.transition,
                produced=[self._produced(place, token_id) for place, token_id in event.produced],
                duration_ms=event.duration_ms,
                **stamp,
            )
        if isinstance(event, core_events.Failed):
            return wire.Failed(
                transition=event.transition, error=event.error, restored=event.restored,
                consumed=_pairs(event.consumed), **stamp,
            )
        if isinstance(event, core_events.Added):
            token = self._live(event.place, event.token_id)
            entry = Tokens.entry(event.token_id, token, "") if token is not _MISSING else None
            return wire.Added(
                place=event.place, id=str(event.token_id),
                label=entry.label if entry else "", color=entry.color if entry else None, **stamp,
            )
        if isinstance(event, core_events.Removed):
            return wire.Removed(place=event.place, id=str(event.token_id), **stamp)
        if isinstance(event, core_events.Mutated):
            token = self._live(event.place, event.token_id)
            label = Tokens.entry(event.token_id, token, "").label if token is not _MISSING else None
            return wire.Mutated(place=event.place, id=str(event.token_id), label=label, **stamp)
        if isinstance(event, core_events.Toggled):
            return wire.Toggled(transition=event.transition, enabled=event.enabled, **stamp)
        if isinstance(event, core_events.ParamChanged):
            return wire.ParamChanged(param=event.param, transition=event.transition, **stamp)
        # Resynced, or an event this runner does not know: the marking moved in a
        # way it cannot describe.
        return wire.Reloaded(generation=self.generation, reason="resync", **stamp)

    def _produced(self, place: str, token_id: int) -> list[str]:
        token = self._live(place, token_id)
        if token is _MISSING:
            return [place, str(token_id), ""]
        return Tokens.produced(place, token_id, token)

    def _live(self, place: str, token_id: int) -> Any:
        node = self._loaded.place_nodes.get(place)
        if node is None:
            return _MISSING
        ids = node.token_ids
        for index in range(len(ids) - 1, -1, -1):  # new tokens are at the back
            if ids[index] == token_id:
                return node.tokens[index] if index < len(node.tokens) else _MISSING
        return _MISSING


# --- the firing coroutine ------------------------------------------------------------------


async def _fire(loaded: _Loaded, transition: FunctionTransitionNode, generation: int) -> Landing:
    """Fire ``transition`` through the engine's one-firing primitive, with a
    selector pinned to it (the owner already ran the net's own selector). The
    engine records the firing's events; the owner reads them on landing."""
    graph = loaded.graph
    graph.last_fired = None

    def pinned(_graph, fireable):
        return transition if any(t is transition for t in fireable) else None

    try:
        await ExecutableGraphOperations.fire_one_transition(
            executable_graph=graph,
            place_names_to_nodes=loaded.place_nodes,
            transition_names_to_incoming_edges=loaded.incoming,
            transition_names_to_outgoing_edges=loaded.outgoing,
            transition_names_to_read_edges=loaded.read,
            transition_selector=pinned,
            executor=loaded.ctx.thread_pool,
        )
    except Exception as error:
        return Landing(transition.name, generation, error)
    return Landing(transition.name, generation)


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
