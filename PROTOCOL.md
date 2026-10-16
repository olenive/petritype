# The petritype net protocol

Status: **protocol version 1.** Written 2026-10-03 and amended the same day
after the first implementation (§2, §4, §5.3, §6.1, §6.3, §6.5, §11), on
2026-10-04 when the Python runner moved to two-piece firings (§6.3, §7.1), and
on 2026-10-09 for nested nets (§12) and token keys, queries and filters (§13),
with what they add to §3, §5, §6, §7, §8 and §11; those additions are the
agreed wire form of `NETS_AS_TOKENS.md` R8 and `TOKEN_QUERYING_AND_FILTERING.md`
R3 (both in `Cyberpunk2020-Simulation/dev-docs/`), written before either
runtime implemented them so that both implement the same thing. On
2026-10-12 it was amended again to close eight gaps in its wording (§2, §6,
§6.4, §7.2, §11, §12.3, §12.5, §13.3, §13.4), which both runners follow, and
on 2026-10-14 it gained two input edge kinds that accept an empty place
(§5.1, §5.2, from `../petritype-rs/dev-docs/EDGES_ON_EMPTY_PLACES.md`), with
a conformance net and a transcript for them (§11). On 2026-10-16 it gained
the controls and place styles a net declares beside it, and `press`, which
runs a control (§3, §5.1, §5.3, §6.6, §7, §11, §12.3). Both
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
  stated otherwise. That is what a runtime writes; what it reads is more
  forgiving: `null` for an optional field of a request counts as the field's
  absence, so `query {filter: null}` is `query {}` and `get_state
  {token_limit: null}` is `get_state {}` (since 2026-10-12).

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
| `unknown_control` | `press` named a control the net does not declare (§6.6) |
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
  the place declares keys (§13.1); `style?`, how a viewer draws the place,
  present only when the net declares one (since 2026-10-16). The one style so
  far is `{"fill": "token"}`, a rule the viewer evaluates: while the place
  holds exactly one token and that token has a `color` (§5.3), the place is
  filled with that colour; a firing's input or output highlight wins while it
  lasts; otherwise the theme's fill. No notification carries a style: the
  viewer already has the token's colour.
- `transitions[]`: `name`; `detail?`, a free object of runtime-specific
  introspection (Python: `function_name`, `signature`, `docstring`; Rust:
  `params`, the parameter type names, and `detachable`). A viewer may show
  `detail`; it must not depend on any key in it.
- `edges[]`: `place`, `transition`, `direction` (`"in"` place to transition,
  `"out"` transition to place); `kind?` for input edges, one of `"take"`,
  `"take_all"`, `"snapshot"`, `"mutate"`, `"take_where"`, `"mutate_where"`,
  `"drain"`, `"survey"` (a reader that meets another value draws a plain
  edge), `take_where` and `mutate_where` carrying `filter` (§13.5); `slot?`
  for output edges of a transition with more than one output. A `drain` edge
  takes every token of its place, oldest first, zero or more: unlike the
  kinds before it, it does not hold its transition back while the place is
  empty, and `consumed` may list nothing from it (since 2026-10-14). A
  `survey` edge hands the body a copy of every token of its place, zero or
  more, consuming none, and likewise never holds its transition back (since
  2026-10-14). Read edges (`snapshot`, `survey`, `mutate`) are listed: they
  move no tokens but they are part of the net. The order of `edges[]` is not
  significant; a reader keys an edge by its endpoints.
- `hints?`: `{"positions": {name: {"x": number, "y": number}}}`. Position
  hints an author or a runtime may supply. **Layout belongs to the viewer**: a
  viewer may honour hints (pinning those nodes) and must work without them.
  No runtime is required to compute a layout.
- `controls?`: the controls the net declares, in declaration order, absent
  when it declares none (since 2026-10-16). A net declares as data the
  buttons it wants; a viewer renders them with widgets it already has, never
  naming a net, and a press runs the control in the runtime that owns the
  net, with code written beside the net (`press`, §6.6). A control is
  `{name, label, kind, node?, action?, config?}`:
  - `name` is unique within the net and is what `press` names; `label` is
    shown.
  - `kind` is the widget, one of `"button"`, `"toggle"`, `"slider"`,
    `"select"`, `"text"`, and fixes the value a press carries: a `button`
    none; a `toggle` a boolean; a `slider` a number within `config.start`
    and `config.stop` (in steps of `config.step`); a `select` one of
    `config.options` (strings); a `text` a string.
  - `config?` is `{value?, options?, start?, stop?, step?, placeholder?}` by
    kind, absent when empty: `value` is the initial value of a kind that has
    one (`toggle`, `slider`, `select`) and absent for the others;
    `placeholder` is a `text`'s hint shown while it is empty. Other keys are a
    runtime's own (a notebook's button `kind`), and a viewer ignores them.
  - `node?` names the place or transition the control acts on. It is for the
    viewer, which may light the node while the control is hovered.
  - `action?` says what kind of thing a press does, for the viewer's lighting
    rule only: `"fire"` is lit while `node` is fireable; `"enable"` is a
    toggle whose state is `node`'s `enabled` switch; `"inject"`, and a control
    without an action, is always lit. A viewer never executes an action.
- `assets?` is reserved for a second round: a table of images a net declares
  once, which a token's `image?` names (§5.3).

### 5.2 Transition state

`transitions` maps each name to:

- `enabled`: the on/off switch set by `enable` and `disable`. Not whether the
  transition can fire now.
- `fireable`: enabled, not in flight, every input place non-empty (but one
  read through a `drain` or `survey` edge, §5.1), guard passes. Both
  libraries use this vocabulary: "fireable" for can-fire-now, "enabled" for
  the switch. petritype-server's engine still says "enabled" for fireable
  until it moves to the library runner.
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
  token (Python: a `.color` attribute; Rust: the style its place is declared
  with, since 2026-10-16).
- `type_name?`: only when it differs from the place's `type_name` (a Python
  place may hold subclasses).
- `keys?`: `{name: value}`, the token's keys, only when its place declares
  keys (§13.1).
- `nets?`: `[{name, nested_id, path, template?, count}]`, the nets the token
  holds, only when it holds some (§12.2).
- `glyph?` is reserved for a second round: one grapheme the viewer draws on
  the token, a field of token entries and `added`.
- `image?` is reserved likewise: the name of an image in `topology.assets?`
  (§5.1) that the viewer draws for the token, a field of token entries and
  `added`; how `produced` carries it and `glyph` is decided when they are
  built.

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
| `inject` | `{place, token, path?, nested_id?}` | `{status: "injected", id, marking_version, path?, nested_id?}` | emits `added`; `path` and `nested_id` for an inner net (§12.3) |
| `enable` / `disable` | `{transition}` | `{marking_version}` | emits `toggled` if it changed |
| `set_param` | `{param, value, transition?}` | `{marking_version}` | optional; `unsupported` otherwise |
| `press` | `{control, value?}` | `{status: "pressed" \| "nothing", reason?, marking_version}` | runs a control the net declares (§6.6); emits the events of whatever it did |
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
  for the body. `step` is the one request answered out of order (with a
  `press` that fires, §6.6): `get_state`, `get_place_tokens`, `get_token`,
  `get_log` and `stop` that arrive while its body runs are answered before it.
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
  `step` and a running net fire, by default, the outer net's choice first and
  an inner net's only when the outer net has nothing fireable (§12.3), and
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
- A parameter value decodes by its declared type: Rust's by the type
  registered for the parameter's wire name, Python's by the annotation of the
  factory parameter of that name, or, for one the factory does not annotate,
  by the JSON type of its current value. So a parameter declared
  `destination: str | None = None` takes a string or `null` in either order
  and refuses a number, and an override can be cleared over the wire (since
  2026-10-12).
- `enable` and `disable` set the switch. Both libraries have it.
- `set_param` is optional. With `transition`, Python sets one keyword
  argument of that transition; without, both runtimes set a net-wide
  parameter by its declared name (Rust ignores `transition`; Python's net
  parameters are `ExecutableGraph.params`, since 2026-10-09). A runtime
  that cannot answers `unsupported`. A name the net does not declare is
  `invalid_params`, never `unsupported` and never an `unknown_*` code.

### 6.5 Reading one token

`get_token {id}` answers `kind: "json"` with `value` exactly when the
token's place is `serializable` (§5.1), and `kind: "text"` with `text`
otherwise. `text` is the runtime's full rendering of the token (Python:
`repr`; Rust: `Debug`), never truncated: `label` (§5.3) is the short form,
this is the long one. A host that wants JSON for a place that has none gets
`kind: "text"`, not an error; `not_serializable` is for `inject`.

### 6.6 Pressing a control

`press {control, value?}` runs one of the controls the net declares
(`topology.controls`, §5.1), since 2026-10-16. The runtime that owns the net
executes it with the code written beside the net; the viewer names the control
and carries the value, nothing more.

- `control` names the control; a name the net does not declare is
  `unknown_control`.
- `value` must match the control's `kind`: absent for a `button` (a `null`
  counts as absent, §2); a boolean for a `toggle`; a number for a `slider`,
  within `config.start` and `config.stop`; one of `config.options` for a
  `select`; a string for a `text`. Anything else, an option not listed and a
  number out of range included, is `invalid_params`, and nothing runs.
- The answer is `status: "pressed"` when the control applied something, and
  `"nothing"` with a `reason` when it chose not to: it gave nothing to do, or
  a `fire` control found its transition not fireable (`"Change is not
  fireable"`). A control whose code raises is `internal`.
- A control that fires a transition fires it as `step` does (§6.3), the
  transition chosen by the control rather than by the net's selector: its
  `launched` and `distributed` (or `failed`), progress and `fireable` follow,
  and the answer is written when the firing is complete. Any other control's
  effects are the events of the change it made, as for `inject`, `enable` or
  `set_param`.
- Controls belong to the outer net: `press` acts on the outer net only, and a
  `path` or `nested_id` is `invalid_params` (§12.3).

## 7. Notifications

Every notification carries `marking_version` (after the change, if it made
one) and `running`. A `press` (§6.6) has no notification of its own: its
effects arrive as the usual notifications, and its answer carries the
`marking_version` after them.

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

A net's first `fireable` is compared with its baseline, the set as it stood
when the runtime first knew of the net: for the outer net at `load` or
`reset`, which `reloaded` already tells, and for an inner net as §12.3 says.
No `fireable` is sent for a baseline itself.

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
runner's state machine, or over pipes against the binary. The conformance
nets below may change until v1 of the protocol is tagged, and each change is
dated where the net is described.

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
- Masks stand in any value position: `"<label>"`, `"<time>"`, `"<id>"` and
  `"<path>"` match any string; `"<ms>"` and `"<version>"` match any number
  (a boolean is not a number); `"<any>"` matches any value, objects and
  arrays included; `{"<unordered>": [...]}` matches an array holding the
  same elements in any order (for `edges[]`, §5.1); `"<id:NAME>"` matches
  any string and binds it for the rest of the transcript, so a later
  `expect` must show the same string and a later `send` has it substituted,
  whole or inside a longer string (`"transit/Depot/#<id:bus>/cabin"`),
  which is how a transcript names a runtime-assigned token id or nested id
  and spells a path (§12.1). Numbers compare by value (`0` matches `0.0`);
  an object must have exactly the expected keys; arrays must have the same
  length and match element by element.
- A transcript shared across runtimes masks every token id, nested id and
  path, and every `marking_version` (§2, §4, §12.1), every `type_name` and
  `label`, `runtime` and `runtime_version`, every `duration_ms` and time,
  and sets every `detail` to `"<any>"`. It carries `color` only where both
  runtimes give the token the same colour, which the conformance nets do by
  giving hex values (`"#rrggbb"`, lowercase), never names (until 2026-10-16 a
  shared transcript used no `color`). `topology.controls` (§5.1) is the net's
  declaration and is pinned as it stands, labels included.
- Before comparing, the harness folds a firing reported in two pieces into
  one, so a transcript recorded against either runtime reads the same
  (§6.3): a `launched` and the next `distributed` of the same transition at
  the same level become one `fired {transition, consumed, produced,
  duration_ms}` that stands where the `distributed` stood, carrying
  `consumed` from the `launched` and `marking_version` and `running` from
  the `distributed`. The lines between the two pieces keep their places
  before it, and only the firing's own may stand there: its inner effects
  (`nested` around `added`, `removed`, `mutated`, `param_changed` or
  `fireable`), its own `mutated`s, and, under a connection's filter, the
  `hidden`, `added` or `removed` that stand in for a mutation (§13.4).
  Inside `nested` wrappers the same fold applies per inner net: a `nested`
  around a `launched` and the next `nested` around its `distributed`, with
  the same `nested_id`, become a `nested` around a `fired` (§12.4). A
  firing that failed is not folded: its `launched` stays, and its `failed`
  follows the lines between. Both the expected and the actual lines are
  folded before they are compared, so a transcript may hold either shape.
  (Until 2026-10-12 the Python harness folded only a pair with nothing
  between; the Rust harness folded as above.)
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

  **`transit`** ("Vehicles board, carry and alight passengers"), the `City`
  net of `NETS_AS_TOKENS.md` §2, redesigned on 2026-10-10 so that it runs its
  journey under plain stepping: each vehicle's state is where the door tokens
  in its cabin are, read by the outer guards and moved by an inner transition
  and by `Arrive`. Places, in this order: `Depot`, `Central`, `Harbour`,
  `Airport` (type `Vehicle`, nested, serializable), `Platform@Depot`,
  `Platform@Central`, `Platform@Harbour`, `Platform@Airport` (type `str`,
  passengers waiting to board), `In Transit` (type `Vehicle`, nested,
  serializable), `Arrived` (type `str`, passengers who have alighted,
  anywhere; an alighted passenger never goes back onto a platform, where it
  would board again). `Vehicle` is `{name, kind, destination, cabin}` with
  `kind` one of `"Bus"`, `"Tram"`, `"Ferry"`, `"Shuttle"`, `destination` a
  place name while in transit and `null` otherwise, and `cabin` its one inner
  net (named `cabin` whatever the kind), in the form of §12.5. Every
  template's places end, after those its passengers occupy, with
  `Announcements`, `Doors Open` and `Doors Closed` (all `str`); the last two
  are control places holding one token per door, the door's name: `Cabin` and
  `Pod` have one door, `"Front"`, and `Deck` two, `"Bow"` then `"Stern"`; a
  freshly built cabin holds every door in `Doors Open`. A door open with no
  announcement standing is boarding, a door open with an announcement
  standing is alighting, and every door closed is under way. The phase is two
  places, not one token naming it, so that the canvas shows it (it is where
  the door tokens are) and more doors are simply more tokens. Every template
  ends with the same two transitions: `Close Doors` (takes one token from
  `Doors Open` and produces it into `Doors Closed`, the door's name passing
  through; guard: `Door` is empty and `Announcements` is empty, everyone
  aboard having settled, with no condition on anybody being aboard, so an
  empty vehicle closes up and moves on rather than strand at a stop in front
  of others; it fires once per open door, so the ferry fires it twice) and
  `Clear Announcement` (consumes one announcement and produces nothing;
  guard: every place passengers occupy, `Door` included, is empty; it does
  not touch the doors). Templates: `Cabin` (Bus, Tram: places `Door`,
  `Seats`, `Standing`, `Announcements`, `Doors Open`, `Doors Closed`, all
  `str`; transitions, in this order, `Sit` (Door to Seats, guard: `Seats`
  holds fewer than `seats`, `Doors Open` is non-empty and `Announcements` is
  empty), `Stand` (Door to Standing, guard: `Seats` holds `seats` or more,
  `Doors Open` is non-empty and `Announcements` is empty), `Go To Door`
  (Seats to Door) and `Go To Door From Standing` (Standing to Door), both
  guarded by `Announcements` being non-empty (an announcement stands only
  while the doors are open: `Arrive` opens them as it puts one up, and `Close
  Doors` refuses while one stands), `Close Doors`, `Clear Announcement`;
  parameters `stopped` (`true`), `seats` (`2`), `capacity` (`6`)); `Deck`
  (Ferry: places `Door`, `Lower Deck`, `Upper Deck`, `Announcements`, `Doors
  Open`, `Doors Closed`; transitions `Go Below` (Door to Lower Deck, guard:
  `Doors Open` is non-empty and `Announcements` is empty), `Go Up` (Lower
  Deck to Upper Deck, no guard), `Go To Door` (Lower Deck to Door) and `Go To
  Door From Upper Deck` (Upper Deck to Door), both guarded by `Announcements`
  being non-empty, `Close Doors`, `Clear Announcement`; parameters `stopped`
  (`true`), `capacity` (`40`)); `Pod` (Shuttle: places `Door`, `Seats`,
  `Announcements`, `Doors Open`, `Doors Closed`; transitions `Sit`, `Go To
  Door`, `Close Doors`, `Clear Announcement`, guarded as the Cabin's;
  parameters `stopped` (`true`), `seats` (`4`), `capacity` (`4`)). `stopped`
  is kept, set by the outer net and shown to viewers, but no guard reads it
  any more: the doors replaced it. Where the design names one `Go To Door`
  drawing from either of two places, there are two transitions, since an
  input edge draws from one place. The route is a fixed loop: `Depot`,
  `Central`, `Harbour`, `Airport`, then `Depot` again. Seed: `Depot` holds
  one `Vehicle {name: "Bus 1", kind: "Bus"}` with a `Cabin` (`seats` 2)
  holding only `Doors Open: ["Front"]`; `Harbour` one `{name: "Ferry 1",
  kind: "Ferry"}` with a `Deck` holding only `Doors Open: ["Bow", "Stern"]`;
  `Platform@Depot` holds `"Ada"`, `"Bram"`, `"Cato"`, `Platform@Central`
  `"Dara"`, `"Eve"` and `Platform@Harbour` `"Finn"`, `"Gwen"`, each in that
  order; everything else is empty. Transitions, in this order: `Board Depot`,
  `Board Central`, `Board Harbour`, `Board Airport`, `Alight Depot`, `Alight
  Central`, `Alight Harbour`, `Alight Airport`, `Depart Depot`, `Depart
  Central`, `Depart Harbour`, `Depart Airport`, `Arrive`. `Board L` takes one
  passenger from `Platform@L` and mutates the front vehicle at `L`, putting
  the passenger into its inner `Door`; it produces nothing; guard: the front
  vehicle at `L` has room (the tokens in its inner places other than
  `Announcements`, `Doors Open` and `Doors Closed` number fewer than its
  `capacity`), its `Doors Open` is non-empty and its `Announcements` is
  empty. `Alight L` mutates the front vehicle at `L`, taking the oldest
  passenger out of its inner `Door` and producing it into `Arrived`; guard:
  that `Door` is non-empty and its `Announcements` is non-empty. `Depart L`
  takes the front vehicle at `L`, sets its `destination` to the net parameter
  `destination` when that is not `null` and otherwise to the stop after `L`
  on the loop, sets its inner `stopped` to `false`, and produces it into `In
  Transit`; guard: the front vehicle's `Doors Open` is empty, every door
  closed. `Arrive` takes the front vehicle of `In Transit`, sets its inner
  `stopped` to `true`, moves every token of its inner `Doors Closed` to its
  `Doors Open` in order (takes them all, then puts them back), puts `"Arrived
  at <destination>"` into its inner `Announcements`, clears its `destination`
  and produces it into the place its destination named. The doors open in
  `Arrive`'s own body: left to an inner transition, they would all be closed
  when the vehicle landed, and `Depart` would fire again before the inner net
  had a turn. Net parameter `destination` (`null`), an override of the loop:
  while it names a place every departure goes there. Its JSON type is string
  or null, and a number is refused with `invalid_params` (§6.4). It is also
  the net's one load parameter, which sets where it starts: `describe` lists
  it as not required, with default `null` (since 2026-10-12, when the Python
  factory came to declare its type by annotating it). Driven by the order of
  §12.3 (the outer net first, the first declared fireable transition by
  default), the seed tells this story: `Board Depot` three times, `Board
  Harbour` twice, the bus's `Sit`, `Sit`, `Stand` and `Close Doors`, `Depart
  Depot`, `Arrive` at Central, then for each passenger `Go To Door` (or `Go
  To Door From Standing`) and `Alight Central`, the bus's `Clear
  Announcement`, `Board Central` twice, and so on round the loop, the ferry's
  deck likewise, closing its two doors with two firings of `Close Doors`:
  after 45 firings all seven passengers are in `Arrived`, and the vehicles
  keep going round, empty. The story holds whatever the tie-break within each
  net as long as the outer net fires first; a driver that gave the cabins the
  first turn would close every vehicle's doors before anyone boarded. The
  transit transcript holds nothing back: it steps that journey from the seed
  with every transition enabled (until 2026-10-10 it held `Alight`, `Depart`
  and `Arrive` back with `disable` and fired them as the design's script
  did).

  **`wildlife`** ("Species migrate, spread and decline across habitats"),
  the net of `TOKEN_QUERYING_AND_FILTERING.md` §2, second edition
  (2026-10-13). Places, in this order, all of type `Species`, all
  serializable, each declaring the keys `species` (from `name`), `class` and
  `diet`: `Forest`, `Wetland`, `Savanna`, `Reef`, `Tundra`, `Migrating`.
  `Species` is `{name, class, diet, population, destination}` with `class`
  one of `"Bird"`, `"Mammal"`, `"Reptile"`, `"Fish"`, `diet` one of
  `"Herbivore"`, `"Carnivore"`, `"Insectivore"`, `"Omnivore"`, `population`
  an integer and `destination` a place name or `null` (set only while
  migrating). Seed, twenty tokens in place order then token order: `Forest`:
  Robin (Bird, Insectivore, 120), Fox (Mammal, Omnivore, 60), Deer (Mammal,
  Herbivore, 300), Woodpecker (Bird, Insectivore, 45); `Wetland`: Robin
  (Bird, Insectivore, 80), Heron (Bird, Carnivore, 40), Pike (Fish,
  Carnivore, 150), Otter (Mammal, Carnivore, 25); `Savanna`: Zebra (Mammal,
  Herbivore, 5000), Gecko (Reptile, Insectivore, 200), Lion (Mammal,
  Carnivore, 40), Egret (Bird, Insectivore, 1500); `Reef`: Clownfish (Fish,
  Omnivore, 300), Parrotfish (Fish, Herbivore, 400), Reef Shark (Fish,
  Carnivore, 20), Sea Turtle (Reptile, Herbivore, 80); `Tundra`: Fox
  (Mammal, Omnivore, 30), Arctic Hare (Mammal, Herbivore, 90), Lemming
  (Mammal, Herbivore, 2000), Snowy Owl (Bird, Carnivore, 15); `Migrating`
  empty. Net parameters `season` (`"Autumn"`) and `spread_threshold`
  (`200`). Transitions, in this order: `Migrate Forest Wetland`, which takes
  the oldest `Species` in `Forest` whose `class` is `Bird` (a `take_where`
  edge with the filter `class in {Bird}`), sets its `destination` to
  `"Wetland"` and produces it into `Migrating`, guarded by `season` being
  `"Autumn"`; `Migrate Tundra Wetland`, the same from `Tundra` with the same
  guard; `Settle`, which takes the front token of `Migrating`, clears its
  `destination` and produces it into the place that destination named;
  `Spread Savanna Wetland`, which mutates the oldest bird of `Savanna` (a
  `mutate_where` edge, `class in {Bird}`), leaving it half its population
  (rounding down), and produces a copy carrying the rest into `Wetland`,
  guarded by that token's population exceeding `spread_threshold`; `Decline
  Forest`, which mutates the oldest herbivore of `Forest` (`mutate_where`,
  `diet in {Herbivore}`), halving its population (rounding down), guarded by
  that population exceeding 10; and `Decline Tundra`, which mutates the
  oldest lemming of `Tundra` (`mutate_where`, `species in {Lemming}`)
  likewise, guarded by its population exceeding 100. Every guard reads the
  token its edge would take, the oldest match of the same filter in the same
  place, never the place's front token; a mutated token keeps its id and its
  place. Driven by the default selector from the seed, a run is nineteen
  firings: `Migrate Forest Wetland` twice (the robin, then the woodpecker),
  `Migrate Tundra Wetland` (the snowy owl), `Settle` three times (all three
  into `Wetland`), `Spread Savanna Wetland` three times (the egret 1500 to
  750, 375 and 187, its copies of 750, 375 and 188 into `Wetland`), `Decline
  Forest` five times (the deer 300 to 150, 75, 37, 18 and 9) and `Decline
  Tundra` five times (the lemmings 2000 to 1000, 500, 250, 125 and 62); then
  nothing is fireable. With `season` `"Summer"` the migrations are not
  fireable and the run is the other thirteen. No population reaches zero, so
  the design's "at zero the token is removed" never arises (a mutate edge
  removes nothing). Until 2026-10-13 the net had nine tokens, `season`
  `"Summer"`, `spread_threshold` `1000` and front-token mutate edges in
  `Spread Savanna Reef` and `Decline Forest`, which split whatever stood
  first in `Savanna` (the zebra) onto the reef and halved the forest's robin
  down to zero.
- One more conformance net came with the `drain` and `survey` kinds of §5.1
  on 2026-10-14: the example net of `EDGES_ON_EMPTY_PLACES.md` (in
  `../petritype-rs/dev-docs/`), a shop that surveyed its promotions and
  drained them at the close, so that the transcript over it
  (`07_empty_place_edges.json`) replayed against both runtimes. On 2026-10-16
  it was split in two, one mechanic each: the `shop` keeps the survey, and a
  new `traffic-light` shows the drain beside a plain read, with a transcript
  of its own (`08_traffic_light.json`). The Python nets are
  `tests/protocol_nets/shop.py` and `tests/protocol_nets/traffic_light.py`,
  which carry `examples/edges_on_empty_places/shop.py` and
  `examples/edges_on_empty_places/traffic_light.py`. Their names, places,
  transitions, guards and bodies are fixed here, since the transcripts depend
  on them:

  **`shop`** ("Price orders with whatever promotions are running"), hosted
  `manual`, with no load parameters and no net parameters. Places, in this
  order, all of type string, all serializable: `Orders` (orders waiting to be
  priced), `Promotions` (promotions running now, usually none), `Priced` (one
  per order priced), `Discounts Applied` (one per promotion applied to an
  order). Seed: every place empty. One transition, unguarded: `Price Order`,
  which takes the front of `Orders` (a `take` edge) and surveys `Promotions`
  (a `survey` edge: every promotion, oldest first, zero or more, none
  consumed), and produces the order into `Priced` (slot 0) and one
  `"{promotion} on {order}"` per promotion, oldest first, into `Discounts
  Applied` (slot 1). Its `consumed` lists the order alone. An empty
  `Promotions` does not hold it back; it is never fireable while `Orders` is
  empty, whatever `Promotions` holds. The transcript drives it with `inject`
  and `step`: `"Order 1"` is priced with no promotion running; `"10% off"`
  and `"Free shipping"` are injected, which makes nothing fireable, then
  `"Order 2"`, priced with both; then a `step` fires nothing. A final
  `get_state` and a `get_token` of each output token pin the marking:
  `Priced` `["Order 1", "Order 2"]`, `Discounts Applied` `["10% off on Order
  2", "Free shipping on Order 2"]`, `Promotions` `["10% off", "Free
  shipping"]` (the survey left them running), `Orders` empty. Until
  2026-10-16 the shop also had `Day End` (a marker token when the shop
  closed), `Expired` and a second transition, `Close Day`, which took the
  marker and drained `Promotions` into `Expired` (`"{promotion} expired"`
  each), and its description ended "and end them at the close"; the
  transcript closed the day twice, with two promotions running and with none.

  **`traffic-light`** ("Cars wait at a light that cycles red, green and
  amber, and all go when it turns green"), since 2026-10-16 (revised the
  same day: two transitions), hosted `manual`, with no load parameters and
  no net parameters. Places, in this order, all of type string, all
  serializable: `Light` (the light: exactly one token, whose value is its
  colour, `"red"`, `"green"` or `"amber"`, and whose `color` is the lamp's:
  red `"#ff6347"`, green `"#32cd32"`, amber `"#ffa500"`), `Waiting` (cars waiting at the
  light, oldest first: `"Car 1"`, `"Car 2"`, ...), `Passed` (every car that
  has gone past the light, in the order they went). Seed: `"red"` in
  `Light`, every other place empty. The colours go round red, green, amber
  and red again. Transitions, in this order (the order matters: at green
  with cars waiting both are fireable, and the first declared fires): `Go`,
  guard: the light is green (the front token of `Light`) and `Waiting` is
  not empty, which drains `Waiting` (a `drain` edge: every waiting car,
  oldest first, all consumed) and reads `Light` (a `snapshot` edge: the
  colour is read, not taken), and produces the cars, oldest first, into
  `Passed`, one token per car; its `consumed` lists the cars, oldest first,
  and the light's token is left as it was, with its id. `Change`, unguarded,
  which takes the light from `Light` (a `take` edge) and produces the next
  colour into `Light`: red to green, green to amber, amber to red. Bodies:
  `Go` returns the cars unchanged, a list, which its one output place takes
  as one token per car; `Change` returns the next colour; a car keeps its
  name. The guard reads the net and is not described on the wire;
  `fireable` is, and both runtimes compute it the same. `Go`'s guard needs
  someone waiting because a drain accepts an empty place: without it `Go`
  would be fireable at every green, and since a `step` fires the first
  declared, it would fire at every green taking nothing and `Change` would
  never fire (the light would stick on green); declared the other way
  round, `Change` would always win at green and no car would go. The guard
  hands the step to `Change` when nobody is waiting, so this net never fires
  a drain on an empty queue (the `shop`'s survey shows the zero case).
  `Change` is always fireable, so the light cycles by itself and the net
  never quiesces; cars come from outside, by `inject` into `Waiting`. `Go`'s
  only consuming edge is a drain, so it is structurally a source transition,
  made safe by its guard; `Change` takes from and produces into `Light`, a
  loop, so the net is cyclic. Each `Change` gives the light a fresh token;
  `Go` leaves it. The transcript drives it with `inject` and `step` from the
  seed: `Change` fires (consuming the light, `"red"`, and producing
  `"green"` into `Light`); `Change` fires (amber); `"Car 1"` and `"Car 2"`
  are injected into `Waiting`, and only `Change` is fireable, so the cars
  wait (`Go` is held by its guard); `Change` fires (red); `Change` fires
  (green), which makes `Go` and `Change` fireable; `Go` fires and drains
  both (consuming `Car 1` and `Car 2` and producing both into `Passed`; the
  light's token stays, with its id), which leaves `Change` fireable; `"Car
  3"` is injected, which makes `Go` and `Change` fireable, and `Go` fires
  (consuming `Car 3` and producing it into `Passed`); `Change` fires
  (amber). A final `get_state` and a `get_token` of each token in `Passed`
  and of the light pin the marking: `Light` `["amber"]`, `Passed` `["Car
  1", "Car 2", "Car 3"]`, `Waiting` empty. Since 2026-10-16 the net also
  declares two controls, in this order: `car`, `{name: "car", label: "+
  Car", kind: "text", node: "Waiting", action: "inject", config:
  {placeholder: "name"}}`, which adds a car to `Waiting` named as typed, or,
  given the empty string, `"Car N"` with `N` one more than the cars in
  `Waiting` and `Passed` together (read off the marking, so both runtimes name
  it alike); and `change`, `{name: "change", label: "Change", kind:
  "button", node: "Change", action: "fire"}`, which fires `Change` when it is
  fireable. `Light` has the style `{fill: "token"}`. The light's token
  entries and every `Change` firing's produced light carry its colour; cars
  carry none. Six exchanges follow the final reads: `press change` fires
  `Change` (amber to red, the produced light carrying `"#ff6347"`) and
  answers `pressed`; `press car` with `value: ""` adds `"Car 4"` to
  `Waiting`, and with `value: "Taxi"` adds `"Taxi"`, each `added` then
  `pressed`; `press car` with `value: 7` is `invalid_params`, `press nope`
  `unknown_control`, and `press change` with `value: true`
  `invalid_params`. The fireable set stays `Change` alone through them, so
  no `fireable` is sent, and the marking ends with `Light` `["red"]`,
  `Waiting` `["Car 4", "Taxi"]` and `Passed` `["Car 1", "Car 2", "Car 3"]`.
  On 2026-10-16 the light went from three places (`Red`, `Green`, `Amber`)
  to one place (with three transitions: `Turn Green`, `Pass on Green`,
  `Change`) to these two transitions, and then gained its controls and its
  colours.

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
  (absent for the outer net), `inject`'s included: `{status, id,
  marking_version, path, nested_id}`, with the inner net's version (since
  2026-10-12); the `net` of an inner `get_state` is the net's
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
  That order is the default. A net built with the interleaved deep-step
  policy (Python `deep_policy="interleaved"`, Rust `DeepPolicy::Interleaved`,
  since 2026-10-12) is driven round a cycle instead, the outer net and then
  the inner nets in that same order: each firing goes to the first net with
  something to fire, counting from the net after the one the last firing was
  in, so that an outer net that can always fire does not starve the inner
  ones; the policy, like the tie-break within each net, is the runtime's own
  and nothing on the wire changes (`DESIGN_RUNNER.md`, Rust `DESIGN.md`
  §6.6).
- **Baselines.** An inner net's `fireable` baseline (§7.2) is taken when the
  runtime first knows of the net: at `load` for the inner nets the factory
  seeded, at the `inject` that carried the token, or at the landing of the
  outer firing that produced the token. No `fireable` is sent for the
  baseline itself; the first `nested` `fireable` for that net reports the
  first change from it. `reset` and `load` retake every baseline, the nets
  being new (since 2026-10-12).
- **Reads during a firing.** While an outer body runs, a read addressed to
  an inner net of a token that firing took, or holds through a mutate edge,
  is answered after the landing (it may wait, as `get_token` on such a token
  does, §6.5); every other inner net is unchanged by that body, since an
  outer body reaches only the tokens it holds and an inner body only its own
  net, and is answered at once. While an inner body runs, reads of that inner
  net follow §6.3 as for the outer net.
- `enable`, `disable`, `set_param`, `start`, `stop`, `reset`, `step` and
  `press` act on the outer net only; any of them given `path` or `nested_id`
  is `invalid_params` (`step` named here since 2026-10-12: its result names
  the net a firing was in, its request names none; `press` since 2026-10-16,
  the controls being the outer net's). An inner net's switches and
  parameters are set by the template that builds it or by an outer body
  reaching inside.

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

Order within an outer firing: what its body did inside the tokens it holds
(the `nested` lines for the inner `added`, `removed` and `param_changed`)
comes after its `launched` and before its own `mutated`s and `distributed`
(or `failed`); the path on those lines is the token's place once the firing
has placed it (`In Transit` for a `Depart`), since that is where the net is
when the lines are written. The `fireable` of the outer net, if it changed,
precedes the `nested` `fireable` of each inner net the firing touched.

### 12.5 Nested tokens as JSON

The JSON form of a token that holds nets (`get_token` with `kind: "json"`,
`inject`, and a saved net) is the token's own fields plus, under the name of
each inner net, that net's state:

```json
{"name": "Bus 1", "kind": "Bus", "destination": null,
 "cabin": {"template": "Cabin",
           "marking": {"Door": [], "Seats": ["Ada", "Bram"], "Standing": ["Cato"], "Announcements": [],
                       "Doors Open": ["Front"], "Doors Closed": []},
           "params": {"stopped": true, "seats": 2, "capacity": 6},
           "enabled": {"Sit": true, "Stand": true, "Go To Door": true,
                       "Go To Door From Standing": true, "Close Doors": true, "Clear Announcement": true}}}
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
  with it and are read with it. Its nets are known from then on (§12.3,
  baselines). An `inject` addressed to an inner net, of this form or any
  other, is answered with that net's `path`, `nested_id` and version
  (§12.3).

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

- `query` returns the tokens matching `filter` (absent, or `null` as §2
  says: the connection's filter of §13.4, else every token), in place order
  then token order, each as `{place, token, json?}`: `token` a full token
  entry (§5.3, keys included) and `json` the token's JSON value where its
  place is `serializable`. `count` is the number of matches and `matches`
  holds the first `token_limit` of them (default 200, clamped to 1..1000, as
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
  token that comes to match is reported as `added` and one that stops
  matching as `removed`, although the token did not move: by a mutation
  (in place of its `mutated`), or by a change of a net parameter a clause
  names, which is followed by an `added` or `removed` for each token it
  brings into or out of view (after its `param_changed`). `count` in a read
  is the truth whenever a client doubts its own tally.
- `launched.consumed`, `failed.consumed`, `distributed.produced` and
  `fired`'s lists hold only matching tokens (a `failed` lists what its
  `launched` listed); the notification is sent even when a list comes out
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
