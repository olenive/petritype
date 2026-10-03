# The protocol runner and the library changes behind it

Status: **design of 2026-10-03; token ids, events, the runner's state
machine and its process layer built the same day** ("As built" sections
below). `python -m petritype.runner` works and the four shared transcripts
replay against it; the 0.2.0 release is not done yet. The protocol
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
| `places[].serializable` | the place type is a pydantic model or a JSON scalar |
| edges | argument edges (`take`, or `take_all` for a `list[...]` argument), `snapshot_edges`, `mutate_edges`, return edges (`slot` from `return_index`) |

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
- The runner speaks version 1 only. Translation for version 0 clients lives
  in petritype-server's worker for one release (see its design doc), so the
  library never carries the old shapes.

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
  page works, the airborne transition shows `in_flight`. Cost is one shallow
  copy of the marking per firing, in place of the server's capped-plus-full
  snapshots refreshed after every mutation. A later refinement could copy
  only the places the body can touch (its mutate edges).
- `get_token` for a token the airborne body holds waits for the landing;
  every other id is answered at once.
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
each transcript masks what differs by runtime and carries no `color`.

Rust-side follow-ups before the shared set runs there: register the
conformance net in the Rust runner example under the name
`parcel-distribution` and the same description (the demo net now using that
name needs another), mark its four places serializable and derive
`Serialize`/`Deserialize` on `Parcel`. Covering `not_serializable` in the
shared set needs a second token type in the conformance net, since Python
decides serializability per type.

## Release

A minor version (0.2.0): everything above is additive for library users.
`record_events` off and ids kept beside the tokens mean an existing program
sees no change. The protocol transcripts under `tests/protocol/`
(`PROTOCOL.md` §11) are the runner's acceptance tests.
