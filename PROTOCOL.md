# The petritype net protocol

Status: **protocol version 1.** Written 2026-10-03 and amended the same day
after the first implementation (§2, §4, §5.3, §6.1, §6.3, §6.5, §11), on
2026-10-04 when the Python runner moved to two-piece firings (§6.3, §7.1), and
on 2026-10-09 for nested nets (§12) and token keys, queries and filters (§13),
with what they add to §3, §5, §6, §7, §8 and §11; those additions are the
agreed wire form of `NETS_AS_TOKENS.md` R8 and `TOKEN_QUERYING_AND_FILTERING.md`
R3 (both in `Cyberpunk2020-Simulation/dev-docs/`), written before either
runtime implemented them so that both implement the same thing. Both
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
| `unknown_net` | `load` named a net this process does not carry, or a `path` or `nested_id` (§12.1) names no live inner net |
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
  work); `nested?`, `true` when tokens of this place may hold nets of their
  own (§12), and `passive?`, `true` when such a place's inner nets are
  stepped only by an outer body (§12.3); `key_names?`, the names of the keys
  the place derives from its tokens, in declaration order, present only when
  the place declares keys (§13.1).
- `transitions[]`: `name`; `detail?`, a free object of runtime-specific
  introspection (Python: `function_name`, `signature`, `docstring`; Rust:
  `params`, the parameter type names, and `detachable`). A viewer may show
  `detail`; it must not depend on any key in it.
- `edges[]`: `place`, `transition`, `direction` (`"in"` place to transition,
  `"out"` transition to place); `kind?` for input edges, one of `"take"`,
  `"take_all"`, `"snapshot"`, `"mutate"`, `"take_where"`, `"mutate_where"`
  (a reader that meets another value draws a plain edge), the last two
  carrying `filter` (§13.5); `slot?` for output edges of a transition with
  more than one output. Read edges (`snapshot`, `mutate`) are listed: they move no
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
viewer reads sizes from `count`, never from `tokens.length`. Under a
connection filter (§13.4) a place also reports `matching`, the number of its
tokens the filter lets through, and `tokens` holds only those.

A token is `{id, label, color?, type_name?, keys?, nets?}`:

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
- `keys?`: `{name: value}`, the token's keys, only when its place declares
  keys (§13.1).
- `nets?`: `[{name, nested_id, path, template?, count}]`, the nets the token
  holds, only when it holds some (§12.2).

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
| `get_state` | `{token_limit?, path?, nested_id?}` | State (§5), plus `path`, `nested_id`, `template?` for an inner net | never waits for a running body |
| `get_place_tokens` | `{place, offset?, limit?, path?, nested_id?}` | `{place, marking_version, count, matching?, offset, limit, tokens, has_more, path?, nested_id?}` | `limit` clamped to 1..1000, default 200 |
| `get_token` | `{id, path?, nested_id?}` | `{id, place, type_name, kind: "json", value, path?, nested_id?, nets?}` or `{..., kind: "text", text, ...}` | live tokens only |
| `step` | `{}` | `{fired: bool, transition?, failed?, path?, nested_id?, marking_version, running}` | one firing, sequential semantics (§6.3); `path` when it was in an inner net (§12.3) |
| `start` | `{}` | `{status: "started", marking_version, running}` | |
| `stop` | `{}` | `{status: "stopped", marking_version, running}` | lets the body in flight finish |
| `reset` | `{}` | `{status: "reset", generation, marking_version}` | same factory, same params |
| `inject` | `{place, token, path?, nested_id?}` | `{status: "injected", id, marking_version}` | emits `added` |
| `enable` / `disable` | `{transition}` | `{marking_version}` | emits `toggled` if it changed |
| `set_param` | `{param, value, transition?}` | `{marking_version}` | optional; `unsupported` otherwise |
| `get_log` | `{after?, limit?, path?, nested_id?}` | `{entries}` (§8) | |
| `query` | `{filter?, token_limit?, path?, nested_id?}` | `{marking_version, count, matches}` (§13.3) | never waits for a running body |
| `distinct` | `{key, places?, path?, nested_id?}` | `{marking_version, key, values}` (§13.3) | |
| `set_filter` | `{filter?, path?, nested_id?}` | `{marking_version}` | the connection's projection (§13.4) |
| `shutdown` | `{}` | `{status: "ok"}` | then the process exits |

`path` and `nested_id` name an inner net (§12.1); a method that takes them
acts on that net instead of the outer one. Without either, a method acts on
the outer net, as before.

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
  policy and is reported by `execution_stopped` when it stops, with
  `reason: "transition_failed"` and the failure's `error_type` and `message`
  (both runners stop).
- When tokens hold nets, "one firing" may be a firing inside one of them:
  `step` and a running net fire the outer net's choice first and an inner
  net's only when the outer net has nothing fireable (§12.3), and
  `no_fireable_transitions` means nothing fireable anywhere.

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
| `added` | `{place, id, label, color?, keys?}` | a token arrived from outside (`inject`, a command) |
| `removed` | `{place, id}` | a token was removed from outside (replacing a place's tokens) |
| `mutated` | `{place, id, label?, keys?}` | a token changed in place (a mutate edge) |
| `hidden` | `{kind: "added" \| "removed" \| "mutated", place}` | stands in for an `added`, `removed` or `mutated` about a token the connection's filter hides (§13.4) |
| `toggled` | `{transition, enabled}` | the switch changed |
| `param_changed` | `{param, transition?}` | `set_param` took effect |

A firing that mutates through a read edge emits its `mutated`s before its
`fired` (or `distributed`, or `failed`). `fired` carries consumed and
produced so a viewer can animate tokens from place to transition to place
without diffing markings. Produced entries carry the label so a viewer can
draw a token without a fetch; `color` is a fourth element when present, and
`keys` (§13.1) a fifth when the place declares keys, in which case a token
without a colour has `null` as its fourth element (the one place the
protocol writes `null` for an absent optional, so that the fifth keeps its
position). `added` and `mutated` carry `keys` under the same condition, the
keys as they stand after the change.

### 7.2 Other notifications (do not move `marking_version`)

| method | params | meaning |
|---|---|---|
| `reloaded` | `{generation, reason: "load" \| "reset" \| "resync"}` | the marking was replaced wholesale; refetch state. `resync` is a runtime's way of saying "something changed that I cannot describe as events". |
| `fireable` | `{transitions: [name]}` | the fireable set after a batch of changes, sent only when it differs from the last one sent |
| `idle` | `{idle_since}` | a `"24/7"` net found nothing fireable; once per idle spell |
| `execution_stopped` | `{reason, error_type?, message?}` | the net stopped running on its own |
| `heartbeat` | `{step_started_at, executing_transition}` | periodic while loaded; both `null` when no body runs |
| `load_progress` | `{step, message}` | during `load` |
| `nested` | `{path, nested_id, event: {method, params}}` | an event in an inner net, wrapping the notification that net would send (§12.4); moves the inner net's version, carried inside `event`, not the outer net's |

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

The ring is one for the process: a firing inside an inner net (§12) is an
entry too, in order with the outer firings, carrying `path` and `nested_id`
(the path as it stood when the firing happened: the place the token stood in
at the time) and the inner net's place names and token ids; `seq` is shared.
`get_log` with a `path` or `nested_id` returns only the entries of that net
and of nets inside it.

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
- The relay does not keep §3's order between a response and the
  notifications its request caused: the HTTP answer and the event stream
  are two connections, and either may arrive first. A viewer that depends
  on that order restores it itself, by holding the answer to a
  state-changing request until the stream has reached the answer's
  `marking_version` (with a time limit, so a dead stream cannot wedge it)
  and by treating a state notification at or below the version it holds
  as already reflected rather than as a gap. petritype-bevy does both.

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
  both replay the shared set. **A shared transcript is replayed against a
  runner carrying the transcript's own net alone**, so it may pin `describe`
  in full; a runner that carries other nets too replays it with a registry
  reduced to the net the transcript names.
- Two more conformance nets came with §12 and §13 on 2026-10-09, each the
  example net of its design document built deterministically, so that the
  transcripts over them (`05_nested_nets.json`, `06_keys_query_filter.json`)
  replay against both runtimes; the Python ones are
  `tests/protocol_nets/transit.py` and `tests/protocol_nets/wildlife.py`.
  Their names, places, transitions, seeds, parameters and the guards that
  decide what fires are fixed here, since a transcript depends on them:

  **`transit`** ("Vehicles board, carry and alight passengers"), the
  `City` net of `NETS_AS_TOKENS.md` §2. Places, in this order: `Depot`,
  `Central`, `Harbour`, `Airport` (type `Vehicle`, nested, serializable),
  `Platform@Depot`, `Platform@Central`, `Platform@Harbour`,
  `Platform@Airport` (type `str`), `In Transit` (type `Vehicle`, nested,
  serializable). `Vehicle` is `{name, kind, destination, cabin}` with `kind`
  one of `"Bus"`, `"Tram"`, `"Ferry"`, `"Shuttle"`, `destination` a place
  name or `null`, and `cabin` its one inner net (named `cabin` whatever the
  kind), in the form of §12.5. Templates: `Cabin` (Bus, Tram: places
  `Door`, `Seats`, `Standing`, `Announcements`, all `str`; transitions
  `Sit` (Door to Seats, guard: `Seats` holds fewer than `seats`), `Stand`
  (Door to Standing, guard: `Seats` holds `seats` or more), `Go To Door`
  (Seats to Door) and `Go To Door From Standing` (Standing to Door), both
  guarded by `stopped` being true and `Announcements` non-empty, and
  `Clear Announcement` (consumes one announcement, guard: `Seats` and
  `Standing` both empty); parameters `stopped` (`true`), `seats` (`2`),
  `capacity` (`6`)); `Deck` (Ferry: places `Door`, `Lower Deck`,
  `Upper Deck`, `Announcements`; transitions `Go Below` (Door to Lower
  Deck), `Go Up` (Lower Deck to Upper Deck), `Go To Door` (Lower Deck to
  Door) and `Go To Door From Upper Deck` (Upper Deck to Door), the last two
  guarded as the Cabin's, `Clear Announcement` guarded by both decks empty;
  parameters `stopped` (`true`), `capacity` (`40`)); `Pod` (Shuttle: places
  `Door`, `Seats`, `Announcements`; transitions `Sit`, `Go To Door`,
  `Clear Announcement`; parameters `stopped` (`true`), `seats` (`4`),
  `capacity` (`4`)). Where the design names one `Go To Door` drawing from
  either of two places, there are two transitions, since an input edge
  draws from one place. Seed: `Depot` holds one `Vehicle {name: "Bus 1",
  kind: "Bus"}` with an empty `Cabin`; `Harbour` one `{name: "Ferry 1",
  kind: "Ferry"}` with an empty `Deck`; `Platform@Depot` holds `"Ada"`,
  `"Bram"`, `"Cato"` in that order; everything else is empty. Transitions,
  in this order: `Board Depot`, `Board Central`, `Board Harbour`,
  `Board Airport`, `Alight Depot`, `Alight Central`, `Alight Harbour`,
  `Alight Airport`, `Depart Depot`, `Depart Central`, `Depart Harbour`,
  `Depart Airport`, `Arrive`. `Board L` takes one passenger from
  `Platform@L` and mutates the front vehicle at `L`, putting the passenger
  into its inner `Door`; its guard is that the front vehicle at `L` has room
  (the tokens in its inner places other than `Announcements` number fewer
  than its `capacity`); it produces nothing. `Alight L` mutates the front
  vehicle at `L`, taking the oldest passenger out of its inner `Door` and
  producing it into `Platform@L`; guard: that `Door` is non-empty. `Depart
  L` takes the front vehicle at `L`, sets its `destination` to the net
  parameter `destination` and its inner `stopped` to `false`, and produces
  it into `In Transit`. `Arrive` takes the front vehicle of `In Transit`,
  sets its inner `stopped` to `true`, puts `"Arrived at <destination>"` into
  its inner `Announcements`, clears its `destination` and produces it into
  the place its destination named. Net parameter `destination` (`"Central"`).
  The transit transcript holds `Alight` and `Depart` back with `disable`
  where the design's script fires by hand, so that the driver's order
  (§12.3) reproduces the script.

  **`wildlife`** ("Species migrate, spread and decline across habitats"),
  the net of `TOKEN_QUERYING_AND_FILTERING.md` §2. Places, in this order,
  all of type `Species`, all serializable, each declaring the keys
  `species` (from `name`), `class` and `diet`: `Forest`, `Wetland`,
  `Savanna`, `Reef`, `Tundra`, `Migrating`. `Species` is `{name, class,
  diet, population, destination}` with `class` one of `"Bird"`,
  `"Mammal"`, `"Reptile"`, `"Fish"`, `diet` one of `"Herbivore"`,
  `"Carnivore"`, `"Insectivore"`, `"Omnivore"`, `population` an integer
  and `destination` a place name or `null` (set only while migrating). Seed,
  in place order then token order: `Forest`: Robin (Bird, Insectivore, 120),
  Fox (Mammal, Omnivore, 60); `Wetland`: Robin (Bird, Insectivore, 80),
  Heron (Bird, Carnivore, 40); `Savanna`: Zebra (Mammal, Herbivore, 5000),
  Gecko (Reptile, Insectivore, 200); `Reef`: Clownfish (Fish, Omnivore,
  300); `Tundra`: Fox (Mammal, Omnivore, 30), Arctic Hare (Mammal,
  Herbivore, 90). Net parameters `season` (`"Summer"`) and
  `spread_threshold` (`1000`). Transitions, in this order:
  `Migrate Forest Wetland`, which takes the oldest `Species` in `Forest`
  whose `class` is `Bird` (a `take_where` edge with the filter
  `class in {Bird}`; until a runtime has keyed edges, a `take_all` of
  `Forest` that puts the others back, slot 0 to `Forest` and slot 1 to
  `Migrating`), sets its `destination` to `"Wetland"` and produces it into
  `Migrating`, guarded by `season` being `"Autumn"`; `Settle`, which takes
  the front token of `Migrating`, clears its `destination` and produces it
  into the place that destination named; `Spread Savanna Reef`, which
  mutates the front token of `Savanna`, halving its population (rounding
  down), and produces a copy with the other half into `Reef`, guarded by
  that token's population exceeding `spread_threshold`; and
  `Decline Forest`, which mutates the front token of `Forest`, halving its
  population (rounding down), guarded by it being above zero. The design's
  "at zero the token is removed" is not built: a mutate edge removes
  nothing.

## 12. Nested nets

A token may hold Petri nets of its own: *inner nets*, held by a token of the
*outer net*, which produces, stores, moves and drops such tokens like any
other. The net process hosts the outer net; inner nets live inside its
tokens and are reached through them, so a viewer can open a token and watch
the net inside it. This is the wire side of `NETS_AS_TOKENS.md` (R8); both
runners implement it.

### 12.1 Addressing: paths and nested ids

An inner net is named in two ways. Every method that takes one accepts
either, as `path` or as `nested_id`; giving both is `invalid_params`;
giving neither names the outer net.

- A **path** is a string: the outer net's name, then, per level of nesting,
  the place, the token (its id with `#` in front) and the inner net's name
  within the token, joined by `/`: `City/Depot/#17/cabin`, and
  `City/Depot/#17/cabin/Door/#3/luggage` one level further down. The first
  segment must be the loaded net's name. A token that is itself a net holds
  one inner net whose name is the empty string, so its path ends in `/`. A
  place or net name containing `/`, or a place name beginning with `#`,
  cannot be spelt in a path; such a net is reached by its nested id. A path
  names a net *as the marking stands*: an outer token that is taken and put
  back gets a new token id (§2), so the paths to its inner nets change,
  while their nested ids do not.
- A **nested id** is a string the runtime mints for each inner net when it
  comes into being, unique within a generation of the outer net and never
  reused in it, opaque to a reader (the runtimes spell it differently;
  compare it as a string, never parse it). It is the stable name of an inner
  net: it survives every move of the token that holds it. It stops resolving
  when that token is consumed without being put back (§12.6), and when the
  outer net is reloaded or reset, which mints new ids for every inner net.

A `path` or `nested_id` that names no live inner net is answered
`unknown_net`, and the message says which segment failed.

### 12.2 Discovering inner nets

- `topology.places[]` carries `nested: true` on a place whose tokens may hold
  nets, and `passive: true` on such a place whose inner nets are stepped only
  by an outer body (§12.3).
- A token that holds nets carries `nets` wherever a token entry is given in
  full (`get_state`, `get_place_tokens`, `get_token`, `query`): a list, in
  the token's own order, of `{name, nested_id, path, template?, count}`: the
  net's name within the token, its nested id, its path as of now, the
  template that built it (§12.5) when the runtime knows one, and `count`,
  the number of tokens in all its places, so a viewer can badge the token
  without a further read. A token without nets has no `nets` field. The
  positional `produced` entries and the `added` and `mutated` notifications
  carry no `nets`: a viewer that wants them reads the token.
- `describe` stays top level: it lists the nets a process can load, never
  their inner nets, which exist only once a net is built.

### 12.3 Reading and driving an inner net

- `get_state`, `get_place_tokens`, `get_token`, `get_log`, `inject`,
  `query`, `distinct` and `set_filter` take `path` or `nested_id` and then
  act on that inner net exactly as on the outer one: its own topology,
  transition states, marking (its own token ids, unique within the inner
  net only, so a `get_token` on an inner token must name the net), progress
  and log. A result about an inner net carries `path` and `nested_id`
  (absent for the outer net); the `net` of an inner `get_state` is the net's
  name within its token, `template?` the template that built it, and
  `generation` the inner net's own, `1` for as long as it exists.
- **Versions are per net.** Each inner net has its own `marking_version`,
  stamped on its reads and raised by exactly one for each of its state
  notifications, under §4's rules. An event inside an inner net does not
  move the outer net's version; the outer `nets[].count` of a token is a
  hint read with the outer state, not a versioned value.
- **Driving.** Inner nets are live. Quiescence of the outer net means no
  fireable transition in it *and none in any inner net* of a non-passive
  place; `execution_stopped {reason: "no_fireable_transitions"}` means that.
  Both runners' sequential drivers fire, at each `step` and at each firing of
  a running net, one transition chosen in this order: the outer net's own
  selector among the outer net's fireable transitions; if it picks none, the
  inner nets in place order, then token order within a place, then net order
  within a token, each by its own selector, descending into a net's own
  inner nets before moving to the next token. The `step` result and the log
  entry of an inner firing carry its `path` and `nested_id`; the
  `marking_version` of a `step` result is the outer net's. An inner net in a
  `passive` place is never fired by the driver; only an outer body steps it.
- **Reads during a firing.** While an outer body runs, a read addressed to
  an inner net of a token that firing took, or holds through a mutate edge,
  is answered after the landing (it may wait, as `get_token` on such a token
  does, §6.5); every other inner net is unchanged by that body, since an
  outer body reaches only the tokens it holds and an inner body only its own
  net, and is answered at once. While an inner body runs, reads of that inner
  net follow §6.3 as for the outer net.
- `enable`, `disable`, `set_param`, `start`, `stop` and `reset` act on the
  outer net only. An inner net's switches and parameters are set by the
  template that builds it or by an outer body reaching inside.

### 12.4 Inner events

An event inside an inner net reaches the host as one `nested` notification
(§7.2) wrapping the notification the inner net would have sent on its own:

```json
{"method": "nested", "params": {
  "path": "City/Depot/#17/cabin", "nested_id": "3",
  "event": {"method": "launched", "params": {"transition": "Sit", "consumed": [["Door", "1"]],
                                             "marking_version": 4, "running": false}},
  "marking_version": 42, "running": false}}
```

`event` is the inner notification, any of §7, with the inner net's
`marking_version` and the process's `running` in its `params`: a firing in
an inner net is `nested` around `launched` then `nested` around
`distributed` (or `failed`), and a harness folds the pair as it folds the
outer one (§11). The wrapper carries the outer net's `marking_version`,
which the inner event does not move, and `running`. Whatever the depth
there is one wrapper, and `path` is the full path. A reader that does not
know `nested` skips it and is untouched by nesting. Before each outer firing
that consumes a nested token, the driver drains the inner net's events into
the outer log, so none are lost when the token is dropped (§12.6).

### 12.5 Nested tokens as JSON

The JSON form of a token that holds nets (`get_token` with `kind: "json"`,
`inject`, and a saved net) is the token's own fields plus, under the name of
each inner net, that net's state:

```json
{"name": "Bus 1", "kind": "Bus", "destination": null,
 "cabin": {"template": "Cabin",
           "marking": {"Door": [], "Seats": ["Ada", "Bram"], "Standing": ["Cato"], "Announcements": []},
           "params": {"stopped": true, "seats": 2, "capacity": 6},
           "enabled": {"Sit": true, "Stand": true, "Go To Door": true,
                       "Go To Door From Standing": true, "Clear Announcement": true}}}
```

- `template` names the factory, registered by name with the runtime, that
  built the inner net and rebuilds it on `inject` or load. `marking` lists
  each place's tokens oldest first, each in the JSON form of its place, so
  every place of the inner net must be `serializable` for the token to have
  a JSON form (a nested place whose inner nets cannot be serialised is not
  `serializable`). `params` are the inner net's parameters by name and
  `enabled` its transition switches. Token ids are not preserved: a loaded
  inner net numbers its tokens afresh. A place missing from `marking` loads
  empty; an unknown place, an unknown template, a parameter the template
  does not declare or a token that does not decode is `invalid_token`.
- `inject` into a nested place takes this form, builds the inner nets from
  their templates and imports their markings, parameters and switches, then
  appends the token and emits `added` for it alone: the inner tokens arrived
  with it and are read with it.

### 12.6 Lifetime

An inner net exists as long as the token that holds it. When an outer firing
consumes the token without putting it back, the inner net and its undrained
events are gone: `launched.consumed` names the token, its nested ids stop
resolving (`unknown_net`), and a viewer attached to one detaches. An outer
firing that takes the token and puts it back, the common move, keeps the
inner net and its nested id and gives the token a new id. `reset` and `load`
drop every inner net with the marking. No runtime changes an inner net's
topology once built; no inner transition reads or writes the outer net; the
outer net reaches inside only through its own bodies.

## 13. Keys, queries and filters

A token may carry *keys*: named attributes a place derives from each token's
value by an extractor the net author declares. Keys are what a query filters
on, what a viewer filters by, and what a host projects with when it serves
one net to several clients that each see their own subset. This is the wire
side of `TOKEN_QUERYING_AND_FILTERING.md` (R3); both runners implement it.

### 13.1 Keys on the wire

- `topology.places[].key_names` lists the keys a place derives, in
  declaration order; it is absent for a place that declares none. (The
  design asks `describe` for them; `describe` lists nets before they are
  built, so they are in the topology, which is per built net.)
- A token entry (§5.3) carries `keys: {name: value}` when its place
  declares keys and no `keys` field otherwise. A value is a JSON string, an
  integer or a boolean, nothing else. Keys are derived from the value, so a
  mutated token may have new keys: `mutated` and `added` carry `keys` under
  the same condition, as they stand after the change, and a `produced` entry
  carries them as its fifth element (§7.1).

### 13.2 Filters

A filter is `{clauses?: [clause], places?: [name], prefix?: string}`; a
clause is `{key, values: [value]}` or `{key, param}`.

- A token matches a filter when it matches every clause (AND across
  clauses) and lies in a place `places` names, if given, whose name begins
  with `prefix`, if given. It matches a clause when it has that key with one
  of the clause's `values` (OR within a clause), compared by JSON type and
  value: `1` is not `"1"` and not `true`. A token in a place that does not
  declare the clause's key matches no such clause. A `{key, param}` clause
  accepts one value, the current value of the named net parameter (keyed
  edges, §13.5).
- A filter with no clauses and no restriction matches every token. A key no
  place declares matches nothing and is not an error; a place in `places`
  that does not exist is `unknown_place`.

### 13.3 Queries

| method | params | result |
|---|---|---|
| `query` | `{filter?, token_limit?, path?, nested_id?}` | `{marking_version, count, matches: [{place, token, json?}]}` |
| `distinct` | `{key, places?, path?, nested_id?}` | `{marking_version, key, values: [value]}` |

- `query` returns the tokens matching `filter` (absent: the connection's
  filter of §13.4, else every token), in place order then token order, each
  as `{place, token, json?}`: `token` a full token entry (§5.3, keys
  included) and `json` the token's JSON value where its place is
  `serializable`. `count` is the number of matches and `matches` holds the
  first `token_limit` of them (default 200, clamped to 1..1000, as
  `get_state`). It reflects the marking at `marking_version` and never waits
  for a running body: it is served from the same marking as `get_state`.
- `distinct` returns the distinct values of `key` over the tokens of the
  net, or of `places`, in order of first appearance in place order then
  token order; a key no place declares gives `[]`.

### 13.4 A filter on the connection

| method | params | result |
|---|---|---|
| `set_filter` | `{filter?, path?, nested_id?}` | `{marking_version}` |

A host serving one net to several clients may need each to see only some
tokens (one character's view, fog of war). `set_filter` sets, for this
connection and the net it names, the filter every later read and
notification of that net is projected through; `set_filter` without
`filter` clears it. A net process has one connection, so one filter per
net; a relay (§9) that serves several clients keeps a filter per client and
applies this same projection itself, which tokens carrying `keys` on the
wire make possible. Under a filter:

- `get_state` and `get_place_tokens` list only matching tokens. Each place
  reports `count`, the true number of tokens, and `matching`, the number the
  filter lets through (present only while a filter is set); `get_place_tokens`
  pages the matching tokens and `has_more` is relative to them. `get_token`
  on a token the filter hides is `unknown_token`.
- `added`, `removed` and `mutated` are sent for matching tokens only. For a
  token the filter hides the connection receives instead
  `hidden {kind, place}` (§7.1), a state notification that moves the
  version as the hidden one would have and names the place and what
  happened, so counts can be kept while nothing of the token is told. A
  mutation that makes a token start matching is reported as `added` and one
  that makes it stop matching as `removed`, although the token did not move;
  `count` in a read is the truth whenever a client doubts its own tally.
- `launched.consumed`, `distributed.produced` and `fired`'s lists hold only
  matching tokens; the notification is sent even when a list comes out
  empty, since it moves the version.
- `query` without a `filter` uses the connection's; `query` with one uses
  that alone and leaves the connection's as it was.
- Ids and versions are unchanged by a filter: a token has the same id on
  every connection, and the versions a filtered connection sees are the net's.

### 13.5 Keyed edges

An input edge may take, or hand over for mutation, the oldest token
*matching a filter* rather than the front token. A transition with such an
edge is fireable when the place holds a matching token (and its guard
passes); its firing takes (or mutates) the oldest matching token and leaves
every other token where it is, with its id, so no `removed` or `added` is
emitted for them. On the wire such an edge is `kind: "take_where"` or
`"mutate_where"` and carries `filter`, the filter of §13.2 as declared (a
`{key, param}` clause is shown as declared, not evaluated).
`launched.consumed` names the token taken.
