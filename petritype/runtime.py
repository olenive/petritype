"""The petritype Runner — observable, interactive execution of a net in selectable modes.

Lifted from the `examples/execution_modes/` prototype. **One way to define a net** (an
`ExecutableGraph`); **one `Runner`**; the execution mode is selected by `RunContext.mode`.

Layers built so far:
- **Execution (#7):** `SEQUENTIAL` / `CONCURRENT` firing over the engine's existing stages.
- **Observation (#3, #6):** sync-or-async observers handed the *live graph by reference*
  after each state change. Best-effort coalescing views; no lossless observer *stream* —
  diffing ``graph.fired_counts`` snapshots via ``fired_since`` recovers every firing.
  ``graph.in_flight`` names the transitions whose bodies are running, in both modes: in
  sequential mode it holds the one firing transition for the length of its body, so a
  thread reading the graph during a long synchronous body can tell what is working.
  Observers are also notified when a transition goes in flight, in both modes, so one on
  the event loop sees it working before its body runs (the order of calls is at
  ``Observer``). An inline body holds the loop until it returns, so anything an observer
  only schedules runs after the body; an async body lets that work run at its awaits, and
  a body marked ``execution="thread"`` leaves the loop free.
- **Input (#4):** typed commands placed on `RunContext.inbox` (an `asyncio.Queue` the caller
  owns) and **drained between steps** — the runner is the only thing that mutates the graph.
  A control/UI produces commands; it never touches the net.
- **Real-time (#6):** `run_indefinitely` drives the net on an internal clock until `stop` is
  set, draining input each tick and surviving idle ticks (it never stops just because nothing
  is fireable).
- **Offloaded bodies (#8):** a synchronous transition marked `execution="thread"` runs on a
  thread-pool executor instead of the event loop, so blocking user code can't stall the runner.
- **Non-consuming edges (#10):** a transition can be handed a place's tokens as an argument
  without consuming them, through a snapshot, survey, mutate or read edge
  (`transition_names_to_read_edges`, a name kept from before the read edges of 2026-11-23).
- **Limits:** `Runner.run(ctx, stop_after_n_firings=N)` paces — it always returns a
  `RunSummary` whose `quiesced` flag tells a stop from completion. `RunContext.
  error_after_n_firings` is a run-wide fuse: `TooManyFiringsError` is raised *before* the
  net fires past it. Both count firings; `execute_graph`'s `max_transitions` is a
  deprecated alias of its `stop_after_n_firings`.
- **Validation & setup:** every entry point validates the full marking and builds the
  adjacency maps once at entry (both modes), then fires on those maps — per-firing cost
  no longer grows with net size. Mid-run tokens are validated at their own gates:
  deposits by the engine, inbox commands as they are applied.
- **Nested tokens:** a token of a ``nested`` place holds nets of its own (``NestedNets``),
  which every loop here runs as part of running the outer net, in the order each place's
  stepping rule gives: sequential firing makes one deep step through
  ``fire_one_transition``; the concurrent loops take the same three parts round the outer
  net's launch sweep, which is the outer net's turn: an inner firing ahead of it
  (inner-first places, then the ring after its cursor), else the sweep, else one behind it
  (the rest of the ring, then the outer-first places once nothing outer is in flight). An
  inner firing runs inline while outer bodies may be in flight, never in a token one of
  them holds through a mutate or a read edge, and never in the subnet of a subnet place one of whose
  ports an outer firing in flight has an edge on. Inner firings count as firings of the
  outer net under their ``InnerFiring``.

Design notes realised here:
- **Functional, no `__init__`:** `Runner` is a namespace class of functions (matching
  `ExecutableGraphOperations`); run-state lives in the `ExecutableGraph` (mutated in place)
  plus a passive `RunContext` the caller owns.
- **Commands target places, transitions or the net.** Places: `Extend` (append a list of
  tokens) / `SetTokens` (replace; `[]` clears). Transitions: `SetParam` (merge kwargs),
  `Enable` / `Disable`, `Fire` (fire it now if it is fireable). The net: `SetNetParam` (one
  of ``graph.params``). Bad commands are dropped-and-logged, or raise under `RunContext.strict`.
- **Closed nets:** every net is closed unless its definition declares it open, the net a
  `RunContext` drives included. A command the program makes is outside the net, so
  `Extend` and `SetTokens` at a place of a closed net that is not one of its ports are
  refused with `ClosedNetError`, raised in every context, strict or not, since the place
  not being a port is a matter of the net's definition; a command a control of the net gives (`apply_control`, `by_control`) is
  inside and lands, pressed by the program or a viewer, and is refused to a body that holds the net,
  and to everyone while a body holds it, until the firing lands. The knobs and `Fire` stay with the
  program (`ClosedNets`).
- **Controls:** a control-map ``{name: ControlSpec}`` binds widgets to commands; a net
  declares one with ``@petri_net(controls=...)`` and its place styles (``PlaceStyle``) with
  ``place_styles=...``, which the protocol runner serves to viewers (PROTOCOL.md §5.1, §6).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import math
import sys
import time
from bisect import bisect_left
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, NoReturn, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict

from petritype.core.executable_graph_components import (
    ClosedNets,
    ExecutableGraph,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FiringTokenIds,
    FunctionTransitionNode,
    GraphEvents,
    HeldByBodyError,
    MapPlaceNames,
    MapTransitionNames,
    MutateEdge,
    NestedNets,
    PetritypeError,
    Questions,
    ReadAllEdge,
    ReadEdge,
    SnapshotEdge,
    Stage1Plan,
    SubnetPlaceNode,
    TokenIds,
    default_transition_selector,
)
from petritype.core.events import Added, InnerFiring, ParamChanged, PlaceKey, Removed, Toggled
from petritype.core.type_comparisons import CompareTypes

_log = logging.getLogger(__name__)

# An observer is handed the live graph after each state change. Sync or async.
#
# In sequential mode the calls come in this order: the initial marking (``step`` has
# none), the effect of drained input, then for each firing one call at launch and one
# after completion. At the launch call ``in_flight`` holds the one transition's name, the
# consumed tokens are out of their places and not yet anywhere else, ``last_fired`` is
# None, ``step_count`` and ``fired_counts`` are unchanged, and the log holds no ``Fired``
# for the firing yet. An inline body holds the loop until it returns, so an observer's
# own work completes before the body starts and anything it only schedules runs after
# the body returns; an async body lets that scheduled work run at its awaits, and a body
# marked ``execution="thread"`` leaves the loop free. A firing inside an inner net gets
# only the call after it, and a ``Fire`` command's firing only the call after the drain.
# The concurrent loops call observers when the set in flight changes and after deposits.
Observer = Callable[[ExecutableGraph], Union[None, Awaitable[None]]]


def fired_since(
    previous: dict[Union[str, InnerFiring], int], current: dict[Union[str, InnerFiring], int],
) -> dict[Union[str, InnerFiring], int]:
    """Per-transition firing counts that occurred between two snapshots of
    ``graph.fired_counts``. Keys with a zero delta are omitted. A firing inside an
    inner net is keyed by its ``InnerFiring``, as ``fired_counts`` keys it.

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
      omitted): the ``fired_since`` diff of the call's entry/exit snapshots. A
      firing inside an inner net is counted under its ``InnerFiring``, the path to
      the net and the transition's own name.

    Per-firing detail (timestamps, token values, orderings) is the observers' job —
    see ``fired_since`` — not extra fields here.
    """

    model_config = ConfigDict(frozen=True)

    quiesced: bool
    n_fired: int
    fired: dict[Union[str, InnerFiring], int]


class TooManyFiringsError(PetritypeError, RuntimeError):
    """``RunContext.error_after_n_firings`` was spent and the net attempted another
    firing. Raised *before* the offending firing consumes anything. ``summary`` is a
    ``RunSummary`` of what the interrupted call had executed (attached when the error
    leaves through a public ``Runner`` entry point)."""

    def __init__(self, message: str, summary: Optional[RunSummary] = None):
        super().__init__(message)
        self.summary = summary


class CancellationTimeoutError(PetritypeError, RuntimeError):
    """A concurrent run that was ending cancelled the bodies still in flight, and
    some had not stopped after the net's ``cancellation_timeout_seconds``: a body
    that catches ``asyncio.CancelledError`` and carries on, or whose cleanup takes
    longer. ``transitions`` names them. The run has let go of them: what they
    consumed is in no place, what they return goes nowhere, and their claims are
    released, so the marking is as partial as after any cancelled run. Raised when
    the run would otherwise have ended without an error (it was stopped); when the
    run was ending with an error of its own, the caller's cancellation, or an error
    whose wait for the firings in flight to land ran out, that error is raised with
    a note that says the same."""

    def __init__(self, message: str, transitions: tuple[str, ...] = ()):
        super().__init__(message)
        self.transitions = transitions


# The share of a net's cancellation timeout after which a run that is still waiting
# for cancelled bodies says so in the log: three minutes of the default five.
_CANCELLATION_WARNING_SHARE = 0.6


class ExecutionMode(Enum):
    SEQUENTIAL = "sequential"
    CONCURRENT = "concurrent"


# --- Input commands (passive data; produced by a UI/caller, applied only by the runner) ---


@dataclass
class Extend:
    """Append a list of tokens to a place. The argument is *always* a list of tokens; a
    single token is a one-element list (`[token]`). ``place`` is a place's name, or a port
    of a subnet place by its pair, ``Port("Level 3", "Lobby")``: the net driven tells the
    change, naming the pair, and the port's subnet mints the new tokens' ids. The program
    is outside the net, and a closed net, every net not declared open, takes it at a port
    only (``ClosedNetError`` otherwise, ``ClosedNets``); a control's is inside."""

    place: PlaceKey
    tokens: list


@dataclass
class SetTokens:
    """Replace a place's tokens with the provided list. `[]` clears the place. ``place``
    is a ``PlaceKey``, and a closed net takes it at a port only, as for ``Extend``."""

    place: PlaceKey
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


@dataclass
class SetNetParam:
    """Set one of the net's own parameters (``graph.params``), which must have been
    declared at construction."""

    param: str
    value: Any


@dataclass
class Fire:
    """Fire one transition now, if it is fireable (enabled, not in flight, its inputs there,
    none of the tokens it would take or be handed claimed against it by a firing in flight,
    and its guard passing); otherwise do nothing. What a ``fire`` control's press produces.
    Fired through the engine's one-firing primitive with a selector pinned to it, outer net
    only (no descent into inner nets), counted as any firing is. The claims are those the
    concurrent loops launch by: a read edge shares its token with other readers, and a
    mutate or a consuming edge needs its token alone."""

    transition: str


Command = Union[Extend, SetTokens, SetParam, Enable, Disable, SetNetParam, Fire]


@dataclass
class RunContext:
    """Passive bundle of run-state the caller owns (data only — no behaviour, no
    hand-written ``__init__``).

    - ``graph``     — the single source of truth (mutated in place).
    - ``mode``      — selects the firing orchestrator.
    - ``observers`` — notified with the live graph after each state change, and when a
                      transition goes in flight.
    - ``inbox``     — an ``asyncio.Queue`` of input commands; drained between steps.
    - ``stop``      — an ``asyncio.Event``; set it to end ``run_indefinitely``.
    - ``strict``    — if True, a malformed command raises; otherwise it is dropped-and-logged.
                      A write a closed net refuses raises either way (``ClosedNetError``).
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

    One more field, ``_flight_state``, is the Runner's alone: not an argument, and not
    shown or compared. While a concurrent loop runs on this context it holds the loop's
    firings in flight with the claims each has on tokens, so that a ``Fire`` a drain
    applies is held to the claims the loop launches by; the loop keeps it current and
    empties it as it ends, however it ends. Otherwise it is empty.
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
    _flight_state: dict[asyncio.Future, tuple] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )


# The widgets a control may be, and what its press does (for a viewer's lighting rule).
CONTROL_KINDS = ("button", "toggle", "slider", "select", "text")
CONTROL_ACTIONS = ("inject", "fire", "enable")


def _takes_net(function: Callable) -> bool:
    """Whether ``function`` takes a second positional argument, the net as it stands."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    positional = [
        p for p in parameters
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


@dataclass
class ControlSpec:
    """A declarative binding of a UI control to a place/transition. Apps author a control-map
    ``{name: ControlSpec}`` — kept *off* the net — and the UI renders it, feeding each widget's
    value to ``to_command`` and enqueuing whatever command comes back (``None`` = nothing to do).
    A net may declare its map with ``@petri_net(controls=...)``; the protocol runner then
    serves it as ``topology.controls`` and runs it on ``press`` (PROTOCOL.md §5.1, §6).

    - ``node``       — the place/transition the control targets (for labelling / future co-location).
    - ``kind``       — which widget: ``"button" | "toggle" | "slider" | "select" | "text"``. It
                       fixes the value a press carries: a button's is ``True`` (clicked), a
                       toggle's a boolean, a slider's a number, a select's one of its options,
                       a text's a string.
    - ``label``      — display label.
    - ``to_command`` — ``value -> Command | None`` (turns the UI value into an inbox command);
                       one that takes a second argument is handed the net as it stands too,
                       ``(value, graph) -> Command | None``, for a command that depends on the
                       marking. ``ControlSpec.command`` calls it either way.
    - ``config``     — widget specifics (slider ``start``/``stop``/``step``, select ``options``,
                       text ``placeholder``, default ``value``).
    - ``action``     — what kind of thing a press does, for a viewer's lighting rule only:
                       ``"fire"`` (lit while ``node`` is fireable), ``"enable"`` (a toggle
                       showing ``node``'s switch), ``"inject"``, or ``None`` (always lit). The
                       constructors ``inject``, ``fire`` and ``enable`` set it.

    The declaration is checked as it is made (``ControlSpec.validate``, since 2026-11-28),
    with ``ValueError``.
    """

    node: str
    kind: str
    label: str
    to_command: Callable[..., Optional[Command]]
    config: dict = field(default_factory=dict)
    action: Optional[str] = None

    def __post_init__(self) -> None:
        ControlSpec.validate(self)

    @staticmethod
    def validate(spec: "ControlSpec") -> None:
        """Refuse with ``ValueError`` a declaration no widget can show (since
        2026-11-28): a ``kind`` or an ``action`` outside the vocabulary
        (``CONTROL_KINDS``, ``CONTROL_ACTIONS``), a ``node`` or a ``label`` that is no
        string, a ``to_command`` that cannot be called, a ``config`` that is no dict,
        and, in it, by kind: a toggle's ``value`` that is no boolean; a slider's
        ``start``, ``stop``, ``step`` or ``value`` that is no finite number, ``start``
        above ``stop``, a ``step`` that is not above zero, or a ``value`` outside
        ``start`` and ``stop`` where they are given; a select with no ``options``, an
        option that is no string, or a ``value`` that is none of them; a text's
        ``placeholder`` or ``value`` that is no string. What the config leaves out
        takes the widget's default, and its other keys are the runtime's own (a
        notebook's button ``kind``), unchecked. Whether ``node`` names a node of the
        net is checked when the protocol runner loads the net, as whether the config is
        plain JSON is."""
        def refuse(problem: str) -> NoReturn:
            raise ValueError(f"control {spec.label!r}: {problem}")

        if spec.kind not in CONTROL_KINDS:
            refuse(f"kind is one of {', '.join(CONTROL_KINDS)}, not {spec.kind!r}")
        if spec.action is not None and spec.action not in CONTROL_ACTIONS:
            refuse(f"action is one of {', '.join(CONTROL_ACTIONS)} or None, not {spec.action!r}")
        if not isinstance(spec.label, str):
            raise ValueError(f"a control's label is a string, not {spec.label!r}")
        if not isinstance(spec.node, str):
            refuse(f"node is the name of a place or a transition, or \"\" for none, not {spec.node!r}")
        if not callable(spec.to_command):
            refuse(f"to_command is a function, not {spec.to_command!r}")
        if not isinstance(spec.config, dict):
            refuse(f"config is a dict, not {spec.config!r}")
        config = spec.config
        if spec.kind == "toggle" and "value" in config and not isinstance(config["value"], bool):
            refuse(f"a toggle's value is True or False, not {config['value']!r}")
        elif spec.kind == "slider":
            for key in ("start", "stop", "step", "value"):
                number = config.get(key)
                if key in config and (
                    isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)
                ):
                    refuse(f"a slider's {key} is a finite number, not {number!r}")
            start, stop, step, value = (config.get(key) for key in ("start", "stop", "step", "value"))
            if start is not None and stop is not None and start > stop:
                refuse(f"a slider's start {start!r} is above its stop {stop!r}")
            if step is not None and step <= 0:
                refuse(f"a slider's step is above zero, not {step!r}")
            if value is not None and ((start is not None and value < start) or (stop is not None and value > stop)):
                refuse(f"a slider's value {value!r} is outside its range {start}..{stop}")
        elif spec.kind == "select":
            options = config.get("options")
            if not isinstance(options, (list, tuple)) or not options:
                refuse(f"a select has at least one option in config['options'], not {options!r}")
            if not all(isinstance(option, str) for option in options):
                refuse(f"a select's options are strings, not {list(options)!r}")
            if "value" in config and config["value"] not in options:
                refuse(f"a select's value {config['value']!r} is not one of its options {list(options)!r}")
        elif spec.kind == "text":
            for key in ("placeholder", "value"):
                if key in config and not isinstance(config[key], str):
                    refuse(f"a text's {key} is a string, not {config[key]!r}")

    @staticmethod
    def inject(place: str, make: Callable[..., Any], label: str, kind: str = "button",
               config: Optional[dict] = None) -> "ControlSpec":
        """A control that adds a token to ``place``: ``make(value)`` is the token, or
        ``make(value, graph)`` when it takes the net too (to number what it makes from the
        marking, say). A press gives ``Extend(place, [token])``."""
        reads_net = _takes_net(make)

        def to_command(value: Any, graph: Optional[ExecutableGraph] = None) -> Command:
            return Extend(place, [make(value, graph) if reads_net else make(value)])

        return ControlSpec(place, kind, label, to_command, dict(config or {}), action="inject")

    @staticmethod
    def fire(transition: str, label: str) -> "ControlSpec":
        """A button that fires ``transition`` once, if it is fireable (``Fire``)."""
        return ControlSpec(transition, "button", label, lambda _clicked: Fire(transition), action="fire")

    @staticmethod
    def enable(transition: str, label: str, value: bool = True) -> "ControlSpec":
        """A toggle that switches ``transition`` on (``Enable``) or off (``Disable``); ``value``
        is the toggle's initial state."""
        return ControlSpec(
            transition, "toggle", label,
            lambda on: Enable(transition) if on else Disable(transition),
            {"value": value}, action="enable",
        )

    @staticmethod
    def command(spec: "ControlSpec", value: Any, graph: Optional[ExecutableGraph] = None) -> Optional[Command]:
        """``spec.to_command`` applied to ``value``, handed ``graph`` too when it takes one."""
        if _takes_net(spec.to_command):
            return spec.to_command(value, graph)
        return spec.to_command(value)


@dataclass(frozen=True)
class PlaceStyle:
    """How a viewer draws a place, declared beside the net with
    ``@petri_net(place_styles={place: PlaceStyle(...)})`` and served in the topology
    (PROTOCOL.md §5.1). ``fill="token"``, the one fill so far: while the place holds exactly
    one token and that token has a colour, the viewer fills the place with it."""

    fill: str


def apply_control(ctx: RunContext, spec: ControlSpec, value: Any) -> Optional[Command]:
    """Turn a control's value into a command via ``ControlSpec.command`` (``spec.to_command``,
    handed the net when it takes it) and enqueue it on ``ctx.inbox`` (if any). Returns the
    command, or ``None`` if the control produced nothing. The control never touches the graph
    — only the inbox.

    A control is the net's own, so the command it gives is applied as from inside the net
    (``by_control``, since 2026-11-23): an ``Extend`` it gives lands at any place of a closed
    net, as a ``press`` of the protocol runner does. The program and a viewer press it; a body
    that holds the net, or a net it is inside, is refused with ``HeldByBodyError``, which fails
    its firing (since 2026-11-28). While a body holds the net the program is refused it too,
    with ``HeldByBodyError``, until the firing lands, and the body is not failed for it (since
    2026-11-28): it is refused the net's knobs and a load into it likewise
    (``ClosedNets.ensure_press``)."""
    _ensure_pressable(ctx.graph, f"pressing control {spec.label!r}")
    command = ControlSpec.command(spec, value, ctx.graph)
    if command is not None:
        command = by_control(command)
        if ctx.inbox is not None:
            ctx.inbox.put_nowait(command)
    return command


# The mark a control's command carries: applied as from inside the net (``by_control``).
_BY_CONTROL = "_petritype_by_control"


def by_control(command: Command) -> Command:
    """A copy of ``command`` marked as given by one of the net's own controls (since
    2026-11-23). ``_apply_command`` applies it as from inside the net, so the rule on closed
    nets (``ClosedNets``) does not refuse it, unless a body that holds the net applies it, or
    anyone applies it while a body holds the net (since 2026-11-28). ``apply_control`` and the protocol runner's ``press`` mark what a
    control gives; a command the program makes itself is outside.
    Anything that is not a command is returned as it is, for ``_apply_command`` to refuse."""
    if not dataclasses.is_dataclass(command) or isinstance(command, type):
        return command
    copy = dataclasses.replace(command)
    object.__setattr__(copy, _BY_CONTROL, True)
    return copy


# Why a body that holds a net may not press one of its controls.
_CONTROL_WHY = (
    "its controls are pressed by the program and the viewer. Give the command to the program, or put a token "
    "at a port the edge reaches for a transition of the net's own to act on."
)


def _ensure_pressable(graph: ExecutableGraph, act: str) -> None:
    """Refuse with ``HeldByBodyError`` a control of ``graph`` pressed or applied by a
    running body that holds it, or a net it is inside, and by anyone while a body holds
    it (both since 2026-11-28, ``ClosedNets.ensure_press``)."""
    ClosedNets.ensure_press(graph, act, _CONTROL_WHY)


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


def _run_now(coroutine) -> Any:
    """Run ``coroutine`` to completion from synchronous code: on a fresh event loop, in a
    helper thread when this thread already runs one (``drain_inbox`` is synchronous and is
    called from inside the Runner's own loops). The caller waits, as it waits for a
    synchronous body."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as helper:
        return helper.submit(asyncio.run, coroutine).result()


def _fire_now(ctx: RunContext, cmd: Fire) -> bool:
    """Apply a ``Fire``: if ``cmd.transition`` can fire now (not in flight, and by the rule
    the concurrent sweep launches by, ``_launchable``: switched on in ``ctx`` and on the
    graph, inputs there, no claim of a firing in flight against its own, guard passing),
    fire it once through ``fire_one_transition`` with a selector pinned to it, the plan it
    was checked with and no descent into inner nets, counted against the fuse; otherwise
    do nothing. Returns True iff it fired. A body that fails raises
    ``TransitionFailedError``, as a step's would. It passes no ``on_launch``, so observers
    hear of the firing after the drain: ``drain_inbox`` is synchronous, and ``_run_now``
    may run the firing on a helper thread's loop, where an observer would be called off
    the loop's thread.

    The claims are those of the concurrent loop running on ``ctx`` (``_flight_state``),
    and there are none when no such loop runs. The checked plan is the one taken, since
    a body on a thread may change a token it holds while the caller waits, and so what a
    keyed edge would choose if planned again. ``fire_one_transition`` names only its own
    firing in ``graph.in_flight`` and empties it as it lands, so the names there before
    are put back: the loop's firings are still in flight."""
    graph = ctx.graph
    transition = _transition_named(graph, cmd.transition)
    if transition is None:
        return _reject(ctx, f"unknown transition {cmd.transition!r}")
    if not _controls_allowed(ctx, cmd, f"firing {cmd.transition!r} by name"):
        return False
    if transition.name in graph.in_flight:
        return False
    place_nodes, incoming, outgoing, read = ExecutableGraphOperations.maps(graph)
    launchable = _launchable(ctx, transition, place_nodes, incoming, read, _holds_of(ctx._flight_state))
    if launchable is None:
        return False
    plan, _claims = launchable
    if _fuse_spent(ctx):
        _blow_fuse(ctx, transition.name)

    def pinned(_graph, fireable):
        return transition if any(t is transition for t in fireable) else None

    running = graph.in_flight
    try:
        fired = _run_now(ExecutableGraphOperations.fire_one_transition(
            executable_graph=graph,
            place_names_to_nodes=place_nodes,
            transition_names_to_incoming_edges=incoming,
            transition_names_to_outgoing_edges=outgoing,
            transition_names_to_read_edges=read,
            transition_selector=pinned,
            executor=ctx.thread_pool,
            descend=False,
            plans={transition.name: plan},
        ))
    finally:
        graph.in_flight = running
    if fired is None:
        return False
    ctx.n_fired += 1
    return True


def _refuse(ctx: RunContext, refusal: ValueError) -> bool:
    """A knob or a firing by name refused because a body holds the net now
    (``HeldByBodyError``): raised as it is under ``strict``, otherwise dropped and
    logged, as any command the net cannot take at that moment is. A write a closed
    net refuses is not dropped (``_tokens_allowed``)."""
    if ctx.strict:
        raise refusal
    _log.warning("dropping refused command: %s", refusal)
    return False


def _tokens_allowed(ctx: RunContext, cmd: Command, act: str) -> bool:
    """True when ``cmd`` may write its place: it is inside (``by_control``) or the net
    lets it (``ClosedNets.ensure_tokens``). A closed net's refusal is raised as it is,
    ``ClosedNetError``, in every context, strict or not: the place is not a port, which
    is a matter of the net's definition and not of the input, so dropping the command
    would leave a program waiting for a token that never arrives. A control's command
    applied by a body that holds the net is refused through ``_refuse``
    (``_control_allowed``)."""
    if getattr(cmd, _BY_CONTROL, False):
        return _control_allowed(ctx, act)
    ClosedNets.ensure_tokens(ctx.graph, cmd.place, act)
    return True


def _controls_allowed(ctx: RunContext, cmd: Command, act: str) -> bool:
    """True when ``cmd`` may work a knob or fire by name: it is inside
    (``by_control``) and no body that holds the net applies it, or no body holds the
    net (``ClosedNets.ensure_controls``); otherwise refused through ``_refuse``."""
    if getattr(cmd, _BY_CONTROL, False):
        return _control_allowed(ctx, act)
    try:
        ClosedNets.ensure_controls(ctx.graph, act)
    except HeldByBodyError as refusal:
        return _refuse(ctx, refusal)
    return True


def _control_allowed(ctx: RunContext, act: str) -> bool:
    """True when a control's command (``by_control``) may be applied: unless a body
    that holds the net, or a net it is inside, applies it, or anyone applies it while
    a body holds the net (both since 2026-11-28), which is refused through ``_refuse``.
    The program's and a viewer's land while no body holds the net."""
    try:
        _ensure_pressable(ctx.graph, f"{act} by a control")
    except HeldByBodyError as refusal:
        return _refuse(ctx, refusal)
    return True


def _apply_command(ctx: RunContext, cmd: Command) -> bool:
    """Apply one input command to the live graph. Returns True if applied.

    Token ids and events: ``Extend`` gives each new token a fresh id (``Added``);
    ``SetTokens`` retires every old id (``Removed``) and gives every new token a
    fresh one (``Added``); ``SetParam`` records one ``ParamChanged`` per keyword,
    ``SetNetParam`` one with no transition; ``Enable``/``Disable`` record
    ``Toggled`` only when the switch changed; ``Fire`` records the firing's events
    when it fires (``_fire_now``) and nothing when it does not, which is not a
    rejection (False, nothing logged).

    Closed nets (``ClosedNets``): a command the program makes is outside the net, so
    ``Extend`` and ``SetTokens`` at a place of a closed net (every net not declared
    open) that is not one of its ports are refused with ``ClosedNetError``, raised in
    every context, strict or not; and while a body of the net around it holds the
    net, the knobs and ``Fire`` with ``HeldByBodyError``: raised under ``strict``,
    else dropped and logged, as any command the net cannot take at that moment. A command a control of the net gives
    (``by_control``) is inside, and the rule does not refuse it, unless a body that holds the net applies it,
    or anyone applies it while a body holds the net (``HeldByBodyError``, both since 2026-11-28, raised or
    dropped likewise)."""
    graph = ctx.graph
    TokenIds.resync(graph)

    if isinstance(cmd, Fire):
        return _fire_now(ctx, cmd)

    if isinstance(cmd, (Extend, SetTokens)):
        place = graph.place_named(cmd.place)
        if place is None:
            return _reject(ctx, f"unknown place {cmd.place!r}")
        if isinstance(place, SubnetPlaceNode):
            return _reject(
                ctx, f"{cmd.place!r} is a subnet place, which holds no tokens of its own; name one of its ports",
            )
        if not isinstance(cmd.tokens, list):
            return _reject(
                ctx, f"{type(cmd).__name__}.tokens must be a list, got {type(cmd.tokens).__name__}"
            )
        for token in cmd.tokens:
            if not CompareTypes.between_value_and_type(token, place.type):
                return _reject(
                    ctx, f"token {token!r} does not match place {place.name!r} type {place.type!r}"
                )
            if place.nested:
                try:
                    NestedNets.ensure_nested_token(token, place)
                except TypeError as error:
                    return _reject(ctx, str(error))
        if not _tokens_allowed(ctx, cmd, f"{'an Extend' if isinstance(cmd, Extend) else 'a SetTokens'}"):
            return False
        # Told by this net, under the place as it names it (a port by its pair);
        # a port's new ids are its subnet's.
        if isinstance(cmd, SetTokens):
            for token_id in place.token_ids:
                GraphEvents.record(graph, Removed(cmd.place, token_id))
            place.tokens = []
            place._token_ids = []
        for token_id in TokenIds.append(TokenIds.minter(graph, cmd.place), place, list(cmd.tokens)):
            GraphEvents.record(graph, Added(cmd.place, token_id))
        return True

    if isinstance(cmd, SetParam):
        transition = _transition_named(graph, cmd.transition)
        if transition is None:
            return _reject(ctx, f"unknown transition {cmd.transition!r}")
        if not isinstance(cmd.kwargs, dict):
            return _reject(ctx, f"SetParam.kwargs must be a dict, got {type(cmd.kwargs).__name__}")
        if "" in cmd.kwargs:
            return _reject(ctx, "SetParam names a keyword by the empty string; a name holds at least one character")
        if not _controls_allowed(ctx, cmd, f"setting the keywords of {cmd.transition!r}"):
            return False
        transition.kwargs = {**(transition.kwargs or {}), **cmd.kwargs}
        for param in cmd.kwargs:
            GraphEvents.record(graph, ParamChanged(cmd.transition, param))
        return True

    if isinstance(cmd, SetNetParam):
        if not isinstance(cmd.param, str) or cmd.param not in graph.params:
            return _reject(ctx, f"the net declares no parameter {cmd.param!r}")
        if not _controls_allowed(ctx, cmd, f"setting parameter {cmd.param!r}"):
            return False
        graph.params[cmd.param] = cmd.value
        GraphEvents.record(graph, ParamChanged(None, cmd.param))
        return True

    if isinstance(cmd, (Enable, Disable)):
        if _transition_named(graph, cmd.transition) is None:
            return _reject(ctx, f"unknown transition {cmd.transition!r}")
        enabled = isinstance(cmd, Enable)
        if not _controls_allowed(ctx, cmd, f"switching {cmd.transition!r} {'on' if enabled else 'off'}"):
            return False
        was_enabled = cmd.transition not in ctx.disabled
        if enabled:
            ctx.disabled.discard(cmd.transition)
        else:
            ctx.disabled.add(cmd.transition)
        if enabled != was_enabled:
            GraphEvents.record(graph, Toggled(cmd.transition, enabled))
        return True

    return _reject(ctx, f"unrecognised command {cmd!r}")


def _selector_for(ctx: RunContext):
    """A transition selector that drops disabled transitions from the candidates and lets
    the graph's own policy choose among the rest, or None (engine default) when nothing is
    disabled. Recomputed each step since ``disabled`` may change between drains.

    Disabling only narrows *which* transitions may fire; it must not change *how* one is
    chosen. So the survivors go to the graph's ``transition_selector`` if set, else
    ``default_transition_selector`` (priority, then ``tie_break``) — the same resolution
    ``fire_one_transition`` uses. The previous version returned the first non-disabled
    transition, silently discarding priority, a custom selector and ``tie_break`` for as
    long as anything was disabled."""
    if not ctx.disabled:
        return None
    _ensure_switches_allowed(ctx)
    disabled = ctx.disabled

    def selector(graph, fireable):
        remaining = [t for t in fireable if t.name not in disabled]
        if not remaining:
            return None
        return (graph.transition_selector or default_transition_selector)(graph, remaining)

    return selector


def _ensure_switches_allowed(ctx: RunContext) -> None:
    """Refuse with ``HeldByBodyError`` a run of a net a body holds, or a net inside
    one, while ``ctx.disabled`` switches transitions off (since 2026-11-28): switched
    off in a context, transitions choose which of the rest fires, which is firing
    by name, and a transition's switch is a knob, so both stay with the program and
    the runner while no body holds the net. Both drivers refuse it alike, before
    anything fires: the sequential one used to refuse it as a selector of the
    caller's own, and the concurrent one let the switches choose."""
    if ctx.disabled:
        ClosedNets.ensure_controls(ctx.graph, "a run with transitions its RunContext switches off")


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
    a firing would be attempted (some non-disabled transition is fireable, here or in
    an inner net a deep step enters). Costs one int compare until the fuse is spent.
    It blames what the deep step would try first: an inner net ahead of the outer
    net's own turn, then the outer net's first fireable transition, then an inner net
    behind it. A custom selector that would have vetoed every fireable transition is
    not consulted: a fireable net with a spent fuse counts as attempting. (The
    concurrent loops don't need this sweep: they check the fuse right before each
    launch and each inner turn, where fireability is already established.)"""
    if not _fuse_spent(ctx):
        return
    graph = ctx.graph
    nested = NestedNets.any_nested(graph)
    if nested and NestedNets.any_fireable(graph, "ahead"):
        _blow_fuse(ctx, "an inner net")
    for transition in graph.transitions:
        if transition.name in ctx.disabled:
            continue
        if ExecutableGraphCheck.transition_is_fireable(
            transition=transition,
            executable_graph=graph,
            transition_names_to_incoming_edges=incoming,
            place_names_to_nodes=place_nodes,
            transition_names_to_read_edges=read,
        ):
            _blow_fuse(ctx, transition.name)
    if nested and NestedNets.any_fireable(graph, "behind"):
        _blow_fuse(ctx, "an inner net")


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
        on_launch=lambda: _notify(ctx),
    )
    if fired_name is None:
        return False
    ctx.n_fired += 1
    return True


@dataclass(frozen=True)
class _Claims:
    """Per-token and whole-place reader/writer claims for one concurrent firing.

    ``need_*`` are checked atomically against all currently active ``hold_*``
    claims before launch (preventing concurrent data races and preserving strict
    FIFO head-of-line waiting without lock deadlocks). ``hold_*`` stay active
    while the body runs: consumed tokens are removed synchronously at launch and
    snapshots deep-copy synchronously at launch, so only live ``MutateEdge``
    (exclusive) and ``ReadEdge`` (shared) claims remain held in flight.
    """

    need_exclusive_tokens: frozenset[tuple[PlaceKey, int]] = frozenset()
    need_shared_tokens: frozenset[tuple[PlaceKey, int]] = frozenset()
    need_exclusive_places: frozenset[PlaceKey] = frozenset()
    need_shared_places: frozenset[PlaceKey] = frozenset()
    hold_exclusive_tokens: frozenset[tuple[PlaceKey, int]] = frozenset()
    hold_shared_tokens: frozenset[tuple[PlaceKey, int]] = frozenset()
    hold_exclusive_places: frozenset[PlaceKey] = frozenset()
    hold_shared_places: frozenset[PlaceKey] = frozenset()


def _claims_from_plan(plan: Stage1Plan) -> _Claims:
    """Derive reader/writer claims from a validated ``Stage1Plan``."""
    need_ex_tok: set[tuple[PlaceKey, int]] = set()
    need_sh_tok: set[tuple[PlaceKey, int]] = set()
    need_ex_place: set[PlaceKey] = set()
    need_sh_place: set[PlaceKey] = set()
    hold_ex_tok: set[tuple[PlaceKey, int]] = set()
    hold_sh_tok: set[tuple[PlaceKey, int]] = set()
    hold_ex_place: set[PlaceKey] = set()
    hold_sh_place: set[PlaceKey] = set()

    for item in plan.incoming:
        key = item.edge.place_node_name
        if item.takes_every:
            need_ex_place.add(key)
        else:
            idx = item.indices[0]
            if idx < len(item.place._token_ids):
                need_ex_tok.add((key, item.place._token_ids[idx]))
            else:
                need_ex_place.add(key)

    for item in plan.reads:
        edge = item.edge
        key = edge.place_node_name
        if isinstance(edge, MutateEdge):
            if item.takes_every:
                need_ex_place.add(key)
                hold_ex_place.add(key)
            else:
                idx = item.indices[0]
                if idx < len(item.place._token_ids):
                    pair = (key, item.place._token_ids[idx])
                    need_ex_tok.add(pair)
                    hold_ex_tok.add(pair)
                else:
                    need_ex_place.add(key)
                    hold_ex_place.add(key)
        elif isinstance(edge, ReadEdge):
            if isinstance(edge, ReadAllEdge) or item.takes_every:
                need_sh_place.add(key)
                hold_sh_place.add(key)
            else:
                idx = item.indices[0]
                if idx < len(item.place._token_ids):
                    pair = (key, item.place._token_ids[idx])
                    need_sh_tok.add(pair)
                    hold_sh_tok.add(pair)
                else:
                    need_sh_place.add(key)
                    hold_sh_place.add(key)
        elif isinstance(edge, SnapshotEdge):
            if item.takes_every:
                need_sh_place.add(key)
            else:
                idx = item.indices[0]
                if idx < len(item.place._token_ids):
                    need_sh_tok.add((key, item.place._token_ids[idx]))
                else:
                    need_sh_place.add(key)

    return _Claims(
        need_exclusive_tokens=frozenset(need_ex_tok),
        need_shared_tokens=frozenset(need_sh_tok),
        need_exclusive_places=frozenset(need_ex_place),
        need_shared_places=frozenset(need_sh_place),
        hold_exclusive_tokens=frozenset(hold_ex_tok),
        hold_shared_tokens=frozenset(hold_sh_tok),
        hold_exclusive_places=frozenset(hold_ex_place),
        hold_shared_places=frozenset(hold_sh_place),
    )


@dataclass(slots=True)
class _Holds:
    """The claims the firings in flight hold while their bodies run, gathered into sets
    to look a claim up in: tokens and whole places, exclusive and shared, and the places
    of the held tokens. Gathered once for a sweep or a ``Fire`` (``_holds_of``), and
    added to as a sweep launches (``_add_holds``)."""

    exclusive_tokens: set[tuple[PlaceKey, int]] = field(default_factory=set)
    shared_tokens: set[tuple[PlaceKey, int]] = field(default_factory=set)
    exclusive_places: set[PlaceKey] = field(default_factory=set)
    shared_places: set[PlaceKey] = field(default_factory=set)
    exclusive_token_places: set[PlaceKey] = field(default_factory=set)
    shared_token_places: set[PlaceKey] = field(default_factory=set)


def _holds_anything(claims: _Claims) -> bool:
    """Whether a firing launched with ``claims`` holds a token or a place while its body
    runs, through a mutate or a read edge."""
    return bool(
        claims.hold_exclusive_tokens or claims.hold_shared_tokens
        or claims.hold_exclusive_places or claims.hold_shared_places
    )


def _add_holds(holds: _Holds, claims: _Claims) -> None:
    """Add to ``holds`` what a firing launched with ``claims`` holds while its body runs."""
    if not _holds_anything(claims):
        return
    holds.exclusive_tokens.update(claims.hold_exclusive_tokens)
    holds.shared_tokens.update(claims.hold_shared_tokens)
    holds.exclusive_places.update(claims.hold_exclusive_places)
    holds.shared_places.update(claims.hold_shared_places)
    holds.exclusive_token_places.update(place for place, _ in claims.hold_exclusive_tokens)
    holds.shared_token_places.update(place for place, _ in claims.hold_shared_tokens)


def _holds_of(flight_state: dict) -> _Holds:
    """What the firings in ``flight_state`` hold."""
    holds = _Holds()
    for _transition, _args, _firing_ids, claims in flight_state.values():
        _add_holds(holds, claims)
    return holds


def _held_places(claims: _Claims) -> set[PlaceKey]:
    """The places where a firing launched with ``claims`` holds a token or the whole place."""
    return {
        *claims.hold_exclusive_places,
        *claims.hold_shared_places,
        *(place for place, _ in claims.hold_exclusive_tokens),
        *(place for place, _ in claims.hold_shared_tokens),
    }


def _conflicts(claims: _Claims, holds: _Holds) -> bool:
    """True iff ``claims`` conflicts with a claim held in flight (``holds``): it needs
    alone a token or a place that a firing holds, or shares one that a firing holds
    alone. A claim on a whole place meets every claim on one of its tokens."""
    if not (holds.exclusive_tokens or holds.shared_tokens or holds.exclusive_places or holds.shared_places):
        return False
    for place in claims.need_exclusive_places:
        if (
            place in holds.exclusive_places
            or place in holds.shared_places
            or place in holds.exclusive_token_places
            or place in holds.shared_token_places
        ):
            return True
    for place in claims.need_shared_places:
        if place in holds.exclusive_places or place in holds.exclusive_token_places:
            return True
    for pair in claims.need_exclusive_tokens:
        place = pair[0]
        if (
            pair in holds.exclusive_tokens
            or pair in holds.shared_tokens
            or place in holds.exclusive_places
            or place in holds.shared_places
        ):
            return True
    for pair in claims.need_shared_tokens:
        if pair in holds.exclusive_tokens or pair[0] in holds.exclusive_places:
            return True
    return False


def _launchable(
    ctx: RunContext,
    transition: FunctionTransitionNode,
    place_nodes: dict,
    incoming: dict,
    read: dict,
    holds: _Holds,
    planned: Optional[dict[str, Optional[tuple[Stage1Plan, _Claims]]]] = None,
) -> Optional[tuple[Stage1Plan, _Claims]]:
    """The plan and the claims of a firing of ``transition`` now, beside the firings in
    flight whose claims ``holds`` gathers, or None when it cannot fire: switched off in
    ``ctx`` or on the graph, its tokens not there, one of its claims against a claim
    held in flight (``_conflicts``), or its guard failing, asked in that order. The one
    rule both the concurrent sweep and a ``Fire`` command launch by; whether the
    transition is in flight already is the caller's to check.

    ``planned`` is a sweep's: the plan and claims of each transition planned on the
    marking as it stands, or None for one whose tokens are not there. A transition
    found there is not planned again, and one planned is put there; the sweep takes
    out those a launch may have changed. The guard is asked every time."""
    graph = ctx.graph
    if transition.name in ctx.disabled or transition.name in graph.disabled:
        return None
    if planned is not None and transition.name in planned:
        found = planned[transition.name]
    else:
        plan = ExecutableGraphOperations.plan_stage_1(
            transition=transition,
            transition_names_to_incoming_edges=incoming,
            place_names_to_nodes=place_nodes,
            transition_names_to_read_edges=read,
            net_params=graph.params,
            check_filters=True,
        )
        found = None if plan is None else (plan, _claims_from_plan(plan))
        if planned is not None:
            planned[transition.name] = found
    if found is None or _conflicts(found[1], holds):
        return None
    if transition.guard is not None and not bool(Questions.ask("guard", transition.name, transition.guard, graph)):
        return None
    return found


def _exposure(
    transitions: Sequence[FunctionTransitionNode], place_nodes: dict, incoming: dict, read: dict,
) -> tuple[dict[int, list[int]], list[int]]:
    """For a sweep: by place node (its ``id``, so a node two keys name is one), the
    positions in ``transitions`` of those with an input edge on it, of any kind; and
    the positions of those with a guard."""
    on_place: dict[int, list[int]] = {}
    guarded: list[int] = []
    for position, transition in enumerate(transitions):
        for edge in (*incoming.get(transition.name, ()), *read.get(transition.name, ())):
            node = place_nodes.get(edge.place_node_name)
            if node is not None:
                on_place.setdefault(id(node), []).append(position)
        if transition.guard is not None:
            guarded.append(position)
    return on_place, guarded


def _keep_candidate(
    positions: list[int],
    candidates: list[FunctionTransitionNode],
    launchable: dict[str, tuple[int, Stage1Plan, _Claims]],
    position: int,
    transition: FunctionTransitionNode,
    found: Optional[tuple[Stage1Plan, _Claims]],
) -> None:
    """Put ``transition``, at ``position`` in declaration order, among a sweep's
    candidates with what ``_launchable`` found, or take it out when that is None."""
    at = bisect_left(positions, position)
    present = at < len(positions) and positions[at] == position
    if found is None:
        if present:
            del positions[at]
            del candidates[at]
            del launchable[transition.name]
        return
    if not present:
        positions.insert(at, position)
        candidates.insert(at, transition)
    launchable[transition.name] = (position, *found)


def _offered(
    chosen: Any,
    offered: list[FunctionTransitionNode],
    positions: list[int],
    launchable: dict[str, tuple[int, Stage1Plan, _Claims]],
) -> bool:
    """``chosen in offered``, looked for first where it was offered, which spares a
    comparison of every transition before it."""
    entry = launchable.get(chosen.name) if isinstance(chosen, FunctionTransitionNode) else None
    if entry is not None:
        at = bisect_left(positions, entry[0])
        if at < len(offered) and offered[at] is chosen:
            return True
    return chosen in offered


def _launch(ctx: RunContext, transition: FunctionTransitionNode, place_nodes: dict, incoming: dict,
            outgoing: dict, read: dict, *, plan: Optional[Stage1Plan] = None) -> tuple[asyncio.Future, FiringTokenIds, dict]:
    """Concurrent-mode launch: consume the inputs synchronously (no await, so peers
    can't grab the same tokens), then start the body as a background task. The task
    resolves to ``(outputs, duration_ms)``, timed around the body itself rather than
    around the loop's wait for it. The nets the body is handed are held until it
    returns, and what the rule on closed nets refused meanwhile fails the task
    (``ClosedNets``, since 2026-11-23)."""
    firing_ids = FiringTokenIds()
    with ClosedNets.moving(ctx.graph):
        args, _ = ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
            transition=transition,
            transition_names_to_incoming_edges=incoming,
            place_names_to_nodes=place_nodes,
            transition_names_to_read_edges=read,
            firing_ids=firing_ids,
            # The parameters a keyed edge's clause may name, as the fireability check read them.
            net_params=ctx.graph.params,
            plan=plan,
            record_input_places=False,
            executable_graph=ctx.graph,
        )

    async def timed_body():
        try:
            started = time.perf_counter()
            outputs = await ExecutableGraphOperations.stage_2_call_transition_function(
                transition=transition,
                tokens_kwargs=args,
                transition_names_to_outgoing_edges=outgoing,
                place_names_to_nodes=place_nodes,
                allow_token_copying=ctx.graph.allow_token_copying,
                executor=ctx.thread_pool,
                net_params=ctx.graph.params,
                firing_ids=firing_ids,
            )
            ClosedNets.settle(firing_ids)
            return outputs, (time.perf_counter() - started) * 1000.0
        finally:
            ClosedNets.release(firing_ids)

    return asyncio.ensure_future(timed_body()), firing_ids, args


def _launch_sweep(
    ctx: RunContext,
    place_nodes: dict,
    incoming: dict,
    outgoing: dict,
    read: dict,
    in_flight: dict[asyncio.Future, str],
    flight_state: dict[asyncio.Future, tuple],
    remaining_cap: Optional[int] = None,
) -> int:
    """Launch as many non-conflicting transitions as possible in one concurrent
    sweep, honouring ``priority``, ``tie_break``, ``transition_selector``,
    ``disabled``, guards, and per-token reader/writer claims.

    Each time round, the transitions that can launch (not in flight, and by
    ``_launchable``: switched on, tokens there, no claim against one held in flight,
    guard passing) are handed to the graph's selector in declaration order, and the one
    it picks is launched, after the fuse is checked, until none can or
    ``remaining_cap`` is reached.

    What a launch changes is all that is looked at again. The claims held in flight
    are gathered once, and each launch adds its own. A plan is kept for the sweep and
    made again only after a launch took from a place the transition has an input edge
    on, of any kind: a plan reads only those places, and a launch moves tokens only out
    of the places it takes from. A transition with an input edge on a place where a
    launch holds a token, or the whole place, has its claims checked again, and every
    guard is asked again each time round, since a guard may read the whole net. The
    rest can launch, or not, as before, so the selector is handed the lists it would be
    handed if every transition were planned again, and a time round costs the
    selector, the guards and what the last launch touched."""
    graph = ctx.graph
    _ensure_switches_allowed(ctx)
    transitions = graph.transitions
    airborne = set(in_flight.values())
    selector = graph.transition_selector or default_transition_selector
    holds = _holds_of(flight_state)
    planned: dict[str, Optional[tuple[Stage1Plan, _Claims]]] = {}
    # The transitions that can launch, in declaration order, with their positions in it;
    # and by name each one's position, plan and claims.
    positions: list[int] = []
    candidates: list[FunctionTransitionNode] = []
    launchable: dict[str, tuple[int, Stage1Plan, _Claims]] = {}
    exposure: Optional[tuple[dict[int, list[int]], list[int]]] = None
    last: Optional[tuple[Stage1Plan, _Claims]] = None
    launched = 0

    while remaining_cap is None or launched < remaining_cap:
        if last is None:
            for position, transition in enumerate(transitions):
                if transition.name in airborne:
                    continue
                found = _launchable(ctx, transition, place_nodes, incoming, read, holds, planned)
                if found is not None:
                    positions.append(position)
                    candidates.append(transition)
                    launchable[transition.name] = (position, *found)
        elif len(airborne) == len(transitions):
            break  # every transition is in flight: none can launch
        else:
            if exposure is None:
                exposure = _exposure(transitions, place_nodes, incoming, read)
            on_place, guarded = exposure
            last_plan, last_claims = last
            again: set[int] = set()
            for node in {id(item.place) for item in last_plan.incoming}:
                for position in on_place.get(node, ()):
                    again.add(position)
                    planned.pop(transitions[position].name, None)
            if _holds_anything(last_claims):
                for key in _held_places(last_claims):
                    node = place_nodes.get(key)
                    if node is not None:
                        again.update(on_place.get(id(node), ()))
            again.update(guarded)
            for position in sorted(again):
                transition = transitions[position]
                if transition.name in airborne:
                    continue
                found = _launchable(ctx, transition, place_nodes, incoming, read, holds, planned)
                _keep_candidate(positions, candidates, launchable, position, transition, found)

        if not candidates:
            break
        offered = list(candidates)
        chosen = Questions.ask("selector", None, selector, graph, offered)
        if chosen is None:
            break
        if not _offered(chosen, offered, positions, launchable):
            raise ValueError(
                f"transition_selector returned {chosen!r}, which is not among the launchable transitions "
                f"{[t.name for t in offered]}"
            )
        if _fuse_spent(ctx):
            _blow_fuse(ctx, chosen.name)
        position, plan, claims = launchable[chosen.name]
        task, firing_ids, args = _launch(
            ctx, chosen, place_nodes, incoming, outgoing, read, plan=plan,
        )
        in_flight[task] = chosen.name
        flight_state[task] = (chosen, args, firing_ids, claims)
        airborne.add(chosen.name)
        launched += 1
        ctx.n_fired += 1
        _keep_candidate(positions, candidates, launchable, position, transitions[position], None)
        _add_holds(holds, claims)
        last = (plan, claims)

    if launched:
        NestedNets.took_own_turn(graph)
    return launched


def _complete(ctx: RunContext, task: asyncio.Future, transition: FunctionTransitionNode, consumed: dict,
              firing_ids: FiringTokenIds, place_nodes: dict, incoming: dict) -> None:
    """Concurrent-mode completion of one finished task: deposit its outputs and
    record its events, or raise ``TransitionFailedError`` (restoring the inputs, ids
    included, when ``graph.restore_tokens_on_failure`` is set)."""
    graph = ctx.graph
    with ClosedNets.moving(graph):
        _land(graph, task, transition, consumed, firing_ids, place_nodes, incoming)
    name = transition.name
    graph.step_count += 1
    graph.fired_counts[name] = graph.fired_counts.get(name, 0) + 1
    # Convenience only: names *one* completion of this batch. Observers
    # that need every firing should diff fired_counts via fired_since.
    graph.last_fired = name


def _land(graph: ExecutableGraph, task: asyncio.Future, transition: FunctionTransitionNode, consumed: dict,
          firing_ids: FiringTokenIds, place_nodes: dict, incoming: dict) -> None:
    """The deposit, or the failure, of a finished concurrent firing (``_complete``)."""
    try:
        outputs, duration_ms = task.result()
    except Exception as exception:
        ExecutableGraphOperations.handle_failed_firing(
            transition=transition,
            exception=exception,
            input_edge_names_to_tokens=consumed,
            transition_names_to_incoming_edges=incoming,
            place_names_to_nodes=place_nodes,
            restore_tokens_on_failure=graph.restore_tokens_on_failure,
            executable_graph=graph,
            firing_ids=firing_ids,
        )
    ExecutableGraphOperations.deposit_and_record(
        executable_graph=graph,
        transition=transition,
        output_place_names_to_tokens=outputs,
        place_names_to_nodes=place_nodes,
        firing_ids=firing_ids,
        duration_ms=duration_ms,
        allow_token_copying=graph.allow_token_copying,
    )


def _release_all(flight_state: dict) -> None:
    """The holds of every firing still in flight, released as a concurrent loop
    ends, so that no net is left held by a body that will never be awaited."""
    for entry in flight_state.values():
        ClosedNets.release(entry[2])


def _retrieve(task: asyncio.Future) -> None:
    """Take a finished task's exception, if it has one, so that asyncio does not
    report it as never retrieved: the run that cancelled the task has no use for it."""
    if not task.cancelled():
        task.exception()


async def _wait_for_cancelled(tasks: list, names: dict, timeout: Optional[float]) -> tuple[str, ...]:
    """Wait for cancelled ``tasks`` to finish, for ``timeout`` seconds at most (None:
    without limit), logging a warning that names those still running once
    ``_CANCELLATION_WARNING_SHARE`` of it has passed. Returns the names (from
    ``names``, by task) of those that had not finished, in the order launched."""
    waiting = set(tasks)
    if timeout is None:
        _, waiting = await asyncio.wait(waiting)
    else:
        warn_after = timeout * _CANCELLATION_WARNING_SHARE
        _, waiting = await asyncio.wait(waiting, timeout=warn_after)
        if waiting:
            _log.warning(
                "petritype: %d cancelled bod%s still running after %.6g s (%s); "
                "the run stops waiting at %.6g s (cancellation_timeout_seconds)",
                len(waiting), "y is" if len(waiting) == 1 else "ies are", warn_after,
                ", ".join(names[task] for task in tasks if task in waiting), timeout,
            )
            _, waiting = await asyncio.wait(waiting, timeout=timeout - warn_after)
    for task in tasks:
        if task in waiting:
            task.add_done_callback(_retrieve)
        else:
            _retrieve(task)
    return tuple(names[task] for task in tasks if task in waiting)


def _lands_first(ending_with: Optional[BaseException]) -> bool:
    """Whether a concurrent run ending with ``ending_with`` lands its firings in flight
    before the error goes on: for every error (``Exception``), since the firings in
    flight did nothing wrong. A failed firing (``TransitionFailedError``) lets its peers
    land since 2026-11-28, as an error that is no firing's failure, a guard, a priority
    or a selector that raised, the fuse, a refused command or an observer that raised,
    already did. A cancellation from outside cancels them, and so does a stop, which
    ends with no error."""
    return isinstance(ending_with, Exception)


async def _land_in_flight(
    ctx: RunContext, in_flight: dict, flight_state: dict, place_nodes: dict, incoming: dict,
    ending_with: BaseException,
) -> None:
    """Let every body in flight finish, for the net's ``cancellation_timeout_seconds``
    at most, and land each that did, in the order launched: its outputs deposited
    and its events recorded, as a landing in the loop does, those that had finished
    beside the firing whose failure ends the run included. A landing that fails
    fails as in the loop (``restore_tokens_on_failure`` applies, ``Failed`` is
    recorded) and is told in a note on ``ending_with``, the error the run ends
    with, which stays the one raised. Bodies still running at the timeout stay in
    ``in_flight``, for ``_cancel_and_cleanup``, and a note names them."""
    if not in_flight:
        return
    graph = ctx.graph
    timeout = graph.cancellation_timeout_seconds
    done, _pending = await asyncio.wait(set(in_flight), timeout=timeout)
    for task in [task for task in in_flight if task in done]:
        name = in_flight.pop(task)
        fired_transition, consumed, firing_ids, _claims = flight_state.pop(task)
        try:
            _complete(ctx, task, fired_transition, consumed, firing_ids, place_nodes, incoming)
        except Exception as failed:
            ending_with.add_note(f"petritype: {name!r}, in flight as the run ended, failed as it landed: {failed}")
    if in_flight:
        ending_with.add_note(
            f"petritype: still running after {timeout:g} s (cancellation_timeout_seconds), so cancelled, not "
            f"landed: {', '.join(in_flight.values())}. What they consumed is in no place."
        )
    graph.in_flight = set(in_flight.values())


async def _end_concurrent_run(
    ctx: RunContext, in_flight: dict, flight_state: dict, place_nodes: dict, incoming: dict,
    ending_with: Optional[BaseException],
) -> None:
    """How a concurrent loop ends, from its ``finally``, ``ending_with`` the error it
    ends with, if any: the firings in flight land first when it ends with an error,
    a failed firing's included (``_lands_first``), and whatever is still in flight
    is cancelled and let go of (``_cancel_and_cleanup``)."""
    try:
        if _lands_first(ending_with):
            try:
                await _land_in_flight(ctx, in_flight, flight_state, place_nodes, incoming, ending_with)
            except BaseException as interrupted:  # cancelled while it waited
                await _cancel_and_cleanup(in_flight, flight_state, ctx.graph, interrupted)
                raise
        await _cancel_and_cleanup(in_flight, flight_state, ctx.graph, ending_with)
    finally:
        ctx._flight_state = {}


async def _cancel_and_cleanup(
    in_flight: dict, flight_state: dict, graph: ExecutableGraph, ending_with: Optional[BaseException] = None,
) -> None:
    """Cancel all in-flight tasks, await cancellation of non-thread tasks so their
    cleanup blocks run before holds are released, release any remaining holds, and
    clear ``graph.in_flight``.

    The wait is for the net's ``cancellation_timeout_seconds`` at most. Bodies that
    have not stopped by then are let go of, and named: in a note on ``ending_with``,
    the error the run is ending with, or, when it is ending with none, in a
    ``CancellationTimeoutError`` raised here."""
    pending = list(in_flight)
    for task in pending:
        task.cancel()
    awaitable = [
        task for task in pending
        if task in flight_state and flight_state[task][0].execution != "thread"
    ]
    stuck: tuple[str, ...] = ()
    try:
        if awaitable:
            stuck = await _wait_for_cancelled(awaitable, in_flight, graph.cancellation_timeout_seconds)
    finally:
        _release_all(flight_state)
        graph.in_flight = set()
    if stuck:
        message = (
            f"cancelled but still running after {graph.cancellation_timeout_seconds:g} s "
            f"(cancellation_timeout_seconds): {', '.join(stuck)}. The run has let go of "
            f"them: what they consumed is in no place and what they return goes nowhere."
        )
        if ending_with is None:
            raise CancellationTimeoutError(message, stuck)
        ending_with.add_note(f"petritype: {message}")


def _held(flight_state: dict, place_nodes: dict) -> frozenset:
    """The ``(place, token id)`` pairs the outer bodies in flight hold through a
    mutate edge or a read edge (since 2026-11-23): tokens still in their places,
    whose inner nets no inner firing may touch while such a body runs. A body handed
    every token of a place to change (a mutate edge in list mode, which claims the
    whole place) holds the place's own list, so it also holds whatever lands there
    while it runs: every token now in such a place is among the pairs."""
    pairs = {
        pair for entry in flight_state.values()
        for pair in (*entry[2].mutated, *entry[2].read)
    }
    for entry in flight_state.values():
        for place in entry[3].hold_exclusive_places:
            pairs.update((place, token_id) for token_id in place_nodes[place].token_ids)
    return frozenset(pairs)


async def _fire_inner(ctx: RunContext, part: str, held: frozenset = frozenset()) -> bool:
    """Concurrent-mode turn for the inner nets of nested tokens: one inner firing,
    inline, among the turns of one part of the outer net's round
    (``NestedNets.take_turn``): ``"ahead"`` of its launch sweep, or ``"behind"`` it,
    where the outer-first places wait until nothing outer is in flight. The tokens
    in ``held`` are skipped, and so is the subnet of a subnet place while a body in
    flight (``graph.in_flight``) has an edge on one of its ports. The fuse is checked
    first, against what that part could fire. Counted on the graph as the sequential
    path counts an inner firing, and tallied. Returns True iff something fired."""
    graph = ctx.graph
    if not NestedNets.any_nested(graph):
        return False
    if _fuse_spent(ctx) and NestedNets.any_fireable(graph, part, held):
        _blow_fuse(ctx, "an inner net")
    fired = await NestedNets.take_turn(graph, part, executor=ctx.thread_pool, held=held)
    if fired is None:
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

    @staticmethod
    def drain_inbox(ctx: RunContext) -> int:
        """Apply all currently-queued input commands, in FIFO order. Synchronous (no await),
        so it is safe to call between fires without interleaving a torn marking. Returns the
        number of commands applied. A ``Fire`` fires its transition here, if it is fireable,
        and is waited for (``_fire_now``); one that finds it not fireable is not counted."""
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

    @staticmethod
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

        In concurrent mode a run that ends on an error (a failed firing, a guard,
        priority or selector that raised, the fuse) lets the other firings in flight
        land before the error goes on, for the net's ``cancellation_timeout_seconds``
        at most (since 2026-11-28); a cancellation from outside cancels them.
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

    @staticmethod
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

    @staticmethod
    async def run_indefinitely(ctx: RunContext, *, tick: float = 0.1) -> None:
        """Drive the net on an internal clock — forever — draining input each tick and
        notifying observers, until ``ctx.stop`` is set (or the task is cancelled).

        Unlike ``run_to_completion`` it does **not** stop on an idle tick: nothing fireable just
        means the net is waiting for input or a time-based guard. This is the net-driven /
        real-time regime (#6) — the caller launches it as a task and ends it via ``ctx.stop``.
        Observers are awaited between ticks, so keep them cheap; a slow renderer would throttle
        the clock.

        Cancellation behaves as on ``run_to_completion``: it lands between ticks and at
        ``await`` points inside async bodies; a synchronous body finishes first; an
        ``execution="thread"`` body cannot be interrupted (the thread runs to completion).
        In concurrent mode, in-flight tasks are cancelled on the way out — their consumed
        tokens are not restored, so the graph shows the partial marking. A stop cancels them
        too; an error, a failed firing included, lets them land first, as on ``run``.

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

    @staticmethod
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

    @staticmethod
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

    @staticmethod
    async def _run_concurrent(ctx: RunContext, stop_after_n_firings: Optional[int] = None) -> bool:
        """Returns True if the net quiesced, False if ``stop_after_n_firings`` capped
        this call's launches (work may remain). In-flight firings always finish and
        deposit before returning.

        Each time round: an inner firing ahead of the outer net's turn, if one has a
        firing; else the outer net's turn, one launch sweep of every fireable
        transition (which moves the ring's cursor to the outer net when it launches
        something); else, when the sweep launched nothing, an inner firing behind
        it. An inner firing counts against ``stop_after_n_firings`` as a launch
        does."""
        graph = ctx.graph
        place_nodes, incoming, outgoing, read = _prepare(graph)

        await _notify(ctx)  # initial marking
        in_flight: dict[asyncio.Future, str] = {}
        # What each task consumed, so a failed firing can put its tokens back, and what
        # it claims; on ctx too, so that a Fire the drain applies is held to the claims.
        flight_state: dict[asyncio.Future, tuple] = {}
        ctx._flight_state = flight_state
        launched_this_call = 0
        try:
            while True:
                if Runner.drain_inbox(ctx):
                    await _notify(ctx)
                # An observer or a body may have assigned place.tokens directly.
                TokenIds.resync(graph)

                capped = (
                    stop_after_n_firings is not None
                    and launched_this_call >= stop_after_n_firings
                )
                # The turns ahead of the outer net's own.
                inner_turn = not capped and await _fire_inner(ctx, "ahead", _held(flight_state, place_nodes))
                if inner_turn:
                    launched_this_call += 1
                launched_this_sweep = 0
                if not capped and not inner_turn:
                    remaining_cap = (
                        stop_after_n_firings - launched_this_call
                        if stop_after_n_firings is not None
                        else None
                    )
                    launched_this_sweep = _launch_sweep(
                        ctx, place_nodes, incoming, outgoing, read,
                        in_flight, flight_state, remaining_cap=remaining_cap,
                    )
                    launched_this_call += launched_this_sweep
                    if (
                        stop_after_n_firings is not None
                        and launched_this_call >= stop_after_n_firings
                    ):
                        capped = True

                if set(in_flight.values()) != graph.in_flight:
                    graph.in_flight = set(in_flight.values())
                    await _notify(ctx)  # newly in-flight transitions light up as "working"

                if not capped and not inner_turn and not launched_this_sweep:
                    # The turns behind the outer net's own: the rest of the ring, bodies
                    # in flight or not, then the outer-first places once none is.
                    inner_turn = await _fire_inner(ctx, "behind", _held(flight_state, place_nodes))
                    if inner_turn:
                        launched_this_call += 1
                if inner_turn:
                    await _notify(ctx)

                if not in_flight:
                    if inner_turn:
                        continue  # an inline firing moved the marking: go round again
                    # Either the net quiesced, or the pacing cap stopped the launch
                    # sweep (work may remain).
                    return not capped

                if inner_turn:
                    # An inner net fired while bodies are in flight: let them run, take
                    # what has landed meanwhile, and go round again without waiting.
                    await asyncio.sleep(0)
                    done = {task for task in in_flight if task.done()}
                else:
                    done, _pending = await asyncio.wait(set(in_flight), return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    continue
                # In the order launched, as the tick loop and _land_in_flight land, so
                # that of two failures in one batch the one launched first is raised.
                for task in [task for task in in_flight if task in done]:
                    in_flight.pop(task)
                    fired_transition, consumed, firing_ids, _claims = flight_state.pop(task)
                    if task.exception() is not None:
                        graph.in_flight = set(in_flight.values())
                    # Raises TransitionFailedError carrying the consumed tokens if the
                    # body failed; restores them first when graph.restore_tokens_on_failure is set.
                    _complete(ctx, task, fired_transition, consumed, firing_ids, place_nodes, incoming)
                graph.in_flight = set(in_flight.values())
                await _notify(ctx)  # tokens deposited — branches advanced one stage
        finally:
            # Don't orphan in-flight bodies: asyncio.wait does not cancel what it
            # waits on, and an abandoned task's outputs would never be deposited.
            # On an error, a failed firing's included, the firings in flight land
            # first; on a cancellation or a stop they are cancelled.
            await _end_concurrent_run(ctx, in_flight, flight_state, place_nodes, incoming, sys.exception())

    @staticmethod
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

    @staticmethod
    async def _tick_concurrent(ctx: RunContext, tick: float) -> None:
        """One turn a tick, in the order of ``_run_concurrent``: an inner firing ahead
        of the outer net's turn if one has a firing, else the launch sweep, else an
        inner firing behind it; then whatever has landed is distributed."""
        graph = ctx.graph
        place_nodes, incoming, outgoing, read = _prepare(graph)

        await _notify(ctx)
        in_flight: dict[asyncio.Future, str] = {}
        # What each task consumed, so a failed firing can put its tokens back, and what
        # it claims; on ctx too, so that a Fire the drain applies is held to the claims.
        flight_state: dict[asyncio.Future, tuple] = {}
        ctx._flight_state = flight_state
        try:
            while not (ctx.stop is not None and ctx.stop.is_set()):
                changed = bool(Runner.drain_inbox(ctx))
                # An observer or a body may have assigned place.tokens directly.
                TokenIds.resync(graph)

                inner_turn = await _fire_inner(ctx, "ahead", _held(flight_state, place_nodes))
                launched_this_sweep = (
                    0
                    if inner_turn
                    else _launch_sweep(
                        ctx, place_nodes, incoming, outgoing, read, in_flight, flight_state,
                    )
                )

                for task in [t for t in in_flight if t.done()]:
                    in_flight.pop(task)
                    fired_transition, consumed, firing_ids, _claims = flight_state.pop(task)
                    # Raises TransitionFailedError carrying the consumed tokens if the
                    # body failed; restores them first when graph.restore_tokens_on_failure is set.
                    _complete(ctx, task, fired_transition, consumed, firing_ids, place_nodes, incoming)
                    changed = True

                graph.in_flight = set(in_flight.values())
                if not inner_turn and not launched_this_sweep:
                    # The rest of the ring, then the outer-first places once nothing is in flight.
                    inner_turn = await _fire_inner(ctx, "behind", _held(flight_state, place_nodes))
                changed = changed or inner_turn or bool(launched_this_sweep)

                if changed:
                    await _notify(ctx)
                if await _sleep_or_stop(ctx, tick):
                    return
        finally:
            await _end_concurrent_run(ctx, in_flight, flight_state, place_nodes, incoming, sys.exception())
