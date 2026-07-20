"""The petritype Runner — observable, interactive execution of a net in selectable modes.

Lifted from the `examples/execution_modes/` prototype. **One way to define a net** (an
`ExecutableGraph`); **one `Runner`**; the execution mode is selected by `RunContext.mode`.

Layers built so far:
- **Execution (#7):** `SEQUENTIAL` / `CONCURRENT` firing over the engine's existing stages.
- **Observation (#3, #6):** sync-or-async observers handed the *live graph by reference*
  after each state change. Best-effort coalescing views; no lossless observer.
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
    """

    graph: ExecutableGraph
    mode: ExecutionMode = ExecutionMode.SEQUENTIAL
    observers: tuple[Observer, ...] = ()
    inbox: Optional[asyncio.Queue] = None
    stop: Optional[asyncio.Event] = None
    strict: bool = False
    disabled: set[str] = field(default_factory=set)
    thread_pool: Optional[Executor] = None


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

    async def run_to_completion(ctx: RunContext) -> ExecutableGraph:
        """Drive the net to quiescence using ``ctx.mode``, draining input between steps and
        notifying observers as it goes."""
        if ctx.mode is ExecutionMode.CONCURRENT:
            return await Runner._run_concurrent(ctx)
        return await Runner._run_sequential(ctx)

    async def run_indefinitely(ctx: RunContext, *, tick: float = 0.1) -> None:
        """Drive the net on an internal clock — forever — draining input each tick and
        notifying observers, until ``ctx.stop`` is set (or the task is cancelled).

        Unlike ``run_to_completion`` it does **not** stop on an idle tick: nothing enabled just
        means the net is waiting for input or a time-based guard. This is the net-driven /
        real-time regime (#6) — the caller launches it as a task and ends it via ``ctx.stop``.
        Observers are awaited between ticks, so keep them cheap; a slow renderer would throttle
        the clock.
        """
        if ctx.mode is ExecutionMode.CONCURRENT:
            await Runner._tick_concurrent(ctx, tick)
        else:
            await Runner._tick_sequential(ctx, tick)

    async def step(ctx: RunContext) -> int:
        """Drain queued input, then fire a single transition (sequential semantics). Returns
        the number fired (0 or 1). The observer-driven primitive: the caller renders, then
        decides when to call ``step`` again (a button, an animation-complete signal, a timer).
        """
        if Runner.drain_inbox(ctx):
            await _notify(ctx)
        _, fired = await ExecutableGraphOperations.execute_graph(
            executable_graph=ctx.graph,
            max_transitions=1,
            verbose=False,
            transition_selector=_selector_for(ctx),
            executor=ctx.thread_pool,
        )
        if fired:
            await _notify(ctx)
        return fired

    async def _run_sequential(ctx: RunContext) -> ExecutableGraph:
        await _notify(ctx)  # initial marking
        while True:
            if Runner.drain_inbox(ctx):
                await _notify(ctx)  # show the input's effect even before anything fires
            _, fired = await ExecutableGraphOperations.execute_graph(
                executable_graph=ctx.graph,
                max_transitions=1,
                verbose=False,
                transition_selector=_selector_for(ctx),
                executor=ctx.thread_pool,
            )
            if not fired:
                return ctx.graph
            await _notify(ctx)

    async def _run_concurrent(ctx: RunContext) -> ExecutableGraph:
        graph = ctx.graph
        place_nodes = MapPlaceNames.to_list_place_nodes(graph)
        incoming = MapTransitionNames.to_incoming_edges(graph)
        outgoing = MapTransitionNames.to_outgoing_edges(graph)
        read = MapTransitionNames.to_read_edges(graph)

        await _notify(ctx)  # initial marking
        in_flight: dict[asyncio.Future, str] = {}
        # What each task consumed, so a failed firing can put its tokens back.
        flight_state: dict[asyncio.Future, tuple] = {}
        while True:
            if Runner.drain_inbox(ctx):
                await _notify(ctx)

            airborne = set(in_flight.values())
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

            if set(in_flight.values()) != graph.in_flight:
                graph.in_flight = set(in_flight.values())
                await _notify(ctx)  # newly in-flight transitions light up as "working"

            if not in_flight:
                return graph

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
            graph.in_flight = set(in_flight.values())
            await _notify(ctx)  # tokens deposited — branches advanced one stage

    async def _tick_sequential(ctx: RunContext, tick: float) -> None:
        await _notify(ctx)
        while not (ctx.stop is not None and ctx.stop.is_set()):
            if Runner.drain_inbox(ctx):
                await _notify(ctx)
            _, fired = await ExecutableGraphOperations.execute_graph(
                executable_graph=ctx.graph,
                max_transitions=1,
                verbose=False,
                transition_selector=_selector_for(ctx),
                executor=ctx.thread_pool,
            )
            if fired:
                await _notify(ctx)
            if await _sleep_or_stop(ctx, tick):
                return

    async def _tick_concurrent(ctx: RunContext, tick: float) -> None:
        graph = ctx.graph
        place_nodes = MapPlaceNames.to_list_place_nodes(graph)
        incoming = MapTransitionNames.to_incoming_edges(graph)
        outgoing = MapTransitionNames.to_outgoing_edges(graph)
        read = MapTransitionNames.to_read_edges(graph)

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
