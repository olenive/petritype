# The protocol runner and the library changes behind it

Status: **design proposal, 2026-10-03, not yet implemented.** The protocol
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

## Release

A minor version (0.2.0): everything above is additive for library users.
`record_events` off and ids kept beside the tokens mean an existing program
sees no change. The protocol transcripts under `tests/protocol/`
(`PROTOCOL.md` §11) are the runner's acceptance tests.
