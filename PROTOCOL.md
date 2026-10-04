# The petritype net protocol

Status: **protocol version 1.** Written 2026-10-03 and amended the same day
after the first implementation (§2, §4, §5.3, §6.1, §6.3, §6.5, §11), and on
2026-10-04 when the Python runner moved to two-piece firings (§6.3, §7.1). Both
runners implement it, the Rust one (`petritype-runner`, in
`../petritype-rs`) and the Python one (`python -m petritype.runner`,
`DESIGN_RUNNER.md`), and both replay the shared transcripts of §11; the
Bevy visualiser (`../petritype-bevy`) hosts either as a net process since
2026-10-04. This file is the single source of truth for the protocol; the
other repositories link here and do not restate the shapes:

- library side (Python): [`DESIGN_RUNNER.md`](DESIGN_RUNNER.md)
- library side (Rust): `../petritype-rs/DESIGN.md` §19
- server: `../petritype-server/dev-docs/RUNTIMES_AND_PROTOCOL.md`
- viewers: `../petritype-bevy/DESIGN.md`,
  `../petrivelte/implementation_docs/PROTOCOL_V1_FRONTEND.md`

Canonical URL once merged: <https://github.com/olenive/petritype/blob/main/PROTOCOL.md>.

---

## 1. What it is for

A Petri net is written in one language: its transition bodies are code. The
protocol lets anything that wants to watch or drive a net do so without
knowing which language that is. It has two ends:

- A **net process** hosts one net and speaks the protocol as newline-delimited
  JSON on its stdin and stdout. The Python library ships one
  (`python -m petritype.runner`); the Rust workspace ships a library that
  turns a user's binary into one (`petritype-runner`).
- A **host** spawns a net process and talks to it. petritype-server's worker
  is a host; so is the Bevy visualiser when it runs a net process itself.
  A host may relay the same shapes onward over HTTP and server-sent events
  (§9), which is how a browser, or a remote Bevy, sees a net it did not
  spawn.

A viewer that speaks the protocol does not care which runtime is behind it.
Moving a net between languages, composing nets across languages, and WASM
are out of scope.

## 2. Conventions

- **Places and transitions are addressed by name.** Names are unique within a
  net (both libraries enforce it at build time). Names are what a user, a
  saved file and a URL know, and they survive a change of declaration order.
- **Token ids are strings holding a decimal integer**, for example `"17"`.
  Within one generation of a net (§4) an id is handed out once and never
  reused, so a viewer may key animations, caches and selections by it. The
  first id of a generation is the runtime's choice (the Rust runner starts
  at `"0"`, the Python runner at `"1"`); nothing may depend on it.
  Strings rather than numbers because a JavaScript number loses precision
  above 2^53 and a Rust `TokenId` is a `u64`; the runner converts at the
  boundary. A reader must treat the id as opaque and compare it as a string;
  ordering by numeric value is allowed (ids increase) but nothing should
  depend on it.
- **Times** are ISO 8601 strings in UTC (`"2026-10-03T09:15:02.413Z"`);
  durations are `duration_ms` as a JSON number.
- **Unknown fields are ignored and unknown notifications are skipped.** That
  is what lets version 1 grow without a bump (§10).
- Optional fields are marked `?` below and are omitted, never `null`, unless
  stated otherwise.

## 3. Framing (net process, stdin and stdout)

One JSON object per line, UTF-8, no newline inside an object. Three kinds,
told apart by their keys:

| kind | shape | direction |
|---|---|---|
| request | `{"id": string, "method": string, "params": object}` | host to net process |
| response | `{"id": string, "result": object}` or `{"id": string, "error": {"message": string, "code": string}}` | net process to host |
| notification | `{"method": string, "params": object}` | net process to host |

- The request `id` is chosen by the host and echoed in the response. Requests
  are answered in the order they arrive unless stated otherwise.
- **Every notification caused by a request is written before that request's
  response.** A host that applies notifications as they come therefore holds
  the effect of a `step` before it reads the `step` result.
- **stdout belongs to the protocol.** User code in transition bodies prints;
  the net process sends that to stderr (the Python runner swaps `sys.stdout`
  for `sys.stderr` at start; the Rust runner moves file descriptor 1). A host
  treats stderr as free text, logs it, and may relay each line to viewers as
  a `subprocess_output` event (§9). The net process never emits
  `subprocess_output` itself.
- Lines may be long (a full marking page); a host must accept lines of at
  least 16 MiB. Paged reads (§6) keep single responses well below that.
- When stdin reaches end of file the net process exits.

### Error codes

`error.code` is one of a closed set, so a host can map it to an HTTP status
without parsing `message`:

| code | meaning |
|---|---|
| `unknown_method` | no such method |
| `invalid_params` | params missing or of the wrong shape |
| `not_loaded` | the method needs a loaded net |
| `unknown_net` | `load` named a net this process does not carry |
| `load_failed` | the factory raised or the net did not build |
| `unknown_place`, `unknown_transition`, `unknown_token` | a name or id that does not exist now |
| `not_serializable` | the place has no JSON form, so a token cannot be injected or shown as JSON |
| `invalid_token` | the JSON did not decode to the place's type |
| `unsupported` | this runtime cannot do this (for example `set_param` on a net that declares no parameters by name) |
| `internal` | anything else; `message` says what |

## 4. Identity over time: generations and versions

- **`generation`** (integer) counts loads and resets of the net in this
  process, starting at 1 on the first `load`. Token ids are unique within a
  generation. A reset rebuilds the net from its factory, and a Rust net
  rebuilt from its builder numbers its tokens from the start again; rather
  than ask every runtime to offset its ids, the protocol says so out loud. A
  viewer that sees the generation change drops every token it holds and
  refetches state.
- **`marking_version`** (integer) never decreases during the life of a net
  process. It rises by **exactly one** for each state notification (§7.1)
  and is stamped on it, so a viewer holding version *v* expects the next
  state notification at *v + 1*; anything else is a gap and the viewer
  refetches `get_state`. Every response that reads state says which version
  it reflects. Requests that change nothing do not move it. `load` and
  `reset` do not reset it: the `reloaded` notification (§7.2) carries the
  current value and always means "refetch". A firing moves it once per
  state notification, so by one where a runtime reports `fired` and by two
  where it reports `launched` then `distributed` (§6.3); nothing may
  compare version numbers across runtimes.
- **`running`** (boolean) says whether the net process is executing on its
  own (after `start`, until `stop` or quiescence).

## 5. The state shape

`get_state` returns:

```json
{
  "protocol_version": 1,
  "runtime": "python",
  "net": "parcel-distribution",
  "generation": 1,
  "marking_version": 42,
  "running": false,
  "topology": {
    "places": [{"name": "Receiving", "type_name": "Parcel", "serializable": true}],
    "transitions": [{"name": "Sort", "detail": {"signature": "(parcel: Parcel) -> Sorted"}}],
    "edges": [
      {"place": "Receiving", "transition": "Sort", "direction": "in", "kind": "take"},
      {"place": "Sorting", "transition": "Sort", "direction": "out", "slot": 0}
    ],
    "hints": {"positions": {"Receiving": {"x": 0, "y": 0}}}
  },
  "transitions": {
    "Sort": {"enabled": true, "fireable": true, "in_flight": false, "priority": 0.0}
  },
  "marking": {
    "places": {
      "Receiving": {"count": 8, "tokens": [{"id": "1", "label": "Parcel(id=1, ...)", "color": "#d94f4f"}]}
    }
  },
  "progress": {
    "step_count": 0, "last_fired": null, "fired_counts": {}, "last_progress_at": null
  }
}
```

### 5.1 Topology

Changes only on `load` and `reset` (a new generation), so a viewer fetches it
once per generation and lays it out.

- `places[]`: `name`; `type_name`, the runtime's own spelling of the token
  type, for display only; `serializable`, whether tokens of this place have a
  JSON form (decides whether `inject` and `get_token` with `kind: "json"` can
  work).
- `transitions[]`: `name`; `detail?`, a free object of runtime-specific
  introspection (Python: `function_name`, `signature`, `docstring`; Rust:
  `params`, the parameter type names, and `detachable`). A viewer may show
  `detail`; it must not depend on any key in it.
- `edges[]`: `place`, `transition`, `direction` (`"in"` place to transition,
  `"out"` transition to place); `kind?` for input edges, one of `"take"`,
  `"take_all"`, `"snapshot"`, `"mutate"` (a reader that meets another value
  draws a plain edge); `slot?` for output edges of a transition with more
  than one output. Read edges (`snapshot`, `mutate`) are listed: they move no
  tokens but they are part of the net. The order of `edges[]` is not
  significant; a reader keys an edge by its endpoints.
- `hints?`: `{"positions": {name: {"x": number, "y": number}}}`. Position
  hints an author or a runtime may supply. **Layout belongs to the viewer**: a
  viewer may honour hints (pinning those nodes) and must work without them.
  No runtime is required to compute a layout.

### 5.2 Transition state

`transitions` maps each name to:

- `enabled`: the on/off switch set by `enable` and `disable`. Not whether the
  transition can fire now.
- `fireable`: enabled, not in flight, every input place non-empty, guard
  passes. Both libraries use this vocabulary: "fireable" for can-fire-now,
  "enabled" for the switch. petritype-server's engine still says "enabled"
  for fireable until it moves to the library runner.
- `in_flight`: its body is running.
- `priority?`: the current score the default selector would use, if the
  runtime can compute it cheaply and safely; omitted if computing it raised.

Kept apart from the topology because it changes on every firing.

### 5.3 Marking

`marking.places` maps each place name to `count` (the true number of tokens)
and `tokens`, the oldest tokens first, **capped** (default 200 per place;
`get_state {token_limit}` changes it, `get_place_tokens` pages the rest). A
viewer reads sizes from `count`, never from `tokens.length`.

A token is `{id, label, color?, type_name?}`:

- `label`: short display text chosen by the runtime, at most 1000
  characters counted as Unicode scalar values, *including* any truncation
  marker the runtime appends (both runners append `...`, so a cut label is
  997 characters of text plus the marker). Python: the pydantic JSON dump
  or `repr`; Rust: the place's `Debug`. The full value is `get_token`
  (§6.5).
- `color?`: a CSS hex colour (`"#rrggbb"`), when the runtime has one for this
  token (Python: a `.color` attribute).
- `type_name?`: only when it differs from the place's `type_name` (a Python
  place may hold subclasses).

### 5.4 Progress

`step_count` (completed firings since load or reset), `last_fired` (name or
`null`), `fired_counts` (name to count), `last_progress_at` (time of the last
completed firing, or `null`). Kept by the runner where the core does not keep
it.

## 6. Methods

| method | params | result | notes |
|---|---|---|---|
| `describe` | `{}` | Describe (§6.1) | allowed before `load` |
| `load` | `{net, params?}` | `{status: "loaded", generation, marking_version}` | emits `load_progress`; `reloaded` |
| `get_state` | `{token_limit?}` | State (§5) | never waits for a running body |
| `get_place_tokens` | `{place, offset?, limit?}` | `{place, marking_version, count, offset, limit, tokens, has_more}` | `limit` clamped to 1..1000, default 200 |
| `get_token` | `{id}` | `{id, place, type_name, kind: "json", value}` or `{id, place, type_name, kind: "text", text}` | live tokens only |
| `step` | `{}` | `{fired: bool, transition?, failed?, marking_version, running}` | one firing, sequential semantics (§6.3) |
| `start` | `{}` | `{status: "started", marking_version, running}` | |
| `stop` | `{}` | `{status: "stopped", marking_version, running}` | lets the body in flight finish |
| `reset` | `{}` | `{status: "reset", generation, marking_version}` | same factory, same params |
| `inject` | `{place, token}` | `{status: "injected", id, marking_version}` | emits `added` |
| `enable` / `disable` | `{transition}` | `{marking_version}` | emits `toggled` if it changed |
| `set_param` | `{param, value, transition?}` | `{marking_version}` | optional; `unsupported` otherwise |
| `get_log` | `{after?, limit?}` | `{entries}` (§8) | |
| `shutdown` | `{}` | `{status: "ok"}` | then the process exits |

### 6.1 `describe`

```json
{
  "protocol_version": 1,
  "runtime": "rust",
  "runtime_version": "0.2.0",
  "nets": [
    {
      "name": "parcel-distribution",
      "description": "Sort and dispatch parcels",
      "mode": "manual",
      "params": [{"name": "trucks", "type_name": "u32", "required": false, "default": 2}]
    }
  ]
}
```

- `nets[].mode?` and `schedule?` are the hosting hints Python's
  `@petri_net(mode=..., schedule=...)` already declares (`"manual"`,
  `"24/7"`, `"batch"`, `"cron"`). A host that schedules uses them; a viewer
  ignores them. Like every optional field they are omitted when absent,
  never `null` (§2); `description?` likewise.
- `params[].default?` is a JSON value, present only when the default has
  one.
- A net process may also answer `describe` without a stdin session: a Rust
  runner binary prints the same object as one line for `--describe` and
  exits; the Python runner does the same for
  `python -m petritype.runner --describe --module <module> ...`. This is how
  a build step discovers what a binary carries.

### 6.2 `load`

`{net, params}` names a net from `describe` and its factory parameters as a
JSON object. The Python runner also accepts the version 0 form
`{module, function, factory_params}`, so a host can move to the runner
before it moves to `describe`.

### 6.3 Execution

- `step` drains pending input, then fires at most one transition, chosen by
  the net's own selector. `fired: false` with no `transition` means nothing
  was fireable. `fired: true` with `transition` means a transition was
  chosen and its firing landed, successfully or not: when the body failed
  the response also carries `failed: true`, and follows the `failed`
  notification. Its response is written when the firing is complete (after
  `fired`, `distributed` or `failed`), so a caller waiting on `step` waits
  for the body. `step` is the one request answered out of order: `get_state`,
  `get_place_tokens`, `get_token`, `get_log` and `stop` that arrive while
  its body runs are answered before it.
- `start` runs on the net process's own clock until `stop`. A net whose
  `mode` is `"24/7"` idles when nothing is fireable (emitting `idle` once
  per idle spell) and keeps polling; any other net stops at quiescence and
  emits `execution_stopped {reason: "no_fireable_transitions"}` (version 0 spelt
  this `no_enabled_transitions`; the server's translation maps it).
- The runtime picks its driver. Both fire one transition at a time. Both
  runners run every body off their request thread and report `launched`
  then `distributed` (the Python one since 2026-10-04). `fired` remains
  legal for a runtime that cannot observe a body part-way, which neither
  current runner is. Either way, state reads and `stop` are answered while
  a body runs, and a viewer must handle both shapes.
- A failing body emits `failed`; whether execution goes on is the runtime's
  policy and is reported by `execution_stopped` when it stops.

### 6.4 Input

- `inject {place, token}` decodes `token` (plain JSON) into the place's type
  and appends it. Rust decodes with the place's `from_json`, which needs a
  place marked `serializable` (`not_serializable` otherwise); Python with
  pydantic in strict JSON mode for models and exact JSON types for scalars.
  **Decoding is strict in both runtimes**, with JSON's own types as the
  rule: a number spelt as a string, a string given as a number, a float for
  an integer, or a boolean for a number is `invalid_token`; an integer for a
  float field and an ISO 8601 string for a date-time are accepted, since
  JSON has no other way to carry them. Parameter values (`load`,
  `set_param`) follow the same rule, answering `invalid_params`. The shared
  transcripts check it.
- `enable` and `disable` set the switch. Both libraries have it.
- `set_param` is optional. Python sets one keyword argument of one
  transition (`transition` required); Rust sets a net-wide parameter by its
  registered name (`transition` ignored). A runtime that cannot answers
  `unsupported`.

### 6.5 Reading one token

`get_token {id}` answers `kind: "json"` with `value` exactly when the
token's place is `serializable` (§5.1), and `kind: "text"` with `text`
otherwise. `text` is the runtime's full rendering of the token (Python:
`repr`; Rust: `Debug`), never truncated: `label` (§5.3) is the short form,
this is the long one. A host that wants JSON for a place that has none gets
`kind: "text"`, not an error; `not_serializable` is for `inject`.

## 7. Notifications

Every notification carries `marking_version` (after the change, if it made
one) and `running`.

### 7.1 State notifications (each raises `marking_version` by one)

| method | params | when |
|---|---|---|
| `fired` | `{transition, consumed: [[place, id]], produced: [[place, id, label, color?]], duration_ms}` | a firing reported in one piece, by a runtime that cannot observe a body part-way (neither current runner) |
| `launched` | `{transition, consumed: [[place, id]]}` | a firing reported in two pieces (both runners, which run every body off their request thread): inputs taken, body started |
| `distributed` | `{transition, produced: [[place, id, label, color?]], duration_ms}` | the second piece: body returned, outputs placed |
| `failed` | `{transition, error, restored, consumed: [[place, id]]}` | a body failed; `restored` says whether the consumed tokens went back (same ids, front of their places) |
| `added` | `{place, id, label, color?}` | a token arrived from outside (`inject`, a command) |
| `removed` | `{place, id}` | a token was removed from outside (replacing a place's tokens) |
| `mutated` | `{place, id, label?}` | a token changed in place (a mutate edge) |
| `toggled` | `{transition, enabled}` | the switch changed |
| `param_changed` | `{param, transition?}` | `set_param` took effect |

A firing that mutates through a read edge emits its `mutated`s before its
`fired` (or `distributed`, or `failed`). `fired` carries consumed and
produced so a viewer can animate tokens from place to transition to place
without diffing markings. Produced entries carry the label so a viewer can
draw a token without a fetch; `color` is a fourth element when present.

### 7.2 Other notifications (do not move `marking_version`)

| method | params | meaning |
|---|---|---|
| `reloaded` | `{generation, reason: "load" \| "reset" \| "resync"}` | the marking was replaced wholesale; refetch state. `resync` is a runtime's way of saying "something changed that I cannot describe as events". |
| `fireable` | `{transitions: [name]}` | the fireable set after a batch of changes, sent only when it differs from the last one sent |
| `idle` | `{idle_since}` | a `"24/7"` net found nothing fireable; once per idle spell |
| `execution_stopped` | `{reason, error_type?, message?}` | the net stopped running on its own |
| `heartbeat` | `{step_started_at, executing_transition}` | periodic while loaded; both `null` when no body runs |
| `load_progress` | `{step, message}` | during `load` |

## 8. Log

`get_log {after?, limit?}` returns `{entries: [...]}` from a bounded ring the
runner keeps (at least the last 1000 firings). An entry is:

```json
{"seq": 12, "transition": "Sort", "started_at": "...", "duration_ms": 3.1,
 "outcome": "fired", "consumed": [["Receiving", "4"]], "produced": [["Sorting", "9"]]}
```

`outcome` is `"fired"` or `"failed"` (with `error` and `restored`). Entries
are keyed by **place names** and protocol token ids: no argument names, no
runtime-specific id spellings. `after` returns entries with `seq` greater
than it.

## 9. Relayed over HTTP and server-sent events

A host that serves the net to remote viewers relays the same shapes:

- Each method is an HTTP request whose body is `params` and whose JSON
  answer is `result`, or an error status carrying `{detail, reason}` with
  `reason` the protocol error code. The routes are the server's to choose;
  petritype-server's are in its design doc.
- Each notification becomes one server-sent event whose `kind` is the
  notification `method` and whose `data` is its `params`. The relay may wrap
  it in an envelope of its own (petritype-server adds `seq`, `ts`, `net_id`,
  `marking_version`, `running`) and may add events of its own, such as
  `subprocess_output {text}` for a stderr line and `stream_gap` when its
  buffer overflowed. A viewer treats `stream_gap` as a gap: refetch state.

## 10. Versioning

- `protocol_version` is an integer. **Version 1** is this document.
  **Version 0** is, retroactively, petritype-server's worker contract as it
  stood on 2026-10-03 (`petritype_server/worker/net_ipc.py` and
  `net_runner.py`); its differences from version 1 are tabulated in the
  server's design doc.
- Adding an optional field, a notification, a method or an error code does
  not change the version; readers ignore what they do not know. Removing
  anything, renaming anything, or changing what a field means does.
- A host learns the version from `describe` before `load`, and refuses a net
  process whose version it does not speak with a clear message.

## 11. Conformance

Conformance is tested by recorded transcripts: one JSON file per scenario,
replayed through a runner without a process by a harness that owns the
runner's state machine, or over pipes against the binary.

```json
{
  "net": "parcel-distribution",
  "params": {"trucks": 2},
  "exchanges": [
    {"send": {"method": "describe", "params": {}},
     "expect": [{"id": "1", "result": {"protocol_version": 1, "runtime": "<label>", "..." : "..."}}]},
    {"send": {"method": "step", "params": {}},
     "expect": [
       {"method": "fired", "params": {"transition": "Sort", "consumed": [["Receiving", "<id>"]],
                                      "produced": [["Sorting", "<id>", "<label>"]], "duration_ms": "<ms>",
                                      "marking_version": "<version>", "running": false}},
       {"id": "2", "result": {"fired": true, "transition": "Sort", "marking_version": "<version>", "running": false}}
     ]}
  ]
}
```

- `send` is a request without its `id`; the harness assigns `"1"`, `"2"`,
  ... in order. `expect` is every line the runner must write for that
  request, responses and notifications alike, in order.
- Masks stand in any value position: `"<label>"`, `"<time>"` and `"<id>"`
  match any string; `"<ms>"` and `"<version>"` match any number (a boolean
  is not a number); `"<any>"` matches any value, objects and arrays
  included; `{"<unordered>": [...]}` matches an array holding the same
  elements in any order (for `edges[]`, §5.1); `"<id:NAME>"` matches any
  string and binds it for the rest of the transcript, so a later `expect`
  must show the same string and a later `send` has it substituted, which is
  how a transcript names a runtime-assigned token id. Numbers compare by
  value (`0` matches `0.0`); an object must have exactly the expected keys;
  arrays must have the same length and match element by element.
- A transcript shared across runtimes masks every token id and every
  `marking_version` (§2, §4), every `type_name` and `label`, `runtime` and
  `runtime_version`, every `duration_ms` and time, sets every `detail` to
  `"<any>"`, and uses no `color`.
- Before comparing, the harness folds a `launched` immediately followed by
  its `distributed` into one `fired {transition, consumed, produced,
  duration_ms}`, so a transcript recorded against either runtime reads the
  same (§6.3).
- The format was first implemented by the Rust runner, whose own four
  transcripts (over a demo net with no Python twin) live in
  `../petritype-rs/petritype-runner/tests/protocol/`. The shared set lives
  here under `tests/protocol/` (four files as of 2026-10-03, replayed by
  `tests/test_protocol_transcripts.py` both through the runner's state
  machine and over pipes against `python -m petritype.runner`). They run
  over the **conformance net** `tests/protocol_nets/parcel_distribution.py`,
  named `parcel-distribution`, described "Sort, stage and dispatch
  parcels": places `Receiving`, `Sorting`, `Staging Area`, `Dispatched`;
  transitions `Sort`, `Stage`, `Dispatch Truck`; eight seeded parcels; a
  deterministic copy of the Rust example `petritype/examples/parcel_distribution.rs`.
  Both runners carry it (Rust: the `petritype-runner` example binary) and
  both replay the shared set. **The shared set is replayed against a runner
  carrying the conformance net alone**, so a shared transcript may pin
  `describe` in full; a runner that carries other nets too replays it with
  a registry reduced to the conformance net.
