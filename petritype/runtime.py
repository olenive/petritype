"""The petritype Runner — observable, interactive execution of a net in selectable modes.

Lifted from the `examples/execution_modes/` prototype. **One way to define a net** (an
`ExecutableGraph`); **one `Runner`**; the execution mode is selected by `RunContext.mode`.

Layers built so far:
- **Execution (#7):** `SEQUENTIAL` / `CONCURRENT` firing over the engine's existing stages.
- **Observation (#3, #6):** sync-or-async observers handed the *live graph by reference*
  after each state change. Best-effort coalescing views; no lossless observer *stream* —
  diffing ``graph.fired_counts`` snapshots via ``fired_since`` recovers every firing.
- **Input (#4):** typed commands placed on `RunContext.inbox` (an `asyncio.Queue` the caller
  owns) and **drained between steps** — the runner is the only thing that mutates the graph.
  A control/UI produces commands; it never touches the net.
- **Real-time (#6):** `run_indefinitely` drives the net on an internal clock until `stop` is
  set, draining input each tick and surviving idle ticks (it never stops just because nothing
  is enabled).
- **Offloaded bodies (#8):** a synchronous transition marked `execution="thread"` runs on a
  thread-pool executor instead of the event loop, so blocking user code can't stall the runner.
- **Read arcs (#10):** a transition can read a place's tokens as an argument without consuming
  them (`transition_names_to_read_edges`).
- **Limits:** `Runner.run(ctx, stop_after_n_firings=N)` paces — it always returns a
  `RunSummary` whose `quiesced` flag tells a stop from completion. `RunContext.
  error_after_n_firings` is a run-wide fuse: `TooManyFiringsError` is raised *before* the
  net fires past it. Both count firings; `execute_graph`'s `max_transitions` is a
  deprecated alias of its `stop_after_n_firings`.
- **Validation & setup:** every entry point validates the full marking and builds the
  adjacency maps once at entry (both modes), then fires on those maps — per-firing cost
  no longer grows with net size. Mid-run tokens are validated at their own gates:
  deposits by the engine, inbox commands as they are applied.

Design notes realised here:
- **Functional, no `__init__`:** `Runner` is a namespace class of functions (matching
  `ExecutableGraphOperations`); run-state lives in the `ExecutableGraph` (mutated in place)
  plus a passive `RunContext` the caller owns.
- **Commands target places or transitions.** Places: `Extend` (append a list of tokens) /
  `SetTokens` (replace; `[]` clears). Transitions: `SetParam` (merge kwargs), `Enable` /
  `Disable`. Bad commands are dropped-and-logged, or raise under `RunContext.strict`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from concurrent.futures import Executor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Union

from pydantic import BaseModel, ConfigDict

from petritype.core.executable_graph_components import (
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    MapPlaceNames,
    MapTransitionNames,
)
from petritype.core.type_comparisons import CompareTypes

_log = logging.getLogger(__name__)

# An observer is handed the live graph after each state change. Sync or async.
Observer = Callable[[ExecutableGraph], Union[None, Awaitable[None]]]


def fired_since(previous: dict[str, int], current: dict[str, int]) -> dict[str, int]:
    """Per-transition firing counts that occurred between two snapshots of
    ``graph.fired_counts``. Keys with a zero delta are omitted.

    The lossless way for an observer to answer "what fired since I last looked",
    in every execution mode: snapshot ``dict(graph.fired_counts)`` (a copy — the
    live dict keeps mutating), then diff it against the counts at the next
    notification. ``graph.last_fired`` can name only one completion per
    concurrent deposit batch; the counts lose nothing.
    """
    return {
        name: count - previous.get(name, 0)
        for name, count in current.items()
        if count - previous.get(name, 0) != 0
    }


class RunSummary(BaseModel):
    """What one ``Runner.run`` call executed. Constructed by the Runner — treat it as
    read-only output. New fields may be added in any release: reading attributes is
    forward-compatible; don't construct one yourself or match its fields exhaustively.

    - ``quiesced`` — True: nothing was left to fire. False: ``stop_after_n_firings``
      ended the call and the net may have more work; call ``run`` again to continue
      (state lives in the graph — nothing resumes by itself).
    - ``n_fired``  — transition firings during this call.
    - ``fired``    — per-transition firing counts for this call (zero-count names
      omitted): the ``fired_since`` diff of the call's entry/exit snapshots.

    Per-firing detail (timestamps, token values, orderings) is the observers' job —
    see ``fired_since`` — not extra fields here.
    """

    model_config = ConfigDict(frozen=True)

    quiesced: bool
    n_fired: int
    fired: dict[str, int]


class TooManyFiringsError(RuntimeError):
    """``RunContext.error_after_n_firings`` was spent and the net attempted another
    firing. Raised *before* the offending firing consumes anything. ``summary`` is a
    ``RunSummary`` of what the interrupted call had executed (attached when the error
    leaves through a public ``Runner`` entry point)."""

    def __init__(self, message: str, summary: Optional[RunSummary] = None):
        super().__init__(message)
        self.summary = summary


class ExecutionMode(Enum):
    SEQUENTIAL = "sequential"
    CONCURRENT = "concurrent"


# --- Input commands (passive data; produced by a UI/caller, applied only by the runner) ---


@dataclass
class Extend:
    """Append a list of tokens to a place. The argument is *always* a list of tokens; a
    single token is a one-element list (`[token]`)."""

    place: str
    tokens: list


@dataclass
class SetTokens:
    """Replace a place's tokens with the provided list. `[]` clears the place."""

    place: str
    tokens: list


@dataclass
class SetParam:
    """Merge `kwargs` into a transition's `kwargs` (tunable parameters)."""

    transition: str
    kwargs: dict


@dataclass
class Enable:
    """(Re-)allow a transition to fire."""

    transition: str


@dataclass
class Disable:
    """Stop a transition from being selected to fire (toggle part of the process off)."""

    transition: str


Command = Union[Extend, SetTokens, SetParam, Enable, Disable]


@dataclass
class RunContext:
    """Passive bundle of run-state the caller owns (data only — no behaviour, no
    hand-written ``__init__``).

    - ``graph``     — the single source of truth (mutated in place).
    - ``mode``      — selects the firing orchestrator.
    - ``observers`` — notified with the live graph after each state change.
    - ``inbox``     — an ``asyncio.Queue`` of input commands; drained between steps.
    - ``stop``      — an ``asyncio.Event``; set it to end ``run_indefinitely``.
    - ``strict``    — if True, a malformed command raises; otherwise it is dropped-and-logged.
    - ``disabled``  — transition names currently disabled via ``Disable`` (runner-level, the
                      engine stays unaware).
    - ``thread_pool`` — executor for transitions marked ``execution="thread"``; ``None`` uses
                      the default ThreadPoolExecutor.
    - ``error_after_n_firings`` — run-wide fuse: if set, the Runner raises
                      ``TooManyFiringsError`` the moment the net *attempts* firing
                      number n+1 (checked before firing/launching, so a net that
                      fires exactly n and quiesces succeeds). The tally spans every
                      call driven through this context — this is runaway protection,
                      not pacing; for pacing pass ``stop_after_n_firings`` to
                      ``Runner.run``.
    - ``n_fired``   — total firings driven through this context, maintained by the
                      Runner (the fuse's tally).
    """

    graph: ExecutableGraph
    mode: ExecutionMode = ExecutionMode.SEQUENTIAL
    observers: tuple[Observer, ...] = ()
    inbox: Optional[asyncio.Queue] = None
    stop: Optional[asyncio.Event] = None
    strict: bool = False
    disabled: set[str] = field(default_factory=set)
    thread_pool: Optional[Executor] = None
    error_after_n_firings: Optional[int] = None
    n_fired: int = 0


@dataclass
class ControlSpec:
    """A declarative binding of a UI control to a place/transition. Apps author a control-map
    ``{name: ControlSpec}`` — kept *off* the net — and the UI renders it, feeding each widget's
    value to ``to_command`` and enqueuing whatever command comes back (``None`` = nothing to do).

    - ``node``       — the place/transition the control targets (for labelling / future co-location).
    - ``kind``       — which widget: ``"button" | "toggle" | "slider" | "select"``.
    - ``label``      — display label.
    - ``to_command`` — ``value -> Command | None`` (turns the UI value into an inbox command).
    - ``config``     — widget specifics (slider ``start``/``stop``/``step``, select ``options``,
                       default ``value``).
    """

    node: str
    kind: str
    label: str
    to_command: Callable[[Any], Optional[Command]]
    config: dict = field(default_factory=dict)


def apply_control(ctx: RunContext, spec: ControlSpec, value: Any) -> Optional[Command]:
    """Turn a control's value into a command via ``spec.to_command`` and enqueue it on
    ``ctx.inbox`` (if any). Returns the command, or ``None`` if the control produced nothing.
    The control never touches the graph — only the inbox."""
    command = spec.to_command(value)
    if command is not None and ctx.inbox is not None:
        ctx.inbox.put_nowait(command)
    return command


# --- helpers -----------------------------------------------------------------------------


async def _notify(ctx: RunContext) -> None:
    """Hand the live graph to each observer; await any that are coroutines."""
    for observe in ctx.observers:
        result = observe(ctx.graph)
        if inspect.isawaitable(result):
            await result


def _transition_named(graph: ExecutableGraph, name: str) -> Optional[FunctionTransitionNode]:
    for transition in graph.transitions:
        if transition.name == name:
            return transition
    return None


def _reject(ctx: RunContext, reason: str) -> bool:
    """Strict mode raises; otherwise drop-and-log. Returns False (command not applied)."""
    if ctx.strict:
        raise ValueError(f"invalid command: {reason}")
    _log.warning("dropping invalid command: %s", reason)
    return False


def _apply_command(ctx: RunContext, cmd: Command) -> bool:
    """Apply one input command to the live graph. Returns True if applied."""
    graph = ctx.graph

    if isinstance(cmd, (Extend, SetTokens)):
        place = graph.place_named(cmd.place)
        if place is None:
            return _reject(ctx, f"unknown place {cmd.place!r}")
        if not isinstance(cmd.tokens, list):
            return _reject(
                ctx, f"{type(cmd).__name__}.tokens must be a list, got {type(cmd.tokens).__name__}"
            )
        for token in cmd.tokens:
            if not CompareTypes.between_value_and_type(token, place.type):
                return _reject(
                    ctx, f"token {token!r} does not match place {place.name!r} type {place.type!r}"
                )
        if isinstance(cmd, Extend):
            place.tokens.extend(cmd.tokens)
        else:  # SetTokens
            place.tokens = list(cmd.tokens)
        return True

    if isinstance(cmd, SetParam):
        transition = _transition_named(graph, cmd.transition)
        if transition is None:
            return _reject(ctx, f"unknown transition {cmd.transition!r}")
        if not isinstance(cmd.kwargs, dict):
            return _reject(ctx, f"SetParam.kwargs must be a dict, got {type(cmd.kwargs).__name__}")
        transition.kwargs = {**(transition.kwargs or {}), **cmd.kwargs}
        return True

    if isinstance(cmd, (Enable, Disable)):
        if _transition_named(graph, cmd.transition) is None:
            return _reject(ctx, f"unknown transition {cmd.transition!r}")
        if isinstance(cmd, Disable):
            ctx.disabled.add(cmd.transition)
        else:
            ctx.disabled.discard(cmd.transition)
        return True

    return _reject(ctx, f"unrecognised command {cmd!r}")


def _selector_for(ctx: RunContext):
    """A transition selector that skips disabled transitions, or None (engine default) when
    nothing is disabled. Recomputed each step since ``disabled`` may change between drains."""
    if not ctx.disabled:
        return None
    disabled = ctx.disabled

    def selector(graph, enabled):
        for transition in enabled:
            if transition.name not in disabled:
                return transition
        return None

    return selector


def _prepare(graph: ExecutableGraph) -> tuple[dict, dict, dict, dict]:
    """Validate the full marking and build the adjacency maps the loops fire on —
    once per entry point, not once per firing (both modes; previously sequential
    validated every token on every step and concurrent never validated at all).
    Returns ``(place_nodes, incoming, outgoing, read)``.

    Hoisting is safe because nothing mid-run invalidates either: no inbox command
    changes topology (``SetTokens`` rebinds ``place.tokens`` on the node object the
    maps reference; ``SetParam`` merges into ``transition.kwargs`` in place), and
    tokens arriving later are validated at their own gates — deposits by stage 3,
    inbox commands by ``_apply_command``."""
    ExecutableGraphCheck.ensure_all_token_types_match_place_types(graph)
    return (
        MapPlaceNames.to_list_place_nodes(graph),
        MapTransitionNames.to_incoming_edges(graph),
        MapTransitionNames.to_outgoing_edges(graph),
        MapTransitionNames.to_read_edges(graph),
    )


def _fuse_spent(ctx: RunContext) -> bool:
    return ctx.error_after_n_firings is not None and ctx.n_fired >= ctx.error_after_n_firings


def _blow_fuse(ctx: RunContext, transition_name: str) -> None:
    raise TooManyFiringsError(
        f"error_after_n_firings={ctx.error_after_n_firings} spent after {ctx.n_fired} "
        f"firings, and {transition_name!r} attempted to fire"
    )


def _check_fuse(ctx: RunContext, place_nodes: dict, incoming: dict, read: dict) -> None:
    """Sequential-path fuse check on prebuilt maps: raise iff the fuse is spent *and*
    a firing would be attempted (some non-disabled transition is enabled). Costs one
    int compare until the fuse is spent. A custom selector that would have vetoed
    every enabled transition is not consulted — an enabled net with a spent fuse
    counts as attempting. (The concurrent loops don't need this sweep: they check
    the fuse right before each launch, where enabledness is already established.)"""
    if not _fuse_spent(ctx):
        return
    graph = ctx.graph
    for transition in graph.transitions:
        if transition.name in ctx.disabled:
            continue
        if ExecutableGraphCheck.transition_is_enabled(
            transition=transition,
            executable_graph=graph,
            transition_names_to_incoming_edges=incoming,
            place_names_to_nodes=place_nodes,
            transition_names_to_read_edges=read,
        ):
            _blow_fuse(ctx, transition.name)


async def _fire_one(ctx: RunContext, place_nodes: dict, incoming: dict, outgoing: dict, read: dict) -> bool:
    """Fire at most one transition on prebuilt maps (sequential semantics), honouring
    ``Disable`` and tallying the fuse. Returns True iff something fired.

    ``last_fired`` is reset before each attempt — the same contract as one
    ``execute_graph`` call, so a no-fire attempt leaves it ``None`` (a quiesced
    sequential run still ends with ``last_fired is None``, as before)."""
    ctx.graph.last_fired = None
    fired_name = await ExecutableGraphOperations.fire_one_transition(
        executable_graph=ctx.graph,
        place_names_to_nodes=place_nodes,
        transition_names_to_incoming_edges=incoming,
        transition_names_to_outgoing_edges=outgoing,
        transition_names_to_read_edges=read,
        transition_selector=_selector_for(ctx),
        executor=ctx.thread_pool,
    )
    if fired_name is None:
        return False
    ctx.n_fired += 1
    return True


def _summarise(previous: dict[str, int], graph: ExecutableGraph, quiesced: bool) -> RunSummary:
    fired = fired_since(previous, graph.fired_counts)
    return RunSummary(quiesced=quiesced, n_fired=sum(fired.values()), fired=fired)


async def _sleep_or_stop(ctx: RunContext, tick: float) -> bool:
    """Sleep one tick, or return early if ``ctx.stop`` is set. Returns True iff stop was
    requested (so the caller should break out of its loop)."""
    if ctx.stop is None:
        await asyncio.sleep(tick)
        return False
    try:
        await asyncio.wait_for(ctx.stop.wait(), timeout=tick)
        return True
    except asyncio.TimeoutError:
        return False


# --- the runner --------------------------------------------------------------------------


class Runner:
    """Functions that drive a ``RunContext``. No instances — call ``Runner.run_to_completion(ctx)``."""

    def drain_inbox(ctx: RunContext) -> int:
        """Apply all currently-queued input commands, in FIFO order. Synchronous (no await),
        so it is safe to call between fires without interleaving a torn marking. Returns the
        number of commands applied."""
        if ctx.inbox is None:
            return 0
        applied = 0
        while True:
            try:
                cmd = ctx.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if _apply_command(ctx, cmd):
                applied += 1
        return applied

    async def run(ctx: RunContext, *, stop_after_n_firings: Optional[int] = None) -> RunSummary:
        """Drive the net using ``ctx.mode`` until it quiesces — or, if
        ``stop_after_n_firings`` is set, until that many firings have run this call —
        and report what happened.

        Always returns a ``RunSummary``. ``summary.quiesced`` distinguishes "nothing
        left to fire" from "the limit stopped this call — work may remain"; a stopped
        run continues from exactly where it left off on the next ``run`` call (state
        lives in the graph; nothing resumes by itself). In concurrent mode the limit
        caps *launches* — firings already in flight finish and deposit before the call
        returns.

        If ``ctx.error_after_n_firings`` is spent and the net attempts another firing,
        raises ``TooManyFiringsError`` with the interrupted call's summary attached.
        Cancellation behaves as on ``run_to_completion``.
        """
        previous = dict(ctx.graph.fired_counts)
        try:
            if ctx.mode is ExecutionMode.CONCURRENT:
                quiesced = await Runner._run_concurrent(ctx, stop_after_n_firings)
            else:
                quiesced = await Runner._run_sequential(ctx, stop_after_n_firings)
        except TooManyFiringsError as error:
            error.summary = _summarise(previous, ctx.graph, quiesced=False)
            raise
        return _summarise(previous, ctx.graph, quiesced)

    async def run_to_completion(ctx: RunContext) -> ExecutableGraph:
        """Drive the net to quiescence using ``ctx.mode``, draining input between steps and
        notifying observers as it goes. Equivalent to ``Runner.run(ctx)`` (which also
        reports what fired), returning the graph instead of a summary.

        Cancellation — e.g. ``asyncio.wait_for(Runner.run_to_completion(ctx), timeout=...)``
        — lands between steps and at ``await`` points inside async transition bodies. A
        synchronous body delays it until the body returns; a body offloaded with
        ``execution="thread"`` cannot be interrupted at all — its future is abandoned while
        the thread runs to completion (orphaned work, held locks). In concurrent mode,
        in-flight tasks are cancelled on the way out. Tokens consumed by interrupted firings
        are not restored: the graph is mutated in place, so after cancellation the caller's
        reference shows the partial marking — useful for diagnosis, not a consistent final
        state.
        """
        await Runner.run(ctx)
        return ctx.graph

    async def run_indefinitely(ctx: RunContext, *, tick: float = 0.1) -> None:
        """Drive the net on an internal clock — forever — draining input each tick and
        notifying observers, until ``ctx.stop`` is set (or the task is cancelled).

        Unlike ``run_to_completion`` it does **not** stop on an idle tick: nothing enabled just
        means the net is waiting for input or a time-based guard. This is the net-driven /
        real-time regime (#6) — the caller launches it as a task and ends it via ``ctx.stop``.
        Observers are awaited between ticks, so keep them cheap; a slow renderer would throttle
        the clock.

        Cancellation behaves as on ``run_to_completion``: it lands between ticks and at
        ``await`` points inside async bodies; a synchronous body finishes first; an
        ``execution="thread"`` body cannot be interrupted (the thread runs to completion).
        In concurrent mode, in-flight tasks are cancelled on the way out — their consumed
        tokens are not restored, so the graph shows the partial marking.

        ``ctx.error_after_n_firings`` applies here too: an idle net past the fuse keeps
        ticking (nothing is attempting to fire), but the moment a firing is attempted,
        ``TooManyFiringsError`` is raised out of this call with the summary attached.
        """
        previous = dict(ctx.graph.fired_counts)
        try:
            if ctx.mode is ExecutionMode.CONCURRENT:
                await Runner._tick_concurrent(ctx, tick)
            else:
                await Runner._tick_sequential(ctx, tick)
        except TooManyFiringsError as error:
            error.summary = _summarise(previous, ctx.graph, quiesced=False)
            raise

    async def step(ctx: RunContext) -> int:
        """Drain queued input, then fire a single transition (sequential semantics). Returns
        the number fired (0 or 1). The observer-driven primitive: the caller renders, then
        decides when to call ``step`` again (a button, an animation-complete signal, a timer).
        Honours ``ctx.error_after_n_firings`` — a step past a spent fuse raises before
        firing anything.
        """
        if Runner.drain_inbox(ctx):
            await _notify(ctx)
        place_nodes, incoming, outgoing, read = _prepare(ctx.graph)
        try:
            _check_fuse(ctx, place_nodes, incoming, read)
        except TooManyFiringsError as error:
            # The fuse fires before anything is consumed, so this call ran nothing.
            error.summary = RunSummary(quiesced=False, n_fired=0, fired={})
            raise
        fired = await _fire_one(ctx, place_nodes, incoming, outgoing, read)
        if fired:
            await _notify(ctx)
        return 1 if fired else 0

    async def _run_sequential(ctx: RunContext, stop_after_n_firings: Optional[int] = None) -> bool:
        """Returns True if the net quiesced, False if ``stop_after_n_firings`` ended
        the call first (work may remain)."""
        place_nodes, incoming, outgoing, read = _prepare(ctx.graph)
        await _notify(ctx)  # initial marking
        fired_this_call = 0
        while True:
            if stop_after_n_firings is not None and fired_this_call >= stop_after_n_firings:
                return False
            if Runner.drain_inbox(ctx):
                await _notify(ctx)  # show the input's effect even before anything fires
            _check_fuse(ctx, place_nodes, incoming, read)
            if not await _fire_one(ctx, place_nodes, incoming, outgoing, read):
                return True
            fired_this_call += 1
            await _notify(ctx)

    async def _run_concurrent(ctx: RunContext, stop_after_n_firings: Optional[int] = None) -> bool:
        """Returns True if the net quiesced, False if ``stop_after_n_firings`` capped
        this call's launches (work may remain). In-flight firings always finish and
        deposit before returning."""
        graph = ctx.graph
        place_nodes, incoming, outgoing, read = _prepare(graph)

        await _notify(ctx)  # initial marking
        in_flight: dict[asyncio.Future, str] = {}
        # What each task consumed, so a failed firing can put its tokens back.
        flight_state: dict[asyncio.Future, tuple] = {}
        launched_this_call = 0
        try:
            while True:
                if Runner.drain_inbox(ctx):
                    await _notify(ctx)

                capped = (
                    stop_after_n_firings is not None
                    and launched_this_call >= stop_after_n_firings
                )
                airborne = set(in_flight.values())
                if not capped:
                    for transition in graph.transitions:
                        if transition.name in airborne or transition.name in ctx.disabled:
                            continue  # already running, or toggled off via Disable
                        if ExecutableGraphCheck.transition_is_enabled(
                            transition=transition,
                            executable_graph=graph,
                            transition_names_to_incoming_edges=incoming,
                            place_names_to_nodes=place_nodes,
                            transition_names_to_read_edges=read,
                        ):
                            if _fuse_spent(ctx):
                                _blow_fuse(ctx, transition.name)
                            # Consume synchronously (no await) so peers can't grab the same tokens...
                            args, _ = ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
                                transition=transition,
                                transition_names_to_incoming_edges=incoming,
                                place_names_to_nodes=place_nodes,
                                transition_names_to_read_edges=read,
                            )
                            # ...then launch the body as a background task.
                            task = asyncio.ensure_future(
                                ExecutableGraphOperations.stage_2_call_transition_function(
                                    transition=transition,
                                    tokens_kwargs=args,
                                    transition_names_to_outgoing_edges=outgoing,
                                    place_names_to_nodes=place_nodes,
                                    executor=ctx.thread_pool,
                                )
                            )
                            in_flight[task] = transition.name
                            flight_state[task] = (transition, args)
                            airborne.add(transition.name)
                            launched_this_call += 1
                            ctx.n_fired += 1
                            if (
                                stop_after_n_firings is not None
                                and launched_this_call >= stop_after_n_firings
                            ):
                                capped = True
                                break

                if set(in_flight.values()) != graph.in_flight:
                    graph.in_flight = set(in_flight.values())
                    await _notify(ctx)  # newly in-flight transitions light up as "working"

                if not in_flight:
                    # Nothing airborne and nothing launchable: either the net quiesced,
                    # or the pacing cap stopped the launch sweep (work may remain).
                    return not capped

                done, _pending = await asyncio.wait(set(in_flight), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    name = in_flight.pop(task)
                    fired_transition, consumed = flight_state.pop(task)
                    try:
                        outputs = task.result()
                    except Exception as exception:
                        graph.in_flight = set(in_flight.values())
                        # Raises TransitionFailedError carrying the consumed tokens;
                        # restores them first when graph.restore_tokens_on_failure is set.
                        ExecutableGraphOperations.handle_failed_firing(
                            transition=fired_transition,
                            exception=exception,
                            input_edge_names_to_tokens=consumed,
                            transition_names_to_incoming_edges=incoming,
                            place_names_to_nodes=place_nodes,
                            restore_tokens_on_failure=graph.restore_tokens_on_failure,
                        )
                    ExecutableGraphOperations.add_tokens_to_places(
                        output_place_names_to_tokens=outputs,
                        place_names_to_nodes=place_nodes,
                    )
                    graph.step_count += 1
                    graph.fired_counts[name] = graph.fired_counts.get(name, 0) + 1
                    # Convenience only: names *one* completion of this batch. Observers
                    # that need every firing should diff fired_counts via fired_since.
                    graph.last_fired = name
                graph.in_flight = set(in_flight.values())
                await _notify(ctx)  # tokens deposited — branches advanced one stage
        finally:
            # On cancellation, or a failed firing propagating out: don't orphan
            # in-flight bodies. asyncio.wait does not cancel what it waits on, and
            # an abandoned task's outputs would never be deposited — silent token
            # loss on top of whatever interrupted the run.
            for task in in_flight:
                task.cancel()
            graph.in_flight = set()

    async def _tick_sequential(ctx: RunContext, tick: float) -> None:
        place_nodes, incoming, outgoing, read = _prepare(ctx.graph)
        await _notify(ctx)
        while not (ctx.stop is not None and ctx.stop.is_set()):
            if Runner.drain_inbox(ctx):
                await _notify(ctx)
            _check_fuse(ctx, place_nodes, incoming, read)
            if await _fire_one(ctx, place_nodes, incoming, outgoing, read):
                await _notify(ctx)
            if await _sleep_or_stop(ctx, tick):
                return

    async def _tick_concurrent(ctx: RunContext, tick: float) -> None:
        graph = ctx.graph
        place_nodes, incoming, outgoing, read = _prepare(graph)

        await _notify(ctx)
        in_flight: dict[asyncio.Future, str] = {}
        # What each task consumed, so a failed firing can put its tokens back.
        flight_state: dict[asyncio.Future, tuple] = {}
        try:
            while not (ctx.stop is not None and ctx.stop.is_set()):
                changed = bool(Runner.drain_inbox(ctx))

                airborne = set(in_flight.values())
                for transition in graph.transitions:
                    if transition.name in airborne or transition.name in ctx.disabled:
                        continue
                    if ExecutableGraphCheck.transition_is_enabled(
                        transition=transition,
                        executable_graph=graph,
                        transition_names_to_incoming_edges=incoming,
                        place_names_to_nodes=place_nodes,
                        transition_names_to_read_edges=read,
                    ):
                        if _fuse_spent(ctx):
                            _blow_fuse(ctx, transition.name)
                        args, _ = ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
                            transition=transition,
                            transition_names_to_incoming_edges=incoming,
                            place_names_to_nodes=place_nodes,
                            transition_names_to_read_edges=read,
                        )
                        task = asyncio.ensure_future(
                            ExecutableGraphOperations.stage_2_call_transition_function(
                                transition=transition,
                                tokens_kwargs=args,
                                transition_names_to_outgoing_edges=outgoing,
                                place_names_to_nodes=place_nodes,
                                executor=ctx.thread_pool,
                            )
                        )
                        in_flight[task] = transition.name
                        flight_state[task] = (transition, args)
                        airborne.add(transition.name)
                        ctx.n_fired += 1
                        changed = True

                for task in [t for t in in_flight if t.done()]:
                    name = in_flight.pop(task)
                    fired_transition, consumed = flight_state.pop(task)
                    try:
                        outputs = task.result()
                    except Exception as exception:
                        # Raises TransitionFailedError carrying the consumed
                        # tokens; restores them first when
                        # graph.restore_tokens_on_failure is set.
                        ExecutableGraphOperations.handle_failed_firing(
                            transition=fired_transition,
                            exception=exception,
                            input_edge_names_to_tokens=consumed,
                            transition_names_to_incoming_edges=incoming,
                            place_names_to_nodes=place_nodes,
                            restore_tokens_on_failure=graph.restore_tokens_on_failure,
                        )
                    ExecutableGraphOperations.add_tokens_to_places(
                        output_place_names_to_tokens=outputs,
                        place_names_to_nodes=place_nodes,
                    )
                    graph.step_count += 1
                    graph.fired_counts[name] = graph.fired_counts.get(name, 0) + 1
                    # Convenience only: names *one* completion of this batch. Observers
                    # that need every firing should diff fired_counts via fired_since.
                    graph.last_fired = name
                    changed = True

                graph.in_flight = set(in_flight.values())
                if changed:
                    await _notify(ctx)
                if await _sleep_or_stop(ctx, tick):
                    return
        finally:
            for task in in_flight:
                task.cancel()
            graph.in_flight = set()
