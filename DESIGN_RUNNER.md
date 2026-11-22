# The protocol runner and the library changes behind it

Status: **design of 2026-10-03; token ids, events, the runner's state
machine and its process layer built the same day** ("As built" sections
below). `python -m petritype.runner` works and the ten shared transcripts
replay against it. It speaks protocol version 2 since 2026-11-21, subnet
places included (the last three sections). Version bumped to 0.2.0 on
2026-10-03, and 0.2.0 shipped on 2026-10-04 (the `v0.2.0` tag). The protocol
itself is [`PROTOCOL.md`](PROTOCOL.md)
(<https://github.com/olenive/petritype/blob/main/PROTOCOL.md>); this note
covers only what changes in this library to speak it. House conventions
apply: namespace classes of functions, passive dataclasses and pydantic
models for state, flat `tests/test_*.py`.

## Decisions of 3 October 2026

### The net-process protocol moves into the library

**Decided 2026-10-03.** The NDJSON protocol that petritype-server's
`worker/net_runner.py` speaks today becomes an optional module of this
library, `petritype.runner`, run as `python -m petritype.runner`.
petritype-server imports it instead of owning its own copy.

Why: any viewer can then host a Python net by spawning a process, with no
server. The Bevy visualiser is the first such viewer; a test harness or a
notebook can be the next. The protocol also stops being defined by one
consumer's code.

### Stable token ids and consumed/produced per firing, in the library

**Decided 2026-10-03.** `ExecutableGraph` gives every token a monotonic
integer id when it enters a place and reports, per firing, which ids were
consumed from which places and which were produced into which, matching the
Rust crate's `TokenId` and `Event`.

Why: one source of truth. Today the server's `engine.py` diffs
`id(token)` before and after each step. A memory address is reused once a
token is freed, two equal small integers share one address, and the diff
cannot tell a token that moved from one consumed and one produced. The
engine knows exactly what it took and what it put; it should say so.

### Layout leaves the net process

**Decided 2026-10-03.** The runner sends topology and optional position
hints, never coordinates it computed. grandalf is not a dependency of the
library or of the runner.

Why: every runner in every language would otherwise have to ship a layout,
and the viewers already have (Bevy) or are getting (petrivelte) their own.

### "Fireable" for can-fire-now, "enabled" for the switch

**Decided 2026-10-03.** `ExecutableGraphCheck.transition_is_enabled` became
`transition_is_fireable`, matching the Rust port and the protocol; the old name
stays as an alias that emits `DeprecationWarning`. "Enabled" now means only
the `Enable`/`Disable` switch.

## What the library gains

### Token ids

- `ExecutableGraph` gains `next_token_id: int` (starts at 1) and each
  `ListPlaceNode` a private list of ids parallel to `tokens`
  (`PrivateAttr`), so `place.tokens` keeps its type and every existing
  reader keeps working.
- Ids are assigned at: graph construction (seed tokens, in place order),
  stage 3 deposits, `Extend` and `SetTokens`. Stage 1 pops ids with tokens;
  `restore_tokens_on_failure` puts the same ids back at the front. A read
  through a `SnapshotEdge` changes nothing; a `MutateEdge` keeps the id and
  records a mutation. Under `allow_token_copying`, a copy deposited
  elsewhere is a new token with a new id.
- Code that assigns `place.tokens` directly (tests, old notebooks) bypasses
  the id list. The engine checks the two lengths at each entry point and, on
  a mismatch, re-ids that place and records a resync, which the runner turns
  into `reloaded {reason: "resync"}`. Correct, if blunt; direct assignment
  is already discouraged by the "the runner is the only writer" rule.

### Events

- `ExecutableGraph` gains `record_events: bool = False` and an event list
  with `drain_events()`, the Python counterpart of Rust's `Net::drain_events`.
  Off by default so that a program that never drains does not grow a list
  for ever; the runner turns it on.
- Event records are passive dataclasses: `Fired(transition, consumed,
  produced, duration_ms)`, `Failed(transition, consumed, restored, error)`,
  `Added`, `Removed`, `Mutated(place, token_id)`, `Toggled(transition,
  enabled)`, `ParamChanged(transition, param)`, each with `(place_name,
  token_id)` pairs. Python emits no `Launched`/`Distributed`: a Python
  firing is reported once, when it completes.
  *2026-10-04:* that stays true of the engine's events, but the runner now
  reports every firing in two pieces on the wire, `launched` when it hands
  the firing out (the tokens stage 1 will take, predicted from the edges and
  checked against `Fired`/`Failed` on landing) and `distributed` or `failed`
  when it lands, as the Rust runner does. One piece hid the body: a viewer of
  a slow Python body saw nothing but heartbeats until it landed, while the
  owner already knew the transition was in flight.
- The engine appends `Fired` and `Failed` in `fire_one_transition` and in
  the concurrent deposit loops; `Added`/`Removed` come from command
  application; `Toggled` and `ParamChanged` from `_apply_command`, because
  the switch lives on `RunContext.disabled` (runner-level, as since 0.1.3),
  not on the graph. That stays: `enable`/`disable` over the protocol
  enqueue the existing `Enable`/`Disable` commands and drain them.

### As built, 2026-10-03

Token ids and events landed as designed, with these settlements:

- `ParamChanged.param` is one keyword name, one event per key, matching the
  protocol's `{param, transition?}`.
- A stage-3 failure (a wrongly typed element of a routed tuple) records
  `Failed(restored=False)` after an `Added` for each token already placed,
  and the original `TypeError` still propagates as before.
- In concurrent mode a `Mutated` is emitted only if the token is still in
  its place when the firing completes; a peer may have consumed it.
- `construct_graph(..., record_events=False)` carries the flag.
- Resync runs at the start of a firing, of command application, of each
  concurrent launch sweep, and inside the id lookup. It compares lengths
  only: a same-length replacement of `place.tokens` keeps the old ids, a
  place object shared by two graphs is re-seeded by the graph built last,
  and a body that resizes a place through a whole-list mutate edge is
  repaired at the next entry point. All three are the price of keeping
  `place.tokens` a plain list.
- The events are `petritype/core/events.py`; the id bookkeeping is the
  `TokenIds` namespace class; a firing's events go through `GraphEvents`
  and `ExecutableGraphOperations.deposit_and_record`, which both execution
  modes share, so every firing is reported exactly once.

### What the runner reads from the graph

| protocol field | source |
|---|---|
| `transitions[].enabled` | `name not in ctx.disabled` |
| `transitions[].fireable` | `ExecutableGraphCheck.transition_is_fireable`, enabled, and not in `graph.in_flight` |
| `transitions[].in_flight` | `graph.in_flight` (set in both modes since 0.1.2) |
| `transitions[].priority` | `t.priority(graph)` if set, else 0.0; omitted if it raises |
| `transitions[].detail` | `function_name`, `signature`, `docstring` by introspection (moved from the server) |
| `progress` | `step_count`, `last_fired`, `fired_counts` (already on the graph) plus `last_progress_at` kept by the runner |
| token `label`, `color` | pydantic `model_dump_json` or `repr`, cut at 1000 characters; `.color` resolved to hex (moved from the server) |
| `places[].serializable` | the place type is a pydantic model, or a JSON scalar or a subclass of one |
| edges | argument edges (`take`, or `take_all` for a `list[...]` argument; `take_where` for a `TakeWhereEdge`, `drain` for a `DrainEdge`), `snapshot_edges` (`snapshot`, or `survey` for a `SurveyEdge`), `mutate_edges` (`mutate`, `mutate_where`), return edges (`slot` from `return_index`) |

## The runner module

- `petritype/runner/` with `__main__.py`. Standard library plus pydantic,
  which the library already requires, so no extra is needed; if one becomes
  necessary it is `petritype[runner]`.
- `python -m petritype.runner --module pkg.nets [--module ...]` imports the
  modules, collects `@petri_net` factories by name, and serves `describe`
  and `load {net, params}`. `load {module, function, factory_params}` (the
  version 0 form) is accepted too. `--describe` prints the `describe` answer
  and exits.
- What moves here from petritype-server's `net_runner.py` and `engine.py`:
  the request loop and dispatch, the stdout guard (`sys.stdout` swapped for
  `sys.stderr` before user code is imported), the heartbeat thread, the
  sequential run loop with its 24/7 idle polling, forcing synchronous bodies
  onto threads, `inject` decoding, the log ring, and the two-snapshot rule
  that keeps `get_state` and `get_place_tokens` from ever waiting on a
  running body. What does not: layout, the `id(token)` diff, the
  `"Type@timestamp_hash"` log ids, argument-name log keys.
- The runner speaks one version of the protocol, the current one: version 1
  until 2026-11-21 and version 2 since (the last section). Translation for
  version 0 clients lives in petritype-server's worker for one release (see
  its design doc), so the library never carries the old shapes.

### As built, 2026-10-03: the state machine

`petritype/runner/` has `protocol.py` (the wire shapes as pydantic models;
optional fields omitted, never `null`, except the few the protocol lists as
nullable), `registry.py` (entries from `@petri_net` factories, `describe`,
the version-0 `load` resolver), `translate.py` (labels, colours, type names,
`serializable`, `inject` decoding, `detail`, topology by names) and
`owner.py`. The owner mirrors the Rust runner's: `handle(request, now)`,
`completed(landing, now)` and `tick(now)` each return the lines to write, a
firing for the caller to await, and an exit flag. It creates no tasks or
threads and reads no clock but `now`, so the whole protocol is tested by
feeding it inputs (50 tests), and the process layer and the transcript
harness will drive it the same way.

Settlements:

- **Reads while a body runs** are served from a snapshot of the marking taken
  at launch, not from the live graph. Sync bodies run on a thread (every sync
  transition is set to `execution="thread"` at load, as the server did), and a
  thread is not bound by await points: through a mutate edge it can change a
  token, or a whole live list, while a read iterates it. The snapshot is a
  shallow copy of each place's token and id lists plus the labels of the
  tokens handed to the body, rendered before it starts. Counts are true, any
  page works, the airborne transition shows `in_flight`. Since 2026-10-04
  the snapshot is of the marking `launched` describes: the taken tokens are
  gone from it, and only mutate-edge tokens are rendered early. Cost is one shallow
  copy of the marking per firing, in place of the server's capped-plus-full
  snapshots refreshed after every mutation. A later refinement could copy
  only the places the body can touch (its mutate edges).
- `get_token` for a token the airborne body holds through a mutate edge
  waits for the landing; every other id is answered at once, and a token the
  body took is in no place, so `unknown_token` (as the Rust runner answers).
- `duration_ms` comes from the engine's own `Fired.duration_ms`; the owner
  never reads a clock.
- `set_param` without `transition`, or naming a keyword the function does
  not take or an edge already supplies, is `invalid_params`; `unsupported`
  is never used, since Python can always do it.
- Load parameters and `set_param` values are validated against the factory's
  annotations with a pydantic `TypeAdapter`; a failure is `invalid_params`.
  Scalar `inject` is strict: no `bool` where a number is expected, no
  truncation of `1.5` to an int.
- Version-0 `load`: a missing module or function is `unknown_net`; a module
  that raises on import is `load_failed`.
- `load_progress` goes out once, after the parameters bind and before the
  factory runs, so a refused `load` sends none.
- Only the canonical decimal spelling names a token (`"01"` is
  `unknown_token`).
- A selector or guard that raises makes `step` answer `internal`, and stops
  a run with `execution_stopped {reason: "transition_failed"}`.
- An unknown or missing `.color` is omitted, not given the server's grey.
- **Strict decoding, 2026-10-03.** Pydantic's default lax mode coerced
  `"9"` to `9` at the three wire boundaries (`inject`, `set_param`, load
  parameters), and the scalar `inject` path did the same through the
  constructor; Oleg ruled this out. Models now validate with
  `validate_json(..., strict=True)`, JSON semantics rather than Python's,
  because strict Python mode would refuse the only spellings JSON has for
  a date-time, a tuple or a set; scalars require the exact JSON type, with
  an integer accepted for a float as strict JSON mode does. Pinned in
  `tests/test_runner_strict_input.py` and by a shared transcript exchange
  both runtimes answer with `invalid_token`.
- `TokenIds.resync` also runs at each landing, so a body that resizes a
  place through a live list shows up as `reloaded {reason: "resync"}` in the
  same batch.

### As built, 2026-10-03: the process layer

`petritype/runner/process.py` with `__main__.py`. `python -m petritype.runner
[--module M ...]` serves until `shutdown`, end of input, SIGTERM or SIGINT;
`--describe --module M ...` prints one line and exits 0; a missing module,
an import error or a bad argument exits 2 with the message on stderr.
`--module` is optional when serving, since the version-0 `load` form names
its module itself.

Settlements:

- **Start-up order**: parse arguments; flush and `dup` descriptor 1 as the
  protocol stream (not inherited by children); `dup2` stderr onto
  descriptor 1 and set `sys.stdout = sys.stderr`; only then import the
  user's modules. A `print` at import time or in a body, and a C extension
  writing to descriptor 1, all land on stderr.
- **Writer**: a lock-held object, not a thread. One `write` and one flush
  per batch (the lines of one owner call), so a heartbeat never splits a
  request's notifications from its response.
- **Heartbeat** never touches the owner. After each owner call the loop
  snapshots `loaded`, `marking_version`, `running` and `status` into a
  frozen value handed to the writer with the batch; the heartbeat thread
  writes from the latest one under the writer's lock. It therefore keeps
  going while an async body blocks the loop, which a round trip through the
  loop could not. The same thread posts the owner's `tick` every period.
  Period 30 s, `PETRITYPE_RUNNER_HEARTBEAT_MS` overrides for tests.
- **Reader**: `os.read` on descriptor 0 with its own line splitting, because
  a daemon thread holding `sys.stdin`'s lock can make interpreter shutdown
  fatal. Lines of 17 MiB are tested.
- **Exit with a body still running**: flush, then `os._exit(0)`, because a
  normal exit would join the executor thread and `asyncio.run` waits up to
  300 s for it. `shutdown` is answered promptly even with a 60 s body.
- A broken stdout pipe stops the loop and exits 0.
- Known gaps: `json.dumps` still allows `NaN`/`Infinity`, as the server did;
  no signal handlers on Windows and the descriptor redirect is untested
  there.

### As built, 2026-10-03: the transcripts

`tests/test_protocol_transcripts.py` replays every `tests/protocol/*.json`
through `Owner` (awaiting each firing inline) and one of them over pipes
against the real process, with the matching rules now written into
PROTOCOL.md §11 (`<any>`, `<unordered>`, `<id:NAME>` were added for them).
The conformance net `tests/protocol_nets/parcel_distribution.py` is the
Rust example copied exactly, because the Python original under
`examples/toy/` creates parcels from nothing, uses wall-clock guards and
randomness, and cannot be replayed. A second shared-safety test asserts
each transcript masks what differs by runtime and carries a `color` only
where both runtimes give the same hex, lowercase `#rrggbb` (since
2026-10-16; until then it carried none).

Rust-side follow-ups, done on 2026-10-03: the Rust runner example registers
the conformance net under the name `parcel-distribution` and the same
description, marks its four places serializable and derives
`Serialize`/`Deserialize` on `Parcel`, and both runtimes replay the shared
set (PROTOCOL.md §11). `not_serializable` is covered by this library's own
tests (`tests/test_runner_owner.py`); covering it in the shared set needs a
second token type in a conformance net, since Python decides
serializability per type.

## Release

A minor version (0.2.0): everything above is additive for library users.
`record_events` off and ids kept beside the tokens mean an existing program
sees no change. The protocol transcripts under `tests/protocol/`
(`PROTOCOL.md` §11) are the runner's acceptance tests.

*2026-10-17:* the work from 2026-10-09 to 2026-10-16 (nested nets, keys, drain
and survey, notebooks, controls and `press`) shipped as 0.3.0, and 0.3.1 the
same day added the launch notification for sequential observers
(`CHANGELOG.md`).

*2026-11-22:* the work of 21 and 22 November (a stepping rule per place,
protocol version 2 with paths as lists, subnet places and groups) goes out as
0.4.0, a minor version whose removals the changelog names (`CHANGELOG.md`).

## As built, 2026-10-09: nested nets (PROTOCOL.md §12)

The owner serves a tree of nets, not one: the outer net the host loaded and,
inside the tokens of its `nested` places, inner nets at any depth
(`PROPOSED_DESIGN_GOALS.md`, "Nets as tokens"). `petritype/runner/owner.py`
was reworked so that every method takes the net it acts on, a `_Net` record
(`_Loaded.outer`, and `_Loaded.inner` by `net_id`, built when an inner net is
first addressed or fired); nothing is duplicated per level.

Settlements:

- **Addressing.** `path` or `nested_id` on `get_state`, `get_place_tokens`,
  `get_token`, `get_log` and `inject`; both is `invalid_params`, neither is
  the outer net. A path is the loaded net's name then `place/#id/net` per
  level; a path or id that names no live net is `unknown_net` naming the
  failing segment. The library resolves paths (`NestedNets.resolve`) and
  finds nets by id (`NestedNets.find`); the owner only adds the net name in
  front. `enable`, `disable`, `set_param`, `start`, `stop`, `reset` and
  `step` refuse a path with `invalid_params`: they act on the outer net.
  *2026-11-21:* a path is a list of steps now, resolved by the owner one step
  at a time (the last section).
- **Versions are per net.** Each `_Net` carries its own `marking_version`,
  raised by its own state notifications, stamped on its reads. An inner
  notification is the line the inner net would write, wrapped in `nested
  {path, nested_id, event}` with the outer version, unmoved, on the wrapper
  (`Owner._wrap`). The engine delivers inner events to the outer log already
  wrapped (`Nested` records with the path as it stood), so `_flush` is one
  loop; `fireable` goes out for the outer net and then for each inner net
  the batch touched.
- **Driving.** `_select` asks the outer net's selector first and, when it
  picks nothing, descends in the library driver's order (place, token, net; a
  net's own inner nets before the next token), each net by its own selector
  (the default deep-step policy; the interleaved one came on 2026-10-12, below).
  A firing in an inner net goes through `fire_one_transition` on that net
  with a pinned selector and `descend=False`. On landing the outer net counts
  it under its path (`step_count`, `fired_counts["Depot/#17/cabin/Sit"]`,
  `last_fired`), as the library driver does, so `progress` means the same
  whichever drove the net. `execution_stopped {no_fireable_transitions}` now
  means nothing fireable anywhere. *2026-11-21:* the outer net counts an inner
  firing no more; progress is per net, and the driver makes one deep step by
  the stepping rules of the places (the last two sections).
- **Reads during a body** keep the snapshot rule and add two cases. An outer
  body may reach into the inner nets of the tokens it took or holds through a
  mutate edge, so a read addressed to one of those waits for the landing
  (`_InFlight.held_nets`, `held_tokens`, computed at launch by walking those
  tokens); every other inner net is read live, since nothing else can change
  it. During an inner body, the firing net is read from its snapshot and each
  net on the path above it from a snapshot with the holding token rendered
  eagerly (its label embeds the inner marking the body is changing), so a
  `get_token` on that token waits too.
- **Log.** One ring; an inner firing's entry carries `path` and `nested_id`
  and, internally, the chain of nested ids down to it, so `get_log` with a
  path or id filters to that net and the nets inside it.
- **`set_param` without `transition`** now sets a net parameter
  (`graph.params`, through the new `SetNetParam` command): it must be
  declared, and keeps the JSON type of its current value. Before 2026-10-09
  this was `invalid_params`; the Rust runner's net-wide `set_param` and the
  saved form's `params` now mean the same in both. *2026-10-12:* a parameter
  the factory annotates decodes by that annotation instead (below).
- **Inner nets' sync bodies** are moved to a thread when the owner first sees
  the net, as the outer net's are at load.

The shared transcript `tests/protocol/05_nested_nets.json` steps the transit
journey over the wire on the `transit` conformance net
(`tests/protocol_nets/transit.py`, the example itself) and replays through the
owner and over pipes; `tests/test_runner_nested.py` covers what a transcript
cannot show (the waits and the snapshots). The harness gained the `<path>`
mask, captures inside longer strings and the nested fold, and replays each
transcript against a registry reduced to its own net (PROTOCOL.md §11). The
first two went on 2026-11-21, with paths as text (the last section).

**Revised 2026-10-10.** The transcript first ran the design's acceptance script,
holding `Alight`, `Depart` and `Arrive` back with `disable` and enabling each
where the script fired it by hand. The transit net was redesigned to run its
own journey (each vehicle's state is where the door tokens in its cabin are,
`Doors Open` or `Doors Closed`, which the outer guards read; PROTOCOL.md §11),
and the transcript now steps it from the seed until all seven passengers have
arrived, with nothing disabled, then injects, reads, sets the `destination`
override and follows a departure to it.
It was recorded, not written: `tests/record_transcript.py` runs a list of sends
against `python -m petritype.runner` over pipes, folds and masks what comes back
as §11 asks (every token id and nested id a named capture, `bus`,
`bus_ada_seats`, `bus_cabin`), and writes the transcript; given a transcript it
re-records it from its own sends (`uv run python tests/record_transcript.py
tests/protocol/05_nested_nets.json -o tests/protocol/05_nested_nets.json`).
Recording it showed one runner detail worth knowing: an inner net the owner
had not yet seen got its first `fireable` baseline when it was first touched,
after the firing, so the firing that first touched it reported no `nested`
`fireable` for it; the transcript reads both cabins before the first step,
which kept that out of what both runtimes must agree on. Since 2026-10-12 the
baseline is taken when the owner first knows of the net (below), as the Rust
runner does, and the transcript replays unchanged.

## As built, 2026-10-09: keys, queries and the connection's projection (PROTOCOL.md §13)

Keys (`PROPOSED_DESIGN_GOALS.md`, "Token keys, queries and filters") reach the wire as
`key_names` on a place and `keys` on a token entry, on `added` and `mutated`, and as the
fifth element of a `produced` entry (with `null` in the colour's place when the token has
none). `query`, `distinct` and `set_filter` are three more methods; `hidden` one more
state notification. `Filters.from_wire` (`translate.py`) reads a filter off the wire into
the library's `Filter`, answering `invalid_params` for a shape that is not one and
`unknown_place` for a place not of the net.

Settlements:

- **One projection per net per connection.** `set_filter` sets `_Net.filter`; the owner
  then serves that net through it: `_PlaceView` keeps the indices the filter lets through
  (`kept`), so `get_state` and `get_place_tokens` list and page those alone and report
  `matching` beside `count`; `get_token` on a hidden token is `unknown_token`; `query`
  without a filter uses the connection's, with one uses that alone.
- **What the connection has been shown.** `_Net.visible` holds the ids of the live tokens
  the filter keeps. The token events of a batch keep it in step as they go (an `added` that
  matches joins it, a `removed` leaves it, a `mutated` that crosses the boundary becomes an
  `added` or a `removed`), and `hidden {kind, place}` goes out for what they hide, so the
  version moves exactly as it would unfiltered. After each batch `_sync_visible` recomputes
  the set from the live marking and announces any difference as `added` or `removed`: that
  is how a change of a net parameter a `{key, param}` clause names brings tokens into or out
  of view without any token event, right after its `param_changed`. `set_filter` itself
  announces nothing: the client changed the projection and refetches.
- **Firings** list only kept tokens: `launched.consumed` is filtered against the live tokens
  before stage 1 takes them, `distributed.produced` against the tokens just placed.
- **`query` and `distinct`** are reads: answered while a body runs, from the same snapshot
  as `get_state`, so they say which version they reflect; a projection on an inner net is
  set by naming it (`path` or `nested_id`), and the outer net's is unaffected.
- **No index.** Keys are derived on demand (`Keys.of`), so a `mutated` token's keys are
  whatever its value says now; `query` scans. The design's index per key and value is a
  Rust-side performance measure; this runner's nets are small enough to scan.

The shared transcript `tests/protocol/06_keys_query_filter.json` asks the design's five
questions over the wire on the `wildlife` conformance net (`tests/protocol_nets/wildlife.py`,
the example itself), sets a projection, turns `season` to summer and back, and watches through
it a keyed spread with its copy, a hidden decline, three keyed migrations, a settling and two
injections (re-recorded on 2026-10-13 for the net's second edition, PROTOCOL.md §11);
`tests/test_runner_keys.py` covers the rest (the mutations that cross the boundary, the
parameter-driven changes, a filter on an inner net).

## As built, 2026-10-12: the protocol's wording gaps and the scheduling policies

PROTOCOL.md was amended on 2026-10-12 to close eight gaps in its wording (the
decisions A1 to A8 of that day, shared with the Rust runner); where the runner
did not yet do what the words say, it changed. The engine gained two
scheduling policies at the same time (`PROPOSED_DESIGN_GOALS.md`, "Ordering is
intentional"), which the runner honours.

Settlements:

- **The fold (§11).** `fold()` in `tests/test_protocol_transcripts.py` folds a
  `launched` and the next `distributed` of the same transition across the
  lines the firing's own body causes (`BETWEEN_PIECES`: `nested`, `mutated`,
  `hidden`, `added`, `removed`), which keep their places before the `fired`;
  inside `nested` wrappers likewise per inner net. Both the expected and the
  actual lines are folded, as the Rust harness folds them. A failed firing is
  not folded.
- **`inject` into an inner net** answers `{status, id, marking_version, path,
  nested_id}`, with the inner net's version (§12.3).
- **`step` is outer-only** (§12.3): it was already in `OUTER_ONLY`, so a
  `path` or `nested_id` on it was already `invalid_params`; now pinned by a
  test, as is `invalid_params` for an undeclared `set_param` name (§6.4).
- **`null` counts as absent** (§2): `Requests.null_as_absent`, the one helper,
  drops every `null` field of a request's params when it is parsed, except the
  two whose `null` is a value (`inject.token`, `set_param.value`:
  `NULL_IS_A_VALUE`). A required field given `null` is then missing, which is
  `invalid_params` as before.
- **`failed.consumed` under a filter** (§13.4) lists what its `launched`
  listed (`_Net.launched_shown`); a restored token goes back into the set the
  connection has been shown, so `_sync_visible` no longer announces it as a
  fresh `added`.
- **Baselines** (§7.2, §12.3): `Owner._know_new` builds the `_Net`, and so
  takes the `fireable` baseline, of every inner net at any depth the owner does
  not know yet, at the end of `load` and `reset`, after an `inject` and at each
  landing; a net with events in a batch is still known from that batch, after
  them. The firing that first touches a seeded cabin now reports its `nested`
  `fireable`.
- **Parameter values by their declared type** (§6.4): a net parameter decodes
  by the annotation of the factory parameter of the same name
  (`NetEntries.adapter`, strict JSON as a load parameter is), and, when the
  factory does not annotate one, by the JSON type of its current value as
  before. `NetEntries.params_of` now evaluates annotations
  (`inspect.signature(eval_str=True)`), so a module written with `from
  __future__ import annotations` is checked too; one that will not evaluate
  stays a string and passes unchecked. The transit factory declares
  `destination: str | None = None`, so the override takes a place name or
  `null` in either order and can be cleared over the wire. That makes
  `destination` a load parameter of `transit` as well, which `describe`
  lists (not required, default `null`); PROTOCOL.md §11 says so, and the Rust
  conformance net must declare the same for the shared transcript to replay.
- **The scheduling policies.** The tie-break needs nothing of the runner: it
  chooses with the net's own selector (`_select_in`), so the default selector
  moves the cursor of the net that fires and no other, on every pick under any
  tie-break (only `"round_robin"` reads it), and a
  disabled transition, not being fireable, is never a candidate. `reset` and
  `load` build a new graph, so its cursor starts afresh. `_select` reads the
  loaded net's `deep_policy`: under `"outer_first"` it is what it was; under
  `"interleaved"` it goes round `NestedNets.deep_turns` (the library's cycle,
  starting after the library's cursor) and asks each net's selector in turn.
  Under either policy it records the net it chose with `NestedNets.took_turn`
  as it launches, as the library does, so a `deep_policy` switched to
  `"interleaved"` mid-run resumes after the net that fired last. `step` and a
  running net share `_select`, so both honour it. Nothing on the wire changes.
  *2026-11-21:* the stepping rule per place replaced `deep_policy` (the last
  section).

The shared transcript `tests/protocol/05_nested_nets.json` was re-recorded
with `tests/record_transcript.py` for two lines: `describe` now lists
`destination`, and the inner `inject` answers with its `path` and
`nested_id`. Being recorded with the new fold, its firings read as `fired`
lines now where they read as `launched` and `distributed` pairs around their
bodies' lines before; both harnesses fold the expected lines too, so either
shape replays. The recorder walked `describe`'s `nets` as if it were a token's
`nets`, which left a parameter's `type_name` unmasked; it now masks it.
`tests/test_runner_protocol_gaps.py` covers A2 to A8 and the policies through
the owner.

## As built, 2026-10-14: edges that accept an empty place (PROTOCOL.md §5.1)

The library gained `DrainEdge` and `SurveyEdge` (`PROPOSED_DESIGN_GOALS.md`,
"Input edges that accept an empty place"): an argument edge that takes every
token of its place and a snapshot edge that copies every token, both zero or
more, neither holding its transition back. The runner needed little for them,
since both are subclasses of edges it already reads:

- **Topology.** `Topologies.of` lists a `DrainEdge` as `kind: "drain"` and a
  `SurveyEdge` as `kind: "survey"` (a drain is tested before the `take_all`
  case, which its `list[T]` argument would also pass); `wire.Edge.kind` admits
  the two strings. Nothing else on the wire changes.
- **`launched.consumed`.** `_handed_over` treats a drain as taking every
  remaining token of its place, so `consumed` lists each car the traffic
  light's `Go` drains, oldest first (and nothing of the light, which it
  reads), and would list none from an empty place; the snapshot a read during
  the body is served from leaves them out. A survey takes nothing and hands
  nothing over live.
- **Fireability** is the library's (`ExecutableGraphCheck.transition_is_fireable`),
  which skips the presence check for both kinds (`requires_presence`), so the
  `fireable` lists and notifications follow with no change here.

Two shared transcripts drive the edges over the wire, each on a conformance
net that is the example itself (PROTOCOL.md §11). Until 2026-10-16 there was
one, on a shop that both surveyed its promotions and drained them at the
close; it was split so that each example shows one mechanic.
`tests/protocol/07_empty_place_edges.json`, on the `shop`
(`tests/protocol_nets/shop.py`), shows the topology with its `survey` edge,
an order priced with no promotion running, two promotions injected without
making anything fireable, an order priced with both, a step that fires
nothing, and the final marking with each output token's value.
`tests/protocol/08_traffic_light.json`, on the `traffic-light`
(`tests/protocol_nets/traffic_light.py`; revised the same day so that the
light is one place whose token is its colour, and again so that the net has
two transitions, `Change` cycling the light and `Go` draining the cars on
green), shows the topology with its `drain` and its plain `snapshot` read of
the light, the light going green and amber with nobody waiting, two cars
arriving at amber and waiting through red, the light going green and `Go`
draining both, a car arriving at green and going straight through, and the
final marking with each car's and the light's value. Both were recorded with `tests/record_transcript.py`, re-record to
the same bytes, and replay through the owner and over pipes.
`tests/test_empty_place_edges.py` covers the rest through the library and
the owner.

## As built, 2026-10-16: net-declared controls and place styles (PROTOCOL.md §5.1, §6.6)

A net declares, beside it, the controls a viewer shows and how its places are
drawn: `@petri_net(controls=..., place_styles=...)`, which `NetEntry` carries
(`controls`, the `{name: ControlSpec}` map or a zero-argument callable
returning one; `place_styles`, `{place: PlaceStyle}`). Both stay off the net,
as `ControlSpec` always was. The runner gained one method, `press`, in
`METHODS` and in `OUTER_ONLY`, and one error code, `unknown_control`.

- **Topology.** At each build (`load`, `reset`) `NetEntries.controls`
  resolves the map, kept on `_Loaded.controls` for that generation, and
  `Topologies.declare` puts `Controls.wire` of it on the outer net's topology
  as `controls` and each style on its place as `style`. A wire `Control` is
  the `ControlSpec` without `to_command`: `node`, `action` and `config` left
  out when absent or empty, and `config.value` for a `button` or a `text`,
  which have no initial value. A kind or an action outside the vocabulary, a
  `node` that is no place or transition, a `config` that is not plain JSON or
  a style on a place the net does not have is `load_failed`. Inner nets'
  topologies carry neither.
- **Dispatch.** `Owner._press` looks the control up (`unknown_control`),
  checks the value by kind (`Controls.value`, `invalid_params`: a button takes
  none and hands `to_command` `True`, as a notebook's clicked button does),
  and calls `ControlSpec.command`, which hands `to_command` the net too when
  it takes a second argument. `None` answers `nothing`. A `Fire` is a pinned
  `step`: `nothing` at once when `_is_fireable` says no (switched off, in
  flight, inputs missing or guard failing), else `_launch` with the press's
  id on `_InFlight.press_id`, so `launched`, `distributed`, the log entry,
  progress and `fireable` follow as for a step, and `_pressed` answers when it
  lands. Any other command goes through `_apply` and `_flush` as an `inject`'s
  does, then `pressed`. A `to_command` that raises, a `Fire` naming no
  transition, or a command the strict context refuses is `internal`. While a
  body is airborne a `press` waits for the landing, as every non-read does.
- **The runtime.** `Fire(transition)` is a command: `Runner.drain_inbox`
  fires that transition once if it is fireable, through `fire_one_transition`
  with a selector pinned to it and no descent, counted against the fuse.
  `drain_inbox` is synchronous and the engine's firing is a coroutine, so the
  firing is run to completion on a fresh event loop, on a helper thread when
  the caller's thread already runs one (`_run_now`); the caller waits, as for
  a synchronous body. The Owner does not take that path: its `Fire` is the
  pinned step above.
- **Colour.** No new mechanism: a token's `.color` is its `color`. The
  traffic light's light is a `Colour(str)`, which `JsonForm` now treats as a
  `str` (a subclass of a JSON scalar has its scalar's form, built by its own
  constructor), so its JSON form stays `"red"`.

`tests/protocol/08_traffic_light.json` was re-recorded with six presses
appended (PROTOCOL.md §11) and its colours kept: a shared transcript now
carries a colour given as lowercase `#rrggbb`, which `tests/record_transcript.py`
keeps and the shared-safe check allows, and pins `topology.controls` as
declared, labels included. `tests/test_net_controls.py` covers the rest, and
`tests/test_empty_place_edges.py` the traffic light's two controls.

## As built, 2026-11-21: protocol version 2, paths as lists of steps (PROTOCOL.md §10, §12.1)

The protocol became version 2 on 2026-11-21, for subnet places, a stepping
rule per place and structured addresses
(`../petritype-rs/dev-docs/subnet-place-spec.md`). The runner took the first
part on its own: the version number, paths as lists of steps and progress per
net. The stepping rule and subnet places follow in later steps of the same
change; until then the topology still carries `passive`, and the driver still
follows the loaded net's `deep_policy`.

Settlements:

- **The version.** `wire.PROTOCOL_VERSION` is 2, on `describe` and on every
  `get_state`.
- **Paths.** A path is a list of `wire.PathStep`s, one per level from the
  loaded net down, each `{place, token, net}` for a net carried by a token,
  wherever a path appears: the addressed requests and their results, a
  token's `nets[]`, the `nested` notification, the `step` result and the log.
  `Paths.parse` (`translate.py`) checks a request's path and answers
  `invalid_params` for a value that is not a list, a step that is not an
  object or has no `place`, a step with one of `token` and `net` without the
  other, and a step value that is not a string; the old text form is refused
  with a message that says a path is a list of steps. The owner then resolves
  the steps one at a time (`_descend`), and a step that names no live net is
  `unknown_net`, its message starting `step N of the path:` with N counted
  from 0. The empty path names the loaded net, and a result about the loaded
  net carries no path. A step of the subnet form, `{place}` alone, is read and
  answered `unknown_net`, since no net has subnet places yet; `_descend` is
  where they join. *Done the same day (the last section):* `_descend` takes a
  subnet step to the subnet of its subnet place.
- **Names.** Nothing in the runner builds or parses an address as text, so a
  place or a net may be named with a `/` or a leading `#` and still be reached
  by path. Inside the owner a path is a tuple of `PathStep`s. The library
  keeps its own form of a path, three segments per level with the token's id
  spelt `#<id>` (`NestedNets.walk`, `find` and `resolve`, and the `Nested`
  records), in which each name is a whole segment; `Paths.of_segments` is the
  one place the runner reads it. The library's own descent still counts an
  inner firing on the net around it under a name joined with `/`
  (`"Shelf/#1/counter/Up"`: `NestedNets.count`, the return value of
  `fire_one_transition` with `descend`, `RunSummary.fired`). The runner never
  drives a net that way, so the protocol does not see it, unless a body
  steps an inner net through that descent itself; it goes with the descent,
  when the stepping rule replaces it. *Done the same day (the next section):*
  the library's paths are tuples of its own `PathStep`s and an inner firing an
  `InnerFiring`, and `Paths.of_segments` went with the segments.
- **Progress is per net** (§5.4). The owner no longer counts an inner firing
  in the outer net's progress under a joined key. Each net's progress is its
  own graph's `step_count`, `fired_counts` and `last_fired`, which the engine
  keeps for the firings of that net's own transitions, with the
  `last_progress_at` the owner keeps per net.
- **The transcripts.** The harness lost the `<path>` mask and the
  substitution of a capture inside a longer string. A path in a transcript is
  the list of steps it is, each step's token id a capture or `<id>`, and the
  shared-safe check asks that of every `path`. The recorder masks a path step
  by step: a step's token is its capture when the token has been named, and
  `<id>` otherwise, so that a token is still named where its label is seen.
  On re-recording a transcript it keeps what that transcript chose: its
  capture names, its plain `<id>` masks, its `<any>`, and the order of its
  unordered edges. All eight shared transcripts were re-recorded. 01 to 04,
  written by hand on 2026-10-03, are now in the recorder's layout; compared
  as JSON, each differs from what it replaced in `protocol_version` alone
  (02, which pins no version, not at all), as do 06 to 08. 05 differs in its
  version, its paths and one answer: the request that named a level without
  its net is `invalid_params` in version 2, where the text form's missing
  segment was `unknown_net`. Its sends spell their paths as lists. Every
  transcript re-records to the same bytes.

`tests/test_runner_nested.py` covers each wrong shape of a path, the index of
the failing step, the empty path, names with a slash or a leading hash, and
progress per net at two depths.

## As built, 2026-11-21: the stepping rule per place (PROTOCOL.md §12.3)

The second part of the same change (`../petritype-rs/dev-docs/subnet-place-spec.md`
§1.3 and §5). Every place that holds nets has one stepping rule, and one deep step
follows the rules of the places it meets, at every level. The rule replaced the
net-wide `deep_policy` and the `passive` setting, which are removed with no alias.

Settlements:

- **The setting.** `ListPlaceNode(..., nested=True, stepping=...)`, one of
  `"inner_first"` (the default), `"take_turns"`, `"outer_first"` and `"passive"`,
  typed as the `Literal` `Stepping`, which is how the library types its other
  choices of a name (`tie_break`). A nested place built without one has
  `"inner_first"`; a place that holds no nets has `None`, and one given a rule is
  a `ValueError` when it is built. The rule belongs to the place, so it is part of
  the definition: a template rebuilds it, `NetJson` does not save it, and assigning
  `place.stepping` changes it from the next deep step, the ring's cursor staying
  where it is.
- **One deep step** (`fire_one_transition` with `descend`, so `execute_graph` and
  the sequential `Runner` too): `NestedNets.take_turn(graph, "ahead")`, the
  inner-first members then the ring's members after the cursor; else the net's own
  selector, whose choice is the net's ring turn (`NestedNets.took_own_turn`),
  whatever comes of the firing; else `take_turn(graph, "behind")`, the rest of the
  ring then the outer-first members, offered only while `graph.in_flight` is empty.
  A member's turn is `fire_one_transition` with `descend` on that net
  (`NestedNets.deep_step`), so each level follows its own places' rules, and the
  member that fires, or fails, moves the cursor only if it is in the ring
  (`NestedNets.took_turn`). The cursor is `ExecutableGraph._ring_cursor`, the
  `net_id` of the member that fired last; `forget_cursors` (a load) and a deep copy
  clear it. The member list (`NestedNets.members`, `turns`) is read once per part,
  in walk order; nothing out here moves until something fires, and the step ends
  there. Under inner first a deep step still fires one transition, not a run to
  rest. A plain step (`descend=False`: the `Fire` command, a `press`) never moves
  a cursor.
- **Structured values.** A path is a tuple of `petritype.core.events.PathStep`
  (`place`, `token` as an integer, `net`), in `NestedNets.walk`, `find`,
  `resolve`, `members` and the `Nested` records. A firing inside an inner net is an
  `InnerFiring(path, transition)`: what `fire_one_transition` with `descend`
  returns, and the key under which `fired_counts`, `last_fired` and
  `RunSummary.fired` count it, on every net between. Nothing in the library builds
  or parses a name joined with a separator. The wire's `PathStep` keeps the token
  id as a string (§2), so the two stay two types: `Paths.of_step` and `of_steps`
  (`translate.py`) turn the library's into the wire's.
- **Quiescence deep** is a deep step that finds nothing: the net and every net in
  its non-passive places at rest. `NestedNets.any_fireable(graph, part)` asks it
  of one part of the round, which the fuse uses.
- **The concurrent runtime.** Each time round, `_fire_inner(ctx, "ahead")`, one
  inner firing inline; else the launch sweep, which is the outer net's turn and
  moves the cursor when it launched something; else, when it launched nothing,
  `_fire_inner(ctx, "behind")`, where the outer-first members wait while anything
  outer is in flight. So inner nets now fire beside outer bodies in flight, where
  before an inner net had its turn only when nothing was in flight. Two things
  keep that safe in Python, where a body may be a coroutine or on a thread while
  the loop fires inline: the nets of a token an in-flight body holds through a
  mutate edge are left out of the round until it lands (`_held`, from each
  flight's `FiringTokenIds.mutated`; a taken token is out of its place anyway),
  and after an inner firing with bodies in flight the loop yields
  (`asyncio.sleep(0)`) and takes what has landed before going round again. An
  inner firing counts against `stop_after_n_firings` as a launch does.
  `_tick_concurrent` takes the same three parts, one turn a tick.
- **The fuse** in sequential mode now counts the inner nets a deep step would
  enter, and blames what it would try first: `"an inner net"` ahead of the outer
  net's turn, the outer net's first fireable transition, then `"an inner net"`
  behind it. Before, a spent fuse let an inner firing through when the outer net
  had nothing fireable.
- **The owner.** `_select` is `_choose(outer)`: one deep step of the loaded net,
  chosen and not fired, by the same parts (`NestedNets.turns`) and the same cursor
  moves, each member's turn chosen by `_choose` on that net, so the runner and the
  library step alike. One firing is airborne at a time and nothing is chosen while
  it is, so the outer-first members are always offered. A `press` of a `fire`
  control is a plain firing and no longer records a turn. `topology.places[]`
  carries `stepping` on every place that holds nets, the default included, and
  `passive` is gone from `wire.Place`. Progress lists a net's own firings, the
  counts under its transitions' names, so that an `InnerFiring` a body's own deep
  step leaves on a graph never reaches the wire.

The transit city states `outer_first` on its five places of vehicles, so that its
journey holds: under inner first the cabins would close every door before anyone
boarded. `tests/protocol/05_nested_nets.json` was re-recorded and differs from
what it replaced by those five `stepping` fields alone. The new conformance net
`stepping-rules` (`tests/protocol_nets/stepping_rules.py`, fixed in PROTOCOL.md
§11) and its transcript `tests/protocol/10_stepping_rules.json` show the four rules
and a second level, ten steps and a run; both transcripts re-record to the same
bytes, and the other seven re-record unchanged. The recorder lost its check for a
token id embedded in text, which only the library's old joined names could raise.

`tests/test_stepping_rules.py` covers the rules case for case with the Rust core's
`tests/stepping.rs`, and the concurrent runtime beside a body in flight;
`tests/test_engine_policies.py` keeps the evidence that one level of take turns
steps as the interleaved policy did and outer first as the old default did;
`tests/test_runner_protocol_gaps.py` drives `step` and `start` under each rule and
reads `stepping` in the topology.

## As built, 2026-11-21: subnet places in the runner (PROTOCOL.md §12.7)

The spec's step 7 for Python (`../petritype-rs/dev-docs/subnet-place-spec.md` §2
and §5). The runner serves a net with subnet places and port places as the
protocol text says; the refusal to load one, which the library's step left in
`_build`, is gone.

Settlements:

- **One more inner net.** A subnet is one more `_Net` in `_Loaded.inner`, built by
  `_know_new` at the load or reset that builds it, with its own version, topology,
  progress, log view and fireable baseline, and kept until the next load or reset.
  `_net_for` tells a subnet by its path, whose last step is `{place}`, and links its
  record to the net around it (`_Net.parent`) and its subnet place
  (`_Net.subnet_place`); a subnet's `get_state` names its subnet place as `net`.
  `_descend` takes a subnet step to the subnet, `Paths.of_step` writes one for a
  library step with no token, and `_chain` passes through one without rendering a
  token.
- **Keys.** Every per-place table of a `_Net` is keyed as the net names its places:
  its own by name, the ports of its subnet places by the library's `Port` pair, the
  key every edge, event and handover of the library uses. The node under a `Port`
  is the subnet's own place, the one list of tokens; `_owner_of` gives the subnet
  and the port's own name, whose filter, visible set, type name and ids a port
  has. `Places` (`translate.py`) puts a key on the wire: `port` beside `place` in an
  object (`_where`), and `{"place", "port"}` where a place is named by position
  (`Places.ref`, `_pairs`, `Tokens.produced`).
- **Topology.** `Topologies.of` lists a subnet place as `wire.SubnetPlace`, its
  template's name as `type_name`, its ports as the subnet's own topology lists them,
  and its stepping rule; an edge on a port carries `port`. With `subnet`, which the
  owner passes for a subnet's record, a port place carries `port: true`. A net
  loaded on its own or carried by a token shows no `port`, since nothing reaches
  its ports (written into PROTOCOL.md §5.1). A place style on a subnet place is
  `load_failed`.
- **State and requests.** A snapshot's views are keyed by the same keys, a subnet
  place's ports at its position, and it keeps each subnet place's entry
  (`_subnet_entry`), whose `count` agrees with the ports as the snapshot shows
  them. `get_state` writes `{subnet, ports}` for a subnet place. `_place_key` reads
  `place` and `port` for `get_place_tokens` and `inject`, with the errors of §12.7.
  `query` without a filter projects each place by its owner's connection filter, a
  port by its subnet's; `Filter.keeps` gained the key the filter's net names the
  place by, so that a `places` restriction to a `Port` matches it, and keyed edges
  pass their own key. A filter's or `distinct`'s `places` entry is a name or an
  object with a string `place` and a string `port`, other keys ignored (§2, written
  into §13.2). `get_token` searches the net's own places only: a port's token is
  read through its subnet.
- **Versions.** `_cross` carries out §12.7 for every state notification of a net:
  for each subnet place whose port the change touched it raises that subnet's
  version and sets `subnet_marking_versions`, and for a change of a net's own port
  place, when the net is a subnet, it raises the parent's and sets
  `outer_marking_version`; each net moves once per notification. What a
  notification changed is `_changed_by` (`launched` from the handover, unfiltered):
  what it took, put or put back, or the place of an `added`, `removed` or
  `mutated`, which stand behind a `hidden` and behind the `added` and `removed` a
  filter sends when a token comes into or goes out of view (written into §12.7).
  `wire.PortChange` gives the eight notifications that can change a port the two
  fields, which `Lines.notification` writes before the stamp. The wrapper of a
  `nested` line carries the loaded net's version as it stands after the change.
- **Fireable.** The net across a port is touched by the change, so its fireable set
  is compared too; the touched nets are reported shallower first, so a net's
  `fireable` precedes those of the nets inside it at any depth.
- **Reads while a body runs.** `_held` now runs for a firing in any net: the tokens
  it takes or holds, named by the net whose place holds them, the nets inside them,
  and the subnet of every subnet place one of whose ports its transition has an
  edge on, whose reads wait for the landing. A firing of a subnet that takes from
  or hands over a token of its own port leaves the net around it a snapshot of that
  port as the firing leaves it (`_lifted`, captured with the same cut as the
  firing's own), so that a read of that net is answered at once at the version the
  `launched` gave it (written into §12.3).
- **The transcripts.** The harness's fold carries `subnet_marking_versions` and
  `outer_marking_version` from both pieces (§11), and the shared-safe check asks
  them masked. The recorder masks them, names a subnet's nested id after its subnet
  place (`level_1`), and names a token the net around a subnet names by the pair in
  the subnet, where its id was minted, so that the same token has one capture in
  both views (`level_1_ada`); an `inject` by pair has its answer's id named there
  too. The new conformance net `levels` (`tests/protocol_nets/levels.py`, fixed in
  PROTOCOL.md §11) stamps its levels with the template name `Level` without
  registering it, since the levels example registers a `Level` of its own in the
  same test process. `tests/protocol/09_subnet_places.json` was recorded over it;
  every transcript, 09 included, re-records to the same bytes, and none of the
  others changed.

The levels example gained a factory the runner serves, `levels-world`.
`tests/test_runner_subnets.py` covers each part at one and two depths, a subnet
inside a token's net and a token's net inside a port, the versions rule in each
of its cases, and the reads that wait or are served from a snapshot;
`tests/test_runner_process.py` serves `levels` as a process.

## As built, 2026-11-21: groups in the runner (PROTOCOL.md §5.1)

The spec's step 3 for Python (`../petritype-rs/dev-docs/group-spec.md` §3). A
group is a label on parts of one net that only drawings read, so the runner's
whole share is the topology; nothing else on the wire changes.

- **Topology.** `Topologies.of` writes the net's `ExecutableGraph.groups` as
  `groups`, in their order, and each place's, subnet place's and transition's
  `group`, each left out when absent (the wire models drop `None`). The owner
  builds every net's topology with it, the loaded net's and every inner net's,
  a subnet's and a token's net's, so each shows its own groups and none of the
  net around it. A port listed in a subnet place's `ports` is given no `group`:
  its group is the subnet's, shown in the subnet's own topology, and the net
  around it could not name that group (written into PROTOCOL.md §5.1 and §12.7).
- **Nothing else.** No request, notification, marking entry or log entry names a
  group, and the engine reads none, so firing, stepping, ids and versions are
  those of the same net with no groups.
- **The transcripts.** The recorder needed no change: a group's name is the
  net's own, the same in every runtime, and is kept as it stands. The new
  conformance net `groups` (`tests/protocol_nets/groups.py`, fixed in
  PROTOCOL.md §11) stamps its box with the template name `Box` without
  registering it, as `levels` does. `tests/protocol/11_groups.json` was recorded
  over it and is replayed through the owner and over pipes; every transcript, 11
  included, re-records to the same bytes, and none of the others changed.

`tests/test_runner_groups.py` covers the fields for the loaded net, a subnet by
path and by nested id, a token's net, a net with no groups, and the firings and
log that name none; `tests/test_runner_process.py` serves `groups` as a process.
