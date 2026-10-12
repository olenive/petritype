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

---

# Nets as tokens

Decided and built 2026-10-09 from `Cyberpunk2020-Simulation/dev-docs/NETS_AS_TOKENS.md`
(the consumer's proposal; its "As built (Python)" section records what differs). The wire
side is PROTOCOL.md §12. A token may hold Petri nets of its own; the net holding the token
runs them, observes them and (phase 2) saves them *through the boundary*.

## The model

- **Declaring.** `ListPlaceNode(..., nested=True)` says the place's tokens hold nets: each
  token is an `ExecutableGraph` (one unnamed inner net, `""`) or implements
  `__petritype_nets__(self) -> dict[str, ExecutableGraph]`. Checked at seed, deposit and
  `Extend`/`SetTokens` time with an error naming the place. `passive=True` (nested only)
  keeps the engine's hands off the inner nets: an outer body steps them.
- **Running (R2).** Inner nets are live. `fire_one_transition` gives one inner net the turn
  when the outer net's selector picks nothing: place order, then token order, then net order
  within a token, descending into a net's own inner nets before the next token, each net by
  its own selector (`NestedNets.fire_one_inner`). Both `execute_graph` and the sequential
  `Runner` loops get this for free; the concurrent loops take the same turn when nothing
  outer can launch and nothing is in flight (an inner net is never touched while an outer
  body that could reach it runs). Quiescence means nothing fireable anywhere. An inner
  firing counts as a firing of the outer net under its path, `"Depot/#17/cabin/Sit"`: that
  is what `step_count`, `last_fired`, `fired_counts`, `RunSummary` and the pacing and fuse
  limits see, so every existing driver and limit stays coherent without a second counter.
- **Reaching inside (R3).** An outer body reaches into the tokens it holds (an argument or a
  mutate edge) with `NestedNets.put(inner, place, tokens)`, `take(inner, place, n)` and
  `set_param(inner, name, value)`, which type-check, move ids and record `Added`,
  `Removed` and `ParamChanged` on the inner net. Guards read inner markings through the token
  (`graph.place_named(...).tokens[0].cabin`). That is the whole coupling (R10): no shared
  place, no inner transition sees the outer net.
- **Net parameters.** New on `ExecutableGraph`: `params: dict[str, Any]`, declared at
  construction (`construct_graph(params=...)`), read by guards, priorities and selectors as
  `graph.params`, and handed to a body as a keyword argument when it declares a parameter of
  that name that no edge and no `kwargs` entry supplies (`depart(vehicle, destination)`).
  `NetParams.set` records `ParamChanged(None, name)`; the runtime gains the `SetNetParam`
  command. Inner nets needed them (`stopped`, `seats`, `capacity`) and the Rust port already
  has net-wide parameters, so the saved form of a net (phase 2) and `set_param` without a
  transition can mean the same in both runtimes.
- **Observability (R4, R7).** Inner events reach the outer log wrapped in one `Nested(path,
  nested_id, event)` record, whatever the depth (an inner net's own `Nested` records are
  re-based onto the longer path). The path is as the marking stood when the event was
  gathered: the place the token stood in at the time. `nested_id` is the inner graph's
  `net_id`, minted per graph from a process-wide counter, and stable across the moves of the
  token that holds it. Gathering happens in `drain_events`, before a firing's own events are
  recorded (so what a body did inside comes before its `Fired`), and at the start of every
  firing, so a firing that drops a nested token loses none of its events. Inner nets record
  exactly when the outer net does.
- **Sink transitions.** A transition with no output edge now fires: its body must return
  `None` and nothing is deposited. `Board` and `Clear Announcement` needed it; before, the
  engine raised `KeyError` on such a transition.

## Defaults taken from the design document (section 7)

1. Driver policy: outer first, then inner nets in place, token and net order, repeated.
2. Both token-id paths and the stable nested id on the wire (PROTOCOL.md §12.1).
3. Inner events wrapped in a new `Nested` record, not a path field on every record.
4. Inner changes made by a failing body are not rolled back; `restore_tokens_on_failure` is
   conservation of the outer tokens only, and its docs say so.

## What differs from the design document, and why

- The cabin of the transit example (`examples/tokens_as_nets/transit.py`) does not re-seat passengers who
  came to the door to leave. Built as the document words it (`Sit` guarded by seats alone,
  the announcement cleared once nobody is seated or standing), the net ping-pongs between
  `Go To Door` and `Sit` and can never hold "three at the door, announcement cleared". The
  first fix (2026-10-09) made `Sit` and `Stand` wait while an announcement stands; since
  2026-10-10 they wait on the cabin's doors instead (below). Either way the
  document's expected result "Announcements emptied" after step 4 does not hold: the
  announcement stands until the last passenger has alighted.
- One `Go To Door` drawing from either of two places is two transitions (`Go To Door`,
  `Go To Door From Standing`): an input edge draws from one place.
- There is no acceptance script any more (2026-10-10, below). Until then its "run to quiet"
  held `Alight`, `Depart` and `Arrive` back (`RunContext.disabled`): the design fires them by
  hand, and the driver would otherwise fire `Arrive` the moment a vehicle is in transit and
  `Alight` the moment someone is at the door. Driven by plain stepping with nothing held
  back, that net ping-ponged `Alight Depot` / `Board Depot` for ever and the cabin never
  fired once.

## As built, 2026-10-09 (phase 1: core)

`petritype/core/executable_graph_components.py` (`NestedNets`, `NetParams`, the nested
checks, the descent in `fire_one_transition`, sinks), `petritype/core/events.py`
(`Nested`; `ParamChanged.transition` may be `None`), `petritype/core/nested.py` (the short
import), `petritype/runtime.py` (`SetNetParam`, nested checks on commands, the inner turn in
concurrent mode), `examples/tokens_as_nets/transit.py` (the design's §2 net, its acceptance script as
`main`; since 2026-10-10 its journey, below), `tests/test_nested_nets.py` (R1 to R4, R7, R10: 32 tests,
depth two included).

## As built, 2026-10-09 (phase 2: saving and templates)

`petritype/core/serialisation.py`, three namespaces:

- **`JsonForm`** (R5a's ground): which place types have a JSON form (a pydantic model, a
  JSON scalar, `ExecutableGraph`), `encode` (a model dumps by alias) and strict `decode`.
  Moved here from the runner's `translate.py`, which now delegates, so a token the library
  saves is a token the runner injects, by one rule.
- **`Templates`** (R5b): a process-wide registry, `Templates.register("Cabin", factory)` or
  `@Templates.template("Cabin")`, stamping `graph.template` on what the factory builds; a
  factory is called with no arguments, so its own arguments need defaults. Python has no
  builder to hang it on, and a token restored from JSON needs the template by name alone.
- **`NetJson`** (R5, R5a): `export_marking` / `import_marking` (by place name, oldest first;
  a place not listed loads empty, an unknown place is a `KeyError`, a token that does not
  decode a `ValueError`, both before anything changes; ids are not preserved, each place
  records `Resynced`), and the whole saved form `{template?, marking, params, enabled}` with
  `save`, `load_into` (onto a net built the same way: the top-level case, where the
  `@petri_net` factory is the template) and `restore` (build from the template, then load).
  `ExecutableGraph.export_marking()` and `.import_marking()` are the short spellings.

`ExecutableGraph` itself dumps in JSON mode as its saved form, validates from one (a wrap
validator rebuilds through the template), and declares the saved form as its JSON schema,
so a pydantic token type with an `ExecutableGraph` field (`Vehicle.cabin`) has the JSON form
of PROTOCOL.md §12.5 with no further work and counts as serializable. Python-mode dumps
keep the field form, which existing code reads.

`enabled` needed a switch on the graph: inner nets run without a `RunContext`, so
`ExecutableGraph.disabled` (honoured by `transition_is_fireable`) is the net's own switch,
saved and loaded as the `enabled` map; the runtime's `Enable`/`Disable` stay the runner's
switch on the net it drives.

R6, forking: `NestedNets.fork(token)` is a deep copy, and `ExecutableGraph.__deepcopy__`
mints a new `net_id`, so every copy of a graph is a new net (a snapshot-edge copy included)
and a fork is told apart from its original. The design's "rebuild from the template and
import the marking" is what `NetJson.restore(NetJson.save(net))` does when a copy must not
share Python objects at all.

Tests: `tests/test_nested_serialisation.py` (20), including step 6 of the acceptance script
through JSON text at both save points (since 2026-10-10: saved with the bus in transit and
once everyone has arrived, continued by plain stepping).

## As built, 2026-10-09 (phases 3 and 4: the protocol runner; the visualiser)

The runner is `DESIGN_RUNNER.md`, "As built, 2026-10-09: nested nets", and the wire form
PROTOCOL.md §12. The visualiser: `NodeLabel.default_token_summariser`
(`petritype/plotting/rustworkx_to_graphviz.py`) marks a token that holds nets by printing
its own fields and each inner net's name and token count in place of the net
(`NestedNets.summary`: `Vehicle(name='Bus 1', kind='Bus', destination=None) ⊂ cabin: 3`),
so a place of vehicles reads at a glance and the label never embeds a whole net. A descend
view needs nothing new: `NodeLabel.inner_nets(token)` names the nets a token holds, and each
is an `ExecutableGraph` that `RustworkxGraph.from_executable_graph` renders as any other,
so a notebook draws the city and, beside it, the cabin of the vehicle it is following:
`examples/tokens_as_nets/transit_journey.py` does exactly that, stepping the journey (45 firings from the
seed until every passenger has arrived, pinned by `tests/test_notebooks.py`; until 2026-10-10 the
acceptance script, 13 firings).

## As built, 2026-10-10: the transit net runs its own journey

An example illustrates how to build a net that runs its process correctly on its own, never
a net plus a script that makes it work. The transit net did not: under plain stepping with
nothing disabled (the Bevy viewer's Step, any `fire_one_transition` loop) the outer net
ping-ponged `Alight Depot` / `Board Depot` for ever, since the default tie-break takes the
first declared fireable transition and the driver gives an inner net the turn only when the
outer net has nothing to fire. The fix is the Petri net idiom: the vehicle's state is where
the control tokens in its cabin are, one per door, read by the outer guards and moved by an
inner transition and by `Arrive`.

- Every cabin template gains two places, `Doors Open` and `Doors Closed`, holding one token
  per door, the door's name (a bus or a shuttle has one door, `Front`, a ferry two, `Bow`
  and `Stern`), every door open when built. A door open with no announcement standing is
  boarding, a door open with an announcement standing is alighting, every door closed is
  under way. Two places and not one token naming the phase, so that the canvas shows it and
  more doors are simply more tokens. `Board` needs boarding, `Alight` alighting, `Depart`
  every door closed; inside, `Sit`, `Stand` and `Go Below` need boarding, the `Go To Door`
  transitions an announcement. `Close Doors` (new) moves one door from open to closed once
  nobody is left at the door and no announcement stands, aboard or not, so an empty vehicle
  moves on rather than block a stop; the ferry fires it twice. `Arrive` opens every door in
  its own body as it puts up the announcement: left to the cabin, `Depart` would fire first.
  `Clear Announcement` only takes the announcement down. `stopped` stays, set and shown,
  read by no guard.
- Alighted passengers go to a new place `Arrived`, never back onto a platform, where they
  would board again. The vehicles follow a loop (Depot, Central, Harbour, Airport); the net
  parameter `destination` is now an override (`None` by default).
- The seed adds two passengers at Central and two at the harbour; from it plain stepping
  delivers all seven to `Arrived` in 45 firings, and the same holds under `last_declared`
  and random tie-breaks at every level and in concurrent mode. A driver that gave the cabins
  the first turn would close every door before anyone boarded; both drivers fire the outer
  net first.
- The example keeps two helpers, `step` (one firing, the engine's choice) and `journey`
  (step until everyone has arrived or a budget runs out); the acceptance script, `fire`,
  `run_to_quiet` and `HELD_BACK` are gone. The notebook's Step and Repeat Step are those two.
  PROTOCOL.md §11 fixes the redesigned net, and `tests/protocol/05_nested_nets.json` was
  re-recorded with `tests/record_transcript.py` (`DESIGN_RUNNER.md`).

---

# Token keys, queries and filters

Decided and built 2026-10-09 from `Cyberpunk2020-Simulation/dev-docs/TOKEN_QUERYING_AND_FILTERING.md`
(the consumer's proposal; its "As built (Python)" section records what differs). The wire
side is PROTOCOL.md §13. Tokens carry *keys*, named attributes a place derives from each
token's value; one mechanism then gives queries, viewer filtering, projections and (phase 3)
keyed input edges.

## The model

- **Keys (R1).** `ListPlaceNode(..., keys={"species": lambda s: s.name, "class": lambda s:
  s.class_})`, or `keys=("name", "diet")` for attribute names; `place.key_names` is the
  declaration order. A key value is a string, an integer or a boolean, and `1` is not `True`
  (`same_value` compares type and value). Keys are derived on demand (`Keys.of(place,
  token)`), so a token mutated in place has new keys the next time they are read.
- **No index.** The design asks for an index per key and value kept in step on `Added`,
  `Removed` and `Mutated`. Not built: Python's tokens are plain objects in plain lists, moved
  by the engine at several points, and a query over a net of this library's size costs a
  scan; an index is a later optimisation if profiling asks for one, behind the same
  `Query` functions.
- **Filters (R2).** `Filter(clauses, places?, prefix?)`, a `Clause(key, values)` ORing its
  values or `Clause(key, param=name)` accepting the net parameter's current value; AND across
  clauses; `Filter.where(class_="Bird", diet=[...])` as the short spelling (a trailing
  underscore is dropped, so Python keywords can name keys). A place that declares no keys
  matches no clause; no clauses and no restriction keeps everything.
- **Queries (R2).** `Query.matches(graph, filter)` in place order then token order, each a
  `Match(place, token_id, keys, token)`; `Query.distinct(graph, key, places?)` in order of
  first appearance; `Query.count` and `Query.first` for a place.
- `examples/token_view_filtering/wildlife.py` is the design's §2 net: habitats as places, `Species` tokens with
  `class` as a key they carry (the field is `class_`, aliased `class` on the wire), `Migrate`
  as take-all-and-put-back until keyed edges land, `Settle` routing by destination, `Spread`
  and `Decline` through mutate edges. Its five questions and section 6's answers are
  `tests/test_token_keys.py`.

## As built, 2026-10-09 (phase 1: core)

`petritype/core/keys.py` (`Keys`, `Clause`, `Filter`, `Match`, `Query`), the `keys` field
on `ListPlaceNode`, `examples/token_view_filtering/wildlife.py`, `tests/test_token_keys.py` (17).

## As built, 2026-10-09 (phase 2: the protocol)

`DESIGN_RUNNER.md`, "As built, 2026-10-09: keys, queries and the connection's projection",
and PROTOCOL.md §13: keys on the wire, `query`, `distinct`, `set_filter`, `hidden`, the
`wildlife` conformance net and transcript `06_keys_query_filter.json`.

## As built, 2026-10-09 (phase 3: keyed input edges)

`TakeWhereEdge(place, transition, argument, where=Filter)` and `MutateWhereEdge(...)`,
subclasses of `ArgumentEdgeToTransition` and `MutateEdge` (so `construct_graph`, the edge
validators and the adjacency maps take them as they are), re-exported from
`petritype.core.keys`. A transition with one is fireable when the place holds a token the
filter keeps (`KeyedEdges.first_kept`; the filter's `{key, param}` clauses read the net's
parameters); its firing takes, or hands over live, the oldest such token and nothing else,
so the other tokens are never removed and re-added and keep their ids. The argument receives
the one token, never a list. A failed firing restores a keyed take to the front of its place
(best effort, like a plain take). On the wire the edge is `take_where` / `mutate_where` with
its `filter` (§13.5); the runner's `launched.consumed` names the token actually taken, and
its snapshot leaves that token out wherever it stood. `Migrate` in `examples/token_view_filtering/wildlife.py`
is now one keyed edge; section 6's "only events" (one token out, one in, per firing, the fox
untouched) is `tests/test_keyed_edges.py`.

## As built, 2026-10-09 (phase 4: the visualiser)

`NodeLabel.default_place(node, summariser, filter=None, params=None)` and
`RustworkxToGraphviz.activation_coloured_attr_functions(graph, summariser, filter=None)`
(`petritype/plotting/rustworkx_to_graphviz.py`): with a filter a place lists only the tokens
it keeps, its header reads `matching / count` and a last line says `(n hidden by filter)`, so
a hidden token is never mistaken for an absent one (the design's R4 rule for the Bevy viewer,
applied to the Graphviz renderer). The tabs and the filter bar of R4 are the Bevy viewer's;
a notebook builds its own controls from `Query.distinct`: `examples/token_view_filtering/wildlife_filter.py`,
one tab per `class` and per `diet`, a season switch that lets the keyed-edge migration fire,
and the same question asked of the net as a `Query.matches` table.
