# The protocol runner and the library changes behind it

`python -m petritype.runner` serves the nets a module declares with `@petri_net`
over the net protocol, newline-delimited JSON on stdin and stdout, so that any host
or viewer can run a Python net by spawning a process. The protocol itself is
[`PROTOCOL.md`](PROTOCOL.md)
(<https://github.com/olenive/petritype/blob/main/PROTOCOL.md>); the runner speaks
version 2 (since 2026-11-21) and replays the shared transcripts of §11. What changed
in each release is in `CHANGELOG.md`.

This note says where the runner lives in the code and records the decisions behind
it, and behind the library changes it needed, whose reasons the code does not hold.
Design of 2026-10-03, built the same day and extended with each protocol change since.
House conventions apply: namespace classes of functions, passive dataclasses and
pydantic models for state, flat `tests/test_*.py`.

## Where to look

- `petritype/runner/protocol.py`: the wire shapes as pydantic models.
- `petritype/runner/registry.py`: entries from `@petri_net` factories, `describe`,
  load parameters, the version 0 `load` form.
- `petritype/runner/translate.py`: type names, labels and colours, JSON forms, transition
  detail, topology, filters, paths, controls.
- `petritype/runner/owner.py`: the state machine. Its module docstring covers nested
  nets, subnet places and the reads while a body runs.
- `petritype/runner/process.py`: the command line, the stdout guard, the threads and
  exit, with the start-up order and its reasons in the module docstring.
- Tests: `tests/test_runner_*.py`; the shared transcripts `tests/protocol/*.json` over
  the conformance nets in `tests/protocol_nets/`, replayed by
  `tests/test_protocol_transcripts.py` (through `Owner` and over pipes) and recorded
  with `tests/record_transcript.py`.
- What the runner needs of the library: token ids (`TokenIds`) and events
  (`petritype/core/events.py`), below.

## Decisions

### The net-process protocol lives in the library (2026-10-03)

The NDJSON protocol that petritype-server's `worker/net_runner.py` spoke became this
optional module, which petritype-server imports instead of owning a copy. Why: any
viewer can then host a Python net by spawning a process, with no server (the Bevy
visualiser was the first; a test harness or a notebook can be next), and the protocol
stops being defined by one consumer's code. Translation for version 0 clients lives in
petritype-server's worker, so the library never carries the old shapes; the runner
still accepts the version 0 `load {module, function, factory_params}` form.

### Token ids and events (2026-10-03)

`ExecutableGraph` gives every token a monotonic integer id when it enters a place and,
with `record_events=True`, reports per firing which ids were consumed from which places
and which were produced into which, matching the Rust crate's `TokenId` and `Event`.
Why in the library: one source of truth. The server used to diff `id(token)` before and
after each step, but a memory address is reused once a token is freed, two equal small
integers share one address, and a diff cannot tell a token that moved from one consumed
and one produced. The engine knows exactly what it took and what it put.

- The ids sit in a private list beside `place.tokens`, so `place.tokens` keeps its type
  and every existing reader keeps working. Code that assigns `place.tokens` directly
  bypasses them, which `TokenIds.resync` repairs by comparing lengths (the runner says
  `reloaded {reason: "resync"}`). Correct, if blunt: the price of keeping `place.tokens`
  a plain list.
- `record_events` is off by default, so a program that never drains does not grow a
  list for ever; the runner turns it on.
- The engine reports a firing once, when it completes (`Fired` or `Failed`). The runner
  reports it on the wire in two pieces, `launched` and then `distributed` or `failed`, as
  the Rust runner does (since 2026-10-04): one piece hid the body, so a viewer of a slow
  body saw nothing but heartbeats until it landed.
- `Toggled` and `ParamChanged` come from the runtime's command application, because the
  switch the runner drives lives on `RunContext.disabled`, not on the graph.

### Layout leaves the net process (2026-10-03)

The runner sends topology and optional position hints, never coordinates it computed,
and grandalf is a dependency of neither the library nor the runner. Why: every runner in
every language would otherwise have to ship a layout, and the viewers have their own.

### "Fireable" for can-fire-now, "enabled" for the switch (2026-10-03)

`ExecutableGraphCheck.transition_is_enabled` became `transition_is_fireable`, matching
the Rust port and the protocol; the old name stays as an alias that warns.

### The owner does no I/O

`Owner` turns each request, each landed firing and each tick into the lines to write. It
creates no tasks or threads and reads no clock but the `now` it is handed, as the Rust
runner's owner does, so the whole protocol is tested by feeding it inputs, and the
process layer and the transcript harness drive it the same way.

### Reads while a body runs come from a snapshot

Synchronous bodies run on a thread (the owner marks every sync transition
`execution="thread"`), and a thread is not bound by the event loop's await points:
through a mutate edge it can change a token, or a whole live list, while a read iterates
it. So each launch captures a shallow copy of the marking as its `launched` describes
it, and reads during the body are served from that. The rule is PROTOCOL.md §6.3; the
mechanism and its nested cases are in `owner.py`'s docstring, "Reads while a body runs".
It replaced the server's capped and complete snapshots refreshed after every mutation.

### Strict decoding at the wire (2026-10-03)

Pydantic's lax mode coerced `"9"` to `9` at the three wire boundaries (`inject`,
`set_param`, load parameters); Oleg ruled that out. They validate in strict JSON mode,
for the reason given at `JsonForm.validate`.

### The process layer

The start-up order, the writer, the heartbeat and exit are in `process.py`'s docstring
with their reasons. One reason it does not give: the reader uses `os.read` on
descriptor 0 with its own line splitting, because a daemon thread holding `sys.stdin`'s
lock can make interpreter shutdown fatal.

### Conformance nets can be replayed

A conformance net is the example itself where the example is deterministic (`transit`,
`wildlife`, `shop`, `traffic-light`). `tests/protocol_nets/parcel_distribution.py` is the
Rust example copied exactly, because the Python original under `examples/toy/` creates
parcels from nothing, uses wall-clock guards and randomness, and cannot be replayed. Transcripts are
recorded with `tests/record_transcript.py`, not written by hand.

### Paths are structured, never text (2026-11-21)

Nothing in the runner builds or parses an address as text, so a place or a net may be
named with a `/` or a leading `#` and still be reached by path
(`tests/protocol/15_odd_names.json`). The library's path is a tuple of its `PathStep`s
and the wire's a list of steps; they stay two types because the wire keeps a token id as
a string (PROTOCOL.md §2).

### Read edges are a class family of their own (2026-11-23)

`ReadEdge`, `ReadWhereEdge` and `ReadAllEdge` sit beside the snapshot and mutate edges,
not under them, so that each path that copies a token or records a mutation keeps to the
kinds it had.

### A topology is read tolerantly (2026-11-23)

The models a topology is read into keep a field they do not know (`protocol.Tolerant`),
and every other wire model still refuses one. The topology's models trade that guard on
the writer for reading what a newer runtime writes; the recorded transcripts guard what
this runner writes there.

## Open / known gaps

- No signal handlers on Windows, and the descriptor redirect is untested there.
- `not_serializable` is covered by this library's own tests
  (`tests/test_runner_owner.py`) but not by the shared transcripts: that needs a second
  token type in a conformance net, since Python decides serializability per type.
- The snapshot taken at each launch copies every place; it could copy only the places the
  body can touch (its mutate edges).
