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
  snapshot/restore. Privileged. Only snapshot/restore exists, as the JSON save and
  restore (`NetJson.save` / `NetJson.restore`). The `Fire` command fires a named
  transition only when it is fireable, so it is not force-fire.

## Scope discipline

Build a feature when its rationale traces to "a monitoring/automation runtime needs
this." Stop when it traces to "well, it's a game" (renderers, physics, ECS, frame
timing, netcode). The tiny game is a **test vehicle** that proves the model — not
the product.

## What this file holds

The code is the source of truth: its docstrings and tests, `PROTOCOL.md` (the wire
contract shared with the Rust runtime) and `CHANGELOG.md` (what changed in each
release). This file keeps, per area, a short overview and where to look in the code,
the decisions whose reasons the code cannot hold (alternatives rejected included), and
what is not built yet, listed at the end of each area. The protocol runner has its own
file, `DESIGN_RUNNER.md`.

---

# Runner and state flow

`petritype/runtime.py` replaced the hand-rolled `while`/`for` loops the example
notebooks used to carry: one way to define a net, one `Runner`, selectable execution
modes. Its module docstring lists the layers (execution modes, observation, input
commands, real-time ticking, offload, non-consuming edges, limits, nested nets, closed
nets, controls). `petritype/marimo_controls.py` renders a control-map as marimo
widgets. Examples: `examples/execution_modes/` (one net, two modes) and
`examples/interactive/`. The concern numbers (#1 to #11) are those of the design
discussion of 2026-06-21; the module docstring uses some of them.

## Decisions

- **Functions over data, not an object.** `Runner` is a namespace class of functions,
  like `ExecutableGraphOperations`. Run state lives in the `ExecutableGraph` (mutated in
  place) and a passive `RunContext` the caller owns. The lifecycle is the caller's:
  `run_indefinitely` is a coroutine the caller launches and cancels, and the stdlib
  resources (queue, event, task, executor) are held by the caller, so the runner owns no
  hidden background object.
- **Observers get the live graph by reference** (zero copy); not mutating it is a
  contract. Immutable per-step snapshots were rejected as the default: a copy per step
  costs too much at high firing rates, and tokens are not guaranteed serializable. A
  serializable summary (place names, counts, labels, never raw tokens) is the concern of
  the one consumer that crosses a process boundary. Renderers re-derive structure on
  each render, so a topology change needs nothing on the view side.
- **Observers coalesce; the engine records.** There is no lossless observer. An
  autonomous net (`run_indefinitely`) never blocks on a renderer, which also keeps input
  draining; a renderer that falls behind shows the latest state, and dropping frames is
  correct. A stepped net (`Runner.step`) is gated on its caller, who decides when to
  advance (a button, an animation finishing, a timer), so no frame is missed. Where
  completeness matters (audit, replay) it comes from the engine's own record
  (`step_count`, `fired_counts`, `transition_history`, the events), and `fired_since`
  recovers every firing from two snapshots of `fired_counts`.
- **Input is commands on an inbox, drained between firings** (#4). Never direct mutation
  from a UI callback: that risks writes torn mid-fire, since the engine builds its lookup
  maps once at the start of a call. A visualiser is the input surface but only produces
  commands, so "visualisers don't mutate the net" holds for interactive controls too.
- **A command's tokens are always a list.** `Extend(place, [token])`, never a bare
  token, which settles one list-valued token versus many tokens. An actual `list` is
  required (a `str` is iterable too), and each element is type-checked against the
  place.
- **`strict`** on `RunContext`: drop and log malformed input by default (right for a
  live net), raise under `strict=True` (right for tests and CI).
- **Topology may change; the runner does not police it** (#1). The runner assumes it is
  handed a valid net. Whatever changes structure keeps it valid and does so only between
  firings. No topology-edit API is built, but nothing may assume static structure.
  The lookups by name and the firing maps are kept between calls for speed, so the
  editor also tells the net (`ExecutableGraph.structure_changed`, decided 2026-11-27):
  a key that noticed every edit would cost as much as the lookups it saves, and an edit
  made to a built net directly is already the editor's responsibility.
- **Earlier wins** (#9, #11). Tokens are taken first in, first out, so `Extend` order is
  processing order and matches the list-mode path (the prototype's last-in-first-out
  `pop()` was an accident). The default selector picks the highest priority, ties to the
  first declared; `tie_break` (`default_transition_selector`) and `transition_selector`
  are the overrides.
- **Non-consuming edges model parameters, toggles and guard state as places** you write a
  token to (#10), so each is a visible node you can poke, with no mechanism of its own.
- **Controls stay off the net** (#5). A control-map `{name: ControlSpec}` is authored by
  the app and kept beside the net, so `ExecutableGraph` stays pure data. The panel is
  keyed by node name, so it needs no renderer change.
- **Responsiveness has two independent axes**, often both called concurrency: where a
  body runs (inline, or offloaded to a thread: no change to Petri-net semantics), and how
  many transitions are in flight (sequential or concurrent: a semantic change). A long
  body needs the first; it needs the second only if the rest of the net must keep firing
  meanwhile.
- **Offload is `execution = "inline" | "thread"` and nothing more** (#8). `async` does not
  help CPU-bound work that never awaits. A thread pool covers blocking I/O and native
  compute that releases the GIL (NumPy, PyTorch), which is most real work, with no
  pickling and no assumption about token types. A body that needs a separate process
  brings its own pool and pickles only what it knows is picklable (approach A): the
  engine never owns a process pool or pickles a token, since tokens may not be
  picklable and the author is the one who knows their data. A net-level "all tokens
  picklable" contract for engine-managed process offload (approach B) is left out:
  free-threaded CPython (3.13+) may remove the need for process pools. Offload composes
  with both modes.

## Concurrent mode (#7)

`SEQUENTIAL` is the default; `CONCURRENT` is opt-in through `RunContext.mode`, one
definition and two run modes. Concurrent mode reuses the engine's stages (take the
inputs synchronously, run the body as a task, deposit on completion) and only
orchestrates them differently: `_run_concurrent`, `_tick_concurrent` and
`_launch_sweep` in `petritype/runtime.py`. Independent branches overlap: in
`examples/execution_modes/` sequential takes about the sum of the branch durations,
concurrent about the longest.

**Claims (since 2026-11-26).** Before a launch the runner plans which tokens the firing
will take or be handed (`plan_stage_1` in `petritype/core/executable_graph_components.py`)
and derives reader/writer claims from the plan, per token, or on the whole place for an
edge that takes or reads every token (`_Claims`, `_claims_from_plan`). A transition whose
claims conflict with those of the firings in flight does not launch (`_conflicts`). A
`Fire` command is one of the library's own, so it is held to the same claims and does
nothing when they conflict (decided 2026-11-27).

- **Why.** The earlier design was lock-free: single-threaded asyncio and a synchronous
  take meant no two launches could take one token, and a launch that took a token
  disabled its rivals. That holds only while every input leaves its place at launch. It
  predates the edges that leave a token in its place while a body works on it (mutate
  and read edges). Without claims, two mutators of one token overlapped, and another
  transition could take a token a body in flight was mutating.
- **The trade: head-of-line waiting.** A transition picks its token as it would
  sequentially: the front token, or the oldest match for a keyed edge. If that token is
  claimed, the transition waits; it does not skip to the next token. So a slow reader or
  mutator of the front token holds back consumers of the tokens behind it. Measured: with
  three tokens, a 50 ms reader of the front token and a plain consumer, the consumer took
  nothing until the read ended; before, it took all three during the read.
- **Why wait rather than skip.** Skipping would make which token a transition gets
  depend on timing, and would break first-in-first-out order and the agreement with
  sequential mode on which token is chosen.
- **No lock deadlock.** A launch's claims are checked all at once, and nothing is held
  while a transition waits.
- **Ordering.** Each sweep picks one launchable transition at a time with the net's
  selector, so priorities, `tie_break` and `transition_selector` apply in concurrent
  mode as in sequential.
- **Inner nets** fire inline beside outer bodies in flight (since 2026-11-21), except the
  nets of a token an outer body in flight holds through a mutate or read edge, and the
  subnet of a subnet place whose port an outer firing in flight has an edge on (`_held`,
  `_fire_inner`): a body may run on a thread while the loop fires inline. A body handed
  every token of a place to change holds the place's own list, so the nets of tokens
  that land there while it runs are held still too (decided 2026-11-27). Making the
  deposit itself wait was rejected: the firing that produced the token has finished with
  it, and waiting would add another queue behind a slow body.

**The limit of the guarantee.** Python has no ownership model, so the claims cover only
the firings the concurrent runner launches and those a `Fire` command makes. A body, a guard, an observer or the program
can reach any token or the net itself at any time, and a body can keep a reference to a
token after it returns. The library does not and cannot prevent that; a user who makes
such changes takes responsibility for them. This is the position the README takes for
closed nets ("What Python cannot catch").

**Trades accepted, and why sequential stays the default.**

- No deterministic replay: completion order depends on the wall clock. `step_count`
  still counts every firing, but their order is not reproducible.
- A failed body fails the run with `TransitionFailedError`, as in sequential mode; its
  consumed tokens are lost unless `restore_tokens_on_failure` puts them back (README,
  "Failure semantics": a visible loss beats silent corruption).
- The drawing blurs: several transitions are lit at once. A body's inputs are gone from
  their places until it lands; the cue is `graph.in_flight`, drawn gold.

## Open / not yet implemented

Not built:

- `Runner.read_only(graph)`, a deep read-only view for untrusted visualisers, and a
  serializing observer that projects the net to a small renderable summary at a process
  boundary. (The protocol runner serves a serializable view of a net to other
  processes, `DESIGN_RUNNER.md`, but not as an observer.)
- Tier 2: force-fire, changing a guard or selector at run time, and a topology-edit
  API. With it would come an optional `structure_version` for bandwidth caching, and a
  `validate_graph` a mutator can call on itself: neither exists; the build's checks are
  `ExecutableGraph`'s validators and `ExecutableGraphCheck`.
- Pixel-on-node controls, once the renderer emits node coordinates (today's panel is
  keyed by node name).
- A per-transition concurrency flag ("only these overlap").
- A reserved-token holding area showing a body's inputs while it runs (today's cue is
  the in-flight highlight).
- Rollback or an error place for a failed firing. Expected failures are the body's to
  catch and route to an error place by its return type.
- `Append(place, token)` as sugar for `Extend(place, [token])`.
- A `deque` for a place's tokens, only if profiling asks.
- Engine-managed process offload (approach B, above), to revisit once free-threaded
  CPython is usable.
- `examples/interactive/realtime_sorter.py` is checked by export only; its live ticking
  and injection were to be confirmed in `marimo edit`.

---

# Nets as tokens

A token may hold Petri nets of its own; the net holding the token runs them, observes
them and saves them through the boundary. Decided and built 2026-10-09 from
`Cyberpunk2020-Simulation/dev-docs/NETS_AS_TOKENS.md` (the consumer's proposal; its "As
built (Python)" section records what differs). Wire side: PROTOCOL.md §12.

Where to look: `NestedNets` and `NetParams` in
`petritype/core/executable_graph_components.py` (short import `petritype/core/nested.py`),
`Nested`, `PathStep` and `InnerFiring` in `petritype/core/events.py`, and `JsonForm`,
`Templates` and `NetJson` in `petritype/core/serialisation.py`. Example:
`examples/tokens_as_nets/transit.py` with its notebook `transit_journey.py`. Tests:
`tests/test_nested_nets.py`, `tests/test_nested_serialisation.py`,
`tests/test_stepping_rules.py`.

## Decisions

- **The token is the whole coupling** (R10). No shared place, and no inner transition
  sees the outer net. An outer body reaches into the nets of the tokens it holds with
  `NestedNets.put` and `take`, at the ports its edge names; guards read inner markings
  through the token.
- **An inner firing counts as a firing of the outer net**, under its `InnerFiring`, so
  `step_count`, `fired_counts`, `RunSummary` and the pacing and fuse limits stay
  coherent without a second counter. (The protocol runner keeps progress per net,
  PROTOCOL.md §5.4.)
- **A stepping rule per place, not per net** (since 2026-11-21): `stepping=` on a nested
  place, default `"inner_first"`. It replaced the net-wide `deep_policy` and
  `passive=True`, removed in 0.4.0; the rule and what it replaced are in
  `../petritype-rs/dev-docs/subnet-place-spec.md` §1.3 (and its principle in §0). The
  transit city states `"outer_first"`, the order its guards were written for.
- **Inner events are wrapped in one `Nested` record**, not a path field on every record
  (the reason is in `petritype/core/events.py`).
- **A failing body's changes inside the nets it holds are not rolled back.**
  `restore_tokens_on_failure` conserves the outer tokens only.
- **Every net is closed unless it declares itself open, and `reaches` is enforced**
  (built 2026-11-27 on Oleg's word; from 2026-11-23 only a net that marked a port was
  closed, and `reaches` was a label nothing checked). The code and the drawing of a net
  must not disagree, so every way tokens enter a net from outside is a port on its
  drawing, and every way a body reaches into a token's net is named on its edge. Hence a
  top-level net is closed too, a body writes no port of a net inside the one it holds,
  which no edge could name, and the knobs and whole-state loads of a held net stay with
  the program. `reaches` stays an upper bound, since nets arrive in tokens as the net
  runs and need not have every port named.
- **A closed net shields the nets inside it** (built 2026-11-27, Oleg: "unexpected
  skipping sounds bad"). The program and the runner write no place of a net inside a
  closed net, a subnet or a net a token carries, at any depth, its ports included,
  unless every net around it is declared open; the inner net's own rule applies on top.
  The drawing of a closed net must show every way a token comes into it from outside,
  and addressing the inner net skipped it: in the example it was decided on, a pot put
  into a workshop's kiln through `NestedNets.subnet_scope` or an inject by path skipped
  the workshop's drawn route `Yard -> Inspect and Load -> Hatch`. `open` is the
  override, chosen over a new way to expose an inner port because it already exists in
  the API, on the wire and on the drawing. It is coarse, since it opens the outer net's
  own places too; the fine way is a port on the outer net and a transition that carries
  the token in. A body is judged by the same rule wherever no edge covers it: at a net
  no edge handed it, and at any net inside a closed net it holds, an open one included
  (found by the tests' author, 2026-11-27: a body reaching a net by a closure wrote what
  the program was refused). The net a body holds is where the looking stops, the body
  being on that net's side of whatever is around it. A net keeps a note of
  the net and place it was last seen at rest in, checked against that place, rather
  than a reference to the net around, so that no net keeps the one around it alive.
- **A question changes no net** (built 2026-11-27, Oleg's go-ahead): a guard, a priority,
  a key function, a selector and an output distribution function are refused every
  change to every net, their own and one they built included. Stricter than holding a
  guard to an edge's `reaches`, and simpler: a drawing shows a guard as a condition, so
  there is no reach to permit. The second reason is the engine's own: these run between
  planning a firing and making it, where a moved token could already be promised. A
  refusal fails whatever asked rather than reading as "cannot fire", which would hide
  it; the protocol runner, whose contract is that a raising guard cannot fire, says it
  on stderr instead.
- **Net parameters** (`graph.params`) exist because the inner nets needed them and the
  Rust port already had net-wide parameters, so a saved net and a `set_param` without a
  transition mean the same in both runtimes.
- **Templates are a process-wide registry by name**: Python has no builder to hang a
  template on, and a token restored from JSON knows its template by name alone.
- **Every copy of a net is a new net**: a deep copy mints a new `net_id`, so a fork, or a
  snapshot-edge copy, is told apart from its original. A copy that must share no Python
  objects at all is `NetJson.restore(NetJson.save(net))`.
- **Sink transitions fire.** A transition with no output edge returns `None` and
  deposits nothing; the transit example needed it.
- **An example runs its process on its own**, by plain stepping, never a net plus a
  script that makes it work. The transit net was redesigned on 2026-10-10 for this: its
  first form needed `Alight`, `Depart` and `Arrive` held back and fired by hand, and under
  plain stepping it ping-ponged `Alight Depot` / `Board Depot` for ever. A vehicle's state
  is now where the door tokens in its cabin are (the module docstring).
- **Where the transit example departs from the proposal**: built as worded, the cabin
  ping-pongs between `Go To Door` and `Sit` and never reaches "three at the door,
  announcement cleared", so passengers at the door are not re-seated and the announcement
  stands until the last passenger has alighted; and one `Go To Door` drawing from either
  of two places is two transitions, since an input edge draws from one place.

## Subnet places

A place may hold a net of its own for the whole life of the net around it, reached only
through the subnet's port places (2026-11-21, from
`../petritype-rs/dev-docs/subnet-place-spec.md` §1 and §5; wire side PROTOCOL.md §12.7).
Where to look: `SubnetPlaceNode` and `Port`, `NestedNets.held_subnets` and `subnet_scope`;
example `examples/subnet_places/levels.py`; tests `tests/test_subnet_places.py`.

- **One place, two views, and nothing moves.** The outer net's maps key a port by its
  `Port` pair and point at the subnet's own `ListPlaceNode`, so both nets use one token
  list and one id list. The Rust core tried moving a port's queue into the outer net for
  the length of a firing and gave it up; Python never moves it.

## Open / not yet implemented

- A guard, a priority, a key function, a selector or an output distribution function
  that edits a place's list, `params` or `disabled` directly is not caught: the rule that
  a question changes no net holds through the library's functions only, and comparing
  the net before and after each guard, as is done for a body, would cost every sweep.
- Python's limit: a body that edits a port's list directly, writes from a thread it
  starts itself, or reaches a net through a closure rather than an edge is not caught,
  since the check after the body sees only private places, and a hold and its reach are
  known only on the nets an edge hands over, in the body's own context.
- The Rust library has built none of these rules yet: closing every net not declared
  open, holding a body to its edge's `reaches`, refusing a question every change, and a
  closed net shielding the nets inside it.
- A net that code puts into a place's list directly is not known to be inside that
  place's net, and so not shielded, until an engine entry point re-ids the place; one
  put in place of another, the length kept, is not known at all.
- The knobs of a net inside a closed net are still the program's (decided 2026-11-27:
  a parameter puts no token anywhere). A load into such a net is refused, as a put is.

---

# Groups

Decided 2026-11-21 in `../petritype-rs/dev-docs/group-spec.md`, for both runtimes; the
wire side is PROTOCOL.md §5.1. A group is a named set of places and transitions of one
net, set in the net's definition, that only drawings read: `group=` on the nodes,
`ExecutableGraph.groups`, and the clusters of `RustworkxToGraphviz.dot_source`. Example:
`examples/groups/hike.py`; tests `tests/test_groups.py`. It replaces the viewer's clusters
by name prefix: no character in a name has a meaning, so nothing is found by splitting
one.

A net whose rules all read and change one shared place cannot be split into subnets
without moving those rules into the net around them, which leaves the first view no
simpler. Such a net stays flat and is read by gathering its parts under names. The two
are kept apart in name, in the definition and in the picture:

| | Group | Subnet place |
|---|---|---|
| What it is | a label on parts of one net | a separate net held by a place |
| Token logic | the flat net's, unchanged | its own net, its own ids and turns |
| Shared places | yes, freely | no, only ports |
| What it is for | reading a large flat net | a part that is really separate |

None of the spec's build errors can arise in Python: a node names one group, so it is
never in two; a group is its name, so two of one name are one group; there are no group
handles to bring from another net; and an outer net has no node for a port to put in a
group. Closing a group in a drawing is the viewer's, not this library's.

---

# Token keys, queries and filters

Decided and built 2026-10-09 from
`Cyberpunk2020-Simulation/dev-docs/TOKEN_QUERYING_AND_FILTERING.md` (the consumer's
proposal; its "As built (Python)" section records what differs). The wire side is
PROTOCOL.md §13. A place derives *keys* from each token's value; one mechanism then gives
queries, viewer filtering, the protocol's projections and keyed input edges. Where to
look: `petritype/core/keys.py` (`Keys`, `Clause`, `Filter`, `Query`), `KeyedEdges` and the
keyed edges in the engine module, the `filter` argument of the Graphviz renderer. Example:
`examples/token_view_filtering/wildlife.py` with its notebook `wildlife_filter.py`.

- **No index.** The proposal asks for an index per key and value, kept in step on
  `Added`, `Removed` and `Mutated`. Not built: Python's tokens are plain objects in plain
  lists, moved by the engine at several points, and a scan is cheap at this library's net
  sizes.
- **A keyed edge takes only the token it needs**, the oldest one its filter keeps, so the
  other tokens are never removed and re-added and keep their ids. It replaced
  take-all-and-put-back.
- **A filtered drawing says how many tokens it hides**, so a hidden token is never
  mistaken for an absent one (the proposal's rule for the Bevy viewer, applied to the
  Graphviz renderer). The tabs and filter bar are the viewer's; a notebook builds its own
  controls from `Query.distinct`.

Open: an index per key and value, behind the same `Query` functions, only if profiling
asks for one.

---

# Input edges that accept an empty place

Decided and built 2026-10-14 from `../petritype-rs/dev-docs/EDGES_ON_EMPTY_PLACES.md`
(the Cyberpunk2020-Simulation game's proposal, revised after the owner's review; its "As
built (Python)" section records what differs). The wire side is PROTOCOL.md §5.1.
`DrainEdge` takes every token of its place and `SurveyEdge` copies every token, zero or
more either way; `ReadAllEdge` (2026-11-23) hands over every token live to look at. README,
"Edges that accept an empty place"; examples `examples/edges_on_empty_places/shop.py` (the
survey) and `traffic_light.py` (a drain beside a plain read); tests
`tests/test_empty_place_edges.py`.

- **Why.** Every other input edge holds its transition back until its place has a
  token. That is right for a token the transition works *on*, and wrong for a place it
  only needs to *look at* when something is there ("every promotion running", where none
  is a good answer), which used to need an "empty twin" transition guarded on the place
  being empty, one more per such place.
- **An aggregate stays one token holding a list.** These edges are for members that must
  be tokens of their own.
- **One mechanic per example.** The proposal's net, a shop that priced orders with the
  promotions running and drained them all at the close, was split on 2026-10-16 into the
  shop and the traffic light, since "draining promotions" read badly.
