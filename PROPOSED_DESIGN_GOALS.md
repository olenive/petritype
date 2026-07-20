# Design Goals

**What we're building:** a runtime for long-lived, observable, interactive programs
expressed as Petri nets — nets you can watch and poke *while they run*. **Not** a
pipeline runner; **not** a game.

**State is ephemeral.** A net's marking lives in memory; kill the process and it's
gone — no magic retention. What persists is the net's *definition* and the *intent*
to run it: on restart the runtime reloads and runs it again from a fresh start, not
from where it died. Persisting expensive data (downloads, trained models) is the
*net's own* job — write to disk, cache-first. The runtime won't snapshot your
marking for you.

Game-grade interaction quality is the *bar*, not the identity: if you can't render
a running net's state and respond to input promptly, it isn't done.

## A running net is a live object

- **Observable** — poll the marking, stream the log, get pushed events.
- **Interactive** — write tokens to its input places; read its output places.
- **Restartable** — desired-state persists run-intent, so after a worker reboot the
  runtime reloads and restarts the net (from a fresh marking, *not* a state restore).
- **Real-time** — an internal clock/cadence drives it, not an external caller.

## Two interaction tiers

- **Tier 1 (application):** input places + output places + clock + lifecycle.
  Data-only, safe, portable. The normal way to use a net.
- **Tier 2 (operator/debug):** force-fire, change guard/selector, edit topology,
  snapshot/restore. Privileged.

## Scope discipline

Build a feature when its rationale traces to "a monitoring/automation runtime needs
this." Stop when it traces to "well, it's a game" (renderers, physics, ECS, frame
timing, netcode). The tiny game is a **test vehicle** that proves the model — not
the product.

---

# Runner & state-flow design

Decisions settled in design discussion (2026-06-21). These specify the runtime that
replaces the hand-rolled `while`/`for` loops currently copy-pasted into every example
notebook. **One way to define a net; one runner; selectable execution modes.**

## Execution is functions over data, not an object

- **`Runner`** is a namespace class of functions (matching `ExecutableGraphOperations`) —
  no instances, no hand-written `__init__`.
- Run-state lives in (a) the `ExecutableGraph`, mutated in place, and (b) a passive
  **`RunContext`** dataclass the *caller* owns (`graph`, `mode`, `observers`, `inbox`,
  `stop`, `strict`). `RunContext` is the typed replacement for the notebooks' ad-hoc
  `session` dict.
- **Lifecycle is the caller's.** `run_indefinitely` is a coroutine the caller launches and
  cancels; the runner owns no hidden background object. Stdlib resources (`Queue`,
  `Event`, `Task`, an executor) are held by the caller — they are resources, not our
  behavior objects.

## Observation — live reference out, render at your own cadence

- Observers are functions `(graph) -> None` handed the **live graph by reference**
  (zero copy). The graph already carries everything a renderer needs: marking,
  structure, and `step_count` / `last_fired` / `fired_counts`.
- **"Don't mutate" is a contract** for observers, as today's visualizers already honor.
  Two opt-in guards, both off the hot path: `Runner.read_only(graph)` (a deep
  read-only view for dev / untrusted visualizers) and a *serializing* observer that
  projects to a small renderable summary at a process boundary.
- **Immutable snapshots rejected as the default:** per-step copy cost for high-rate
  nets, and tokens are not guaranteed serializable. A `NetSnapshot` survives only as
  *one consumer's* concern — the web/remote observer that projects to a serializable
  render-summary (place names + token counts/labels), never raw tokens.
- Visualizers **re-derive structure per render** (already the default via
  `from_executable_graph`), so topology changes need no special handling on the view side.

## Input — commands on an inbox, drained between steps

- Input is typed **commands** placed on `RunContext.inbox` (an `asyncio.Queue` the
  caller owns). The runner **drains all pending commands at each step boundary**
  (between fires), applies them in FIFO order, then steps. It is the dual of the
  observation stream, and the same channel works in-process and cross-process.
- **Never direct mutation from a UI callback** — that risks torn mid-fire writes (the
  engine caches its lookup maps at the top of an `execute_graph` call). The runner is
  the only thing that mutates the marking.
- **The visualizer is the input *surface* but produces commands, never touching the
  graph** — so "visualizers don't mutate the net" holds even for interactive controls.
- **Tier-1 command vocabulary:**
  - Places: `Extend(place, tokens)` — append a list of tokens (single token = `[token]`;
    the argument is *always* a list); `SetTokens(place, tokens)` — replace all tokens
    (`[]` clears the place).
  - Transitions: `SetParam(transition, …)` (over `kwargs`); `Enable` / `Disable`.
  - The append/extend ambiguity (one list-valued token vs many tokens) is resolved by
    *always* treating the argument as a list of tokens. Guardrails: require an actual
    `list` (kills the `str`-is-iterable footgun) plus a per-element type-check against
    the place type. `Append(place, token)` may return later as pure unambiguous sugar.
- **`strict` flag** on `RunContext`: default (non-strict) drops-and-logs malformed input
  (right for live interactive nets); `strict=True` raises (right for tests / dev / CI).

## Topology may change — do not preclude it

- The runner **assumes it is handed a valid net.** Whatever mutates structure owns
  keeping it valid — structurally, *and* by mutating only between steps. `validate_graph`
  is exposed as a tool the mutator may call on itself.
- **Do not build a topology-edit API yet**; just don't assume static structure
  (re-derive per render; reserve an optional `structure_version` for bandwidth caching).

## Ordering is intentional — earlier wins

- **Token extraction is FIFO** (`place.tokens.pop(0)`), not the prototype's accidental
  LIFO (`pop()`). Makes `Extend` order = processing order, and matches the already
  insertion-ordered bulk-extraction path. (Switch the container to `deque` only if
  profiling demands it.)
- **Transition selection defaults to first-defined-wins**, implemented directly (remove
  the double-reversal; delete the vestigial `next_transition`). `transition_selector`
  remains the override for priority / round-robin / random.

## Read arcs / non-consuming reads (implemented)

Two non-consuming edge types let a transition read a place without consuming it — modelling
parameters, toggles, and guard-state **uniformly as places you write a token to** (each a
visible, pokable node):

- **`SnapshotEdge`** — the transition receives a **deep-copy snapshot**; the place is untouched
  (the read cannot disturb it).
- **`MutateEdge`** — the transition receives the **live tokens** and may modify them in place;
  they stay (not consumed) but may be changed.

Both require **presence** to enable (a token must exist to read) and never consume. The
argument receives all tokens (list-typed arg) or the single token (scalar arg). Rendered
distinctly: `SnapshotEdge` is a dashed grey arrow into the transition; `MutateEdge` is a thicker
bidirectional grey line with dot ends — read vs read/write at a glance. Both edges (and their
read-place nodes) recolour magenta when their transition fires, like the consuming / producing
edges. Wired into the engine, both run modes, and the input layer (update a read-arc place with
`SetTokens`).

## Co-located controls — node-keyed panel + control-map

- A control is bound to a node via a separate **control-map** `{node_name: ControlSpec}`,
  authored by the app and kept *off* the net (the `ExecutableGraph` stays pure data).
- `ControlSpec` is a passive record: target `node`, `kind` (toggle / button / slider /
  select), `label`, and `to_command(value) -> Command` (UI value → inbox command).
- The UI renders a **panel keyed by node name** beside the diagram, binds each widget to
  its `to_command`, and enqueues onto `ctx.inbox` (see Input) — never touching the graph.
- Needs **no renderer change** (node-keyed = names we already have). Pixel-on-node overlay
  is a future additive upgrade, once the renderer emits node coordinates.

## Observation control regimes — who holds the clock

Observers are **always coalescing** best-effort views of current state; there is no
lossless-recording observer. The real distinction is who drives firing:

- **Net-driven (autonomous):** `run_indefinitely` (+ tick) fires on its own cadence;
  observers coalesce against a latest-reference slot + wake event (zero copy). If a
  renderer can't keep up it shows a stale-but-current semblance — dropping intermediate
  frames is correct. The engine **never blocks on a renderer** (which also protects input
  draining).
- **Observer-driven (stepped):** `Runner.step(ctx)` fires one and returns; the caller
  renders and decides when to advance — the **advance signal can be anything** (a button,
  an animation-complete callback, a timer). The net is *gated* on the observer: a
  deliberate control inversion, not back-pressure. No frames missed, because the net never
  runs ahead.

Where lossless completeness is genuinely needed (audit / replay of an autonomous net), it
is the **engine's own cheap in-loop log** (`step_count` / `fired_counts` /
`transition_history`), not a buffering observer. Observers view; the engine records.
`runtime.fired_since(previous, current)` is the bridge between the two: an observer
snapshots `fired_counts` and diffs at its next wake-up, recovering every firing as
per-transition counts even though the notifications themselves coalesce. (`last_fired`
stays a UI convenience — it names only one completion per concurrent deposit batch.)

## Execution responsiveness — two independent axes

Responsiveness under a long-running transition decomposes into two *separate* axes, often
conflated as "concurrency":

1. **Where the body runs** (#8) — inline on the event loop vs **offloaded** to an executor.
   Pure responsiveness; **no Petri-net semantic change**.
2. **How many transitions are in flight** (#7) — **sequential** (one resolves fully before
   the next is picked) vs **concurrent** (multiple bodies run as tasks, deposit on
   completion). The semantically loaded axis.

A long op needs axis 1 (offload). It needs axis 2 only if the *rest of the pipeline must
keep firing during* the long op.

## Concurrent firing (#7)

**Default `SEQUENTIAL`; `CONCURRENT` is opt-in** via `RunContext.mode` — one definition,
two run modes.

**The engine's stages are already factored** for this: a fire is `stage_1_extract`
(consume tokens — *synchronous*), `stage_2_call` (run body — *async*), `add_tokens`
(deposit — *synchronous*). Sequential runs them inline, one transition at a time.
Concurrent is the **same stages, re-orchestrated** — not a rewrite. `Runner` picks the
orchestrator by `ctx.mode`.

**Concurrent semantics — maximal, conflict-free, lock-free:**

```
each round:
  for each enabled transition (re-checking as you go):
      stage_1 consume its tokens     # synchronous — removes them now
      launch stage_2 body as a task
  await FIRST_COMPLETED
  for each finished task:
      stage_3 deposit; mark not-in-flight; step_count += 1
  repeat until nothing enabled AND nothing in-flight
```

- **No locks.** Single-threaded asyncio + synchronous `stage_1` means there is no `await`
  between "is it enabled?" and "consume its tokens" — so two transitions can't double-spend.
- **Conflicts auto-resolve** via consume-as-you-go: consuming transition A's tokens disables
  a conflicting B in the same round. No separate conflict resolver.
- **Independent branches overlap** (the `execution_modes` example: two slow branches →
  sequential ≈ sum of durations, concurrent ≈ max).

**In-flight marking:** while a body runs, its inputs are consumed-and-gone, outputs not yet
deposited. The cue is the transition shown **in-flight** (the `in_flight` view field + the
existing activation-colour highlight); tokens reappear on completion. A fuller
reserved-token holding-area model is deferred.

**Trades accepted in concurrent mode (why sequential stays default):**

- **No deterministic replay** — completion order is wall-clock-dependent. `step_count` still
  counts fires monotonically, but their order isn't reproducible. Need replay → sequential.
- **Error = consumed tokens lost** — a body that raises has already consumed its inputs.
  Default **log + drop** (matches `strict` / drop); rollback or an error-place is a later
  option.
- **Visualization blurs** — multiple transitions lit at once; the crisp one-per-step
  animation lives in sequential mode.

**Deferred:** the per-transition concurrency flag ("only these overlap") — maximal
concurrency is simplest and most illustrative for v1.

## Offloading blocking transitions (#8)

A body that doesn't yield blocks the single event-loop thread (freezing firing, clock, input
draining, observers). `async` does **not** rescue CPU-bound work — a coroutine that computes
without `await` never yields. The fix is `await loop.run_in_executor(...)`. Which executor is
entirely a **GIL** story (only one thread runs Python bytecode at a time):

| body | executor | why |
|---|---|---|
| async I/O (awaits) | none — just a task | already yields |
| blocking-sync I/O (`requests`, DB, `sleep`) | **Thread**Pool | GIL released while waiting |
| native compute (PyTorch / NumPy / BLAS) | **Thread**Pool | GIL released in C / CUDA kernels |
| pure-Python CPU (tight Python loop) | separate **process** | only way past the GIL |

**Key practical point:** the thread pool already covers I/O *and* library compute — so it
parallelizes most real work, including most ML inference, **with no serialization**.
Pure-Python CPU that's a genuine bottleneck and can't be vectorized into a GIL-releasing
library is the *rare tail*.

**Decision — the engine offers `execution = inline | thread` only.** Both are shared-memory:
**no pickling, no assumptions about token types.**
- `inline` (default) — instant bodies.
- `thread` — opt-in offload for blocking I/O and library compute. Engine-managed, safe for
  any tokens.

**Process-level parallelism is the transition function's own concern (approach A).** A
transition needing a separate process does it *inside its own body*: brings its own process
pool, serializes only the clean payload it knows is picklable, awaits the result, returns it.
To the engine it is a normal async transition that awaits — the **engine never owns a process
pool or pickles a token.** Rationale: tokens may not be picklable, and the framework must not
assume otherwise (a `ProcessPoolExecutor` pickles args + return + the function itself); the
author who needs processes is the one who knows their data.

> A net-level "all tokens picklable" contract enabling engine-managed process offload
> (approach **B**) is **left out** for now — process-level parallelism in Python is likely to
> improve (free-threaded / no-GIL builds, CPython 3.13+), which may remove the need for
> process pools and their pickling altogether. Revisit then.

**Executors are caller-owned resources on `RunContext`** (`thread_pool`, or `None` → default);
pool size is the caller's knob. **Offload composes with both modes:** sequential + thread =
responsive single-stream (solves "stay responsive during a 10 s inference" with *no*
concurrency); concurrent + thread = parallel multi-core throughput. Errors surface via the
awaited future — same `strict` / log-drop policy.

---

**Status: design complete (2026-06-21); implementation underway.** Concerns #1–#11 resolved.

**Done:**
- The **ordering cleanup** (#9 FIFO extraction `place.tokens.pop(0)` + #11 first-defined
  selection, implemented directly — the double-reversal and the vestigial `next_transition`
  removed) and removal of the `stage_1` debug-print cruft (the lone remaining info line is now
  `verbose`-gated). All 17 notebook tests pass; gallery regenerated.
- **`petritype/runtime.py`** — the **core `Runner`**: a namespace class of functions over a
  passive `RunContext` (`graph`, `mode`, `observers`), with `run_to_completion` / `step` and
  the `SEQUENTIAL` / `CONCURRENT` orchestrators. Observers are sync-or-async callables handed
  the live graph after each state change.
- **Input layer (#4)** in `petritype/runtime.py` — typed commands on `RunContext.inbox`
  (`Extend` / `SetTokens` for places; `SetParam` / `Enable` / `Disable` for transitions),
  drained FIFO **between steps** and wired into both modes and `step`. `RunContext.strict`
  raises on malformed input vs the default drop-and-log. The runner is the only thing that
  mutates the graph; a control/UI only produces commands.
- **`run_indefinitely` (#6 real-time)** in `petritype/runtime.py` — drives the net on an
  internal `tick` until `RunContext.stop` is set, draining input each tick and **surviving idle
  ticks** (it never stops just because nothing is enabled). Works in both modes (concurrent
  polls in-flight tasks per tick). Verified headlessly: autonomous ticking, live mid-run
  injection, clean stop.
- **`examples/execution_modes/`** — `net.py` (two independent 3-stage branches) and two
  **animated** notebooks that now import the lifted `Runner` and differ in exactly one value
  (`ExecutionMode.SEQUENTIAL` vs `.CONCURRENT`), proving "one definition, two run modes."
- **`examples/interactive/parcel_sorter.py`** — a user-driven net (`Inbox → Sort → Sorted`)
  where each button *produces a command onto the inbox* (`Extend` to inject a parcel,
  `Enable` / `Disable` to toggle the sorter, `SetTokens` to clear, `step` to advance). The
  first example where you poke a net *while it runs*; exercises the whole input layer.
- **`examples/interactive/realtime_sorter.py`** — the same net running on a clock: marimo's
  `mo.ui.refresh` timer drives `Runner.step` every 0.4 s, draining queued input each tick, so
  you inject *while it runs*. (Marimo owns the clock here; `run_indefinitely` is the owned-loop
  equivalent.) Structurally verified via export; the live ticking + injection needs confirming
  in `marimo edit`.
- **`examples/interactive/read_arc_scaler.py`** — read arcs live: `Scale` reads a `Multiplier`
  via a `SnapshotEdge` (adjust the factor live with ➖ / ➕; it's never consumed) and increments
  a `Processed` tally via a `MutateEdge` (a running total kept in place). Both render dashed —
  the demo of read arcs composing with the input layer.
- **Read arcs (#10)** — non-consuming `SnapshotEdge` (deep-copy read, place untouched) and
  `MutateEdge` (live read/write) edge types: `construct_graph` routing, name + type validation,
  presence-required enabledness, non-consuming read in both run modes (deep-copy vs live), and
  dashed rendering (snapshot: no head; mutate: diamond). Verified.

- **Offload (#8)** — `FunctionTransitionNode.execution = "inline" | "thread"`; a sync body
  marked `thread` runs via `loop.run_in_executor` (the caller's `RunContext.thread_pool`, else
  the default) so it never blocks the loop. Async bodies run inline (already yield). Verified:
  sequential + thread keeps the loop responsive under a 0.3 s blocking body (55 loop ticks vs 0);
  concurrent + thread runs two such bodies in parallel (0.31 s vs 0.61 s). Process-level
  parallelism stays the function's own concern — the engine never pickles a token.

- **In-flight visualisation** — concurrent mode maintains `ExecutableGraph.in_flight` (transitions
  whose bodies are running but haven't deposited yet); the runner notifies after launching, so
  those transitions render **gold ("working")** and clear as each deposits. Verified: two
  overlapping bodies show gold mid-flight, then clear.

- **Control-map (#5)** — `ControlSpec` + `apply_control` (framework, in `runtime.py`) and
  `petritype/marimo_controls.py` (`build_controls` / `controls_row` / `drain_controls`): a
  declarative `{name: ControlSpec}` map, kept off the net, rendered as marimo widgets that only
  *produce inbox commands* (buttons on click, toggles / sliders / selects on change).
  `examples/interactive/parcel_sorter.py` refactored onto it. Verified.

**Status: the runtime spec (concerns #1–#11) is fully built and verified.** Possible future
work, none blocking: **pixel-on-node** co-located controls (vs the current node-keyed panel);
**operator / Tier-2** capabilities (force-fire, topology editing, snapshot / restore); the
**serializing observer** for a cross-process / web frontend; and the fuller reserved-token
in-flight model (the current cue is the gold transition highlight).
