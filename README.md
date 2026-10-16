# Petritype

Typed, executable, visual Petri nets in Python.

Petritype turns [Petri nets](https://en.wikipedia.org/wiki/Petri_net) into a practical tool for building and visualising data processing pipelines. Places are typed containers, transitions are real Python functions, and tokens are your actual data — Pydantic models, dataclasses, primitives, whatever you need. Types are enforced at runtime, so wiring errors surface immediately rather than silently propagating.

*Early stage — contributions and feedback welcome.*

<p align="center"><img src="https://raw.githubusercontent.com/olenive/petritype/main/images/docs/illustrations/readme_example/animation.gif" alt="Pipeline animation" width="380"></p>

## Core Idea

A Petri net has two kinds of nodes connected by directed edges:

- **Places** (blue ovals) — typed containers that hold tokens
- **Transitions** (green rectangles) — functions that consume tokens from input places and produce tokens into output places

A transition can only fire when all of its input places have tokens available. When it fires, it pops tokens from input places, calls its function, and routes the result to output places based on type matching. The current set of tokens across all places represents the live state of the system.

Petritype adds a type system on top of this: each place declares the Python type it accepts, and every token is checked against that type. This means the graph itself encodes the shape of your data pipeline — what types flow where, what each function expects, and where different outcomes end up.

## Quickstart

```python
import asyncio
from petritype.core.executable_graph_components import (
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ArgumentEdgeToTransition,
    ReturnedEdgeFromTransition,
)

# 1. Define your types and functions
def double(x: int) -> int:
    return x * 2

# 2. Build the graph
graph = ExecutableGraphOperations.construct_graph([
    ListPlaceNode('Input', int, [1, 2, 3]),
    ArgumentEdgeToTransition('Input', 'Double', 'x'),
    FunctionTransitionNode('Double', double),
    ReturnedEdgeFromTransition('Double', 'Output'),
    ListPlaceNode('Output', int),
])

# 3. Execute
graph, fired = asyncio.run(
    ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=3)
)

print(graph.place_named('Output').tokens)  # [2, 4, 6]
```

`construct_graph` takes a flat list of places, transitions, and edges in any order — it sorts them out. Execution runs an async loop: find fireable transitions, select one, fire it, repeat.

## Key Features

### Runtime type checking

Places declare types. Tokens are validated on entry. If a transition returns a `str` but the output place expects `int`, you get an immediate error — not a silent downstream failure.

```python
ListPlaceNode('Scores', float, [0.95, 0.87])   # only accepts float tokens
ListPlaceNode('Labels', str, ['cat', 'dog'])     # only accepts str tokens
```

Type matching also governs output routing: when a transition has multiple output places, the result is sent to the place whose type matches.

### Static structure checks

For pipeline-shaped nets you can prove termination before running anything: `construct_graph(..., expect_acyclic=True)` (or `ExecutableGraphCheck.assert_acyclic(graph)`) rejects token cycles at build time, naming the offending path (`P1 → T1 → P2 → T2 → P1`). Acyclicity alone is not termination — a *source* transition that consumes no tokens fires forever in a perfectly acyclic net — so pair it with `ExecutableGraphCheck.assert_no_source_transitions(graph)`; a net that passes both provably quiesces. Nets that are cyclic or source-fed on purpose get run-time bounds instead (`error_after_n_firings`).

### Failure semantics

If a transition body raises, the firing fails with `TransitionFailedError`. The tokens it consumed are **not** put back: the body may have mutated them before failing, and a place should never silently hold corrupted tokens — a visible loss beats invisible corruption. The consumed tokens ride along on the error:

```python
try:
    await ExecutableGraphOperations.execute_graph(graph)
except TransitionFailedError as e:
    e.transition_name   # 'Charge Card'
    e.consumed          # {'order': <the consumed token>}
    e.__cause__         # the original exception
```

*Expected* failures are the body's job: catch them and return an error token, and type-based routing (e.g. `-> Receipt | FailedOrder`) delivers it to an error-handling place. If your tokens are immutable or bodies don't mutate them before failing, `restore_tokens_on_failure=True` (on `construct_graph` or per `execute_graph` call) opts into putting consumed tokens back.

### Async execution

Transition functions can be `async`. The execution loop handles both sync and async functions transparently.

```python
async def fetch(url: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.text()

FunctionTransitionNode('Fetch', fetch)
```

### Guards, priorities, and transition selectors

Each transition can carry two optional callables. Both receive the live graph, so they can read the whole marking:

- **`guard`** — a firing condition the engine enforces in every execution mode: a transition whose guard returns `False` is not fireable, regardless of available tokens. Guards run on every fireable-discovery sweep, so keep them cheap, side-effect-free predicates over the marking.
- **`priority`** — a selection hint: the default selector fires the highest-priority fireable transition. Unset scores `0.0`; ties fall back to definition order. `tie_break` (on `construct_graph` or the graph) picks which end: `"first_declared"` (default) runs a FIFO pipeline in batches, every token through stage one before any reaches stage two, while `"last_declared"` lets each token flow through before the next enters, and `"round_robin"` takes the live transitions in turn, each pick the first one declared after the last.

```python
# Guard: BatchProcess is not fireable until the pool is full
def batch_ready(graph) -> bool:
    return len(graph.place_named('Pool').tokens) >= 10

# Priority: drain the longest queue first
def queue_pressure(graph) -> float:
    return len(graph.place_named('Queue').tokens)

FunctionTransitionNode('BatchProcess', batch_process, guard=batch_ready)
FunctionTransitionNode('Drain', drain, priority=queue_pressure)
```

For full control over what fires next, replace the selector itself — a function from the fireable transitions to the one to fire:

```python
graph.transition_selector = my_selector   # (graph, fireable) -> transition or None
```

The older `activation_function` field is deprecated: the engine never consulted it, so use `guard` or `priority` instead. It remains visible to custom selectors until removed.

A selector is just a callable over the fireable transitions, so round-robin, bottleneck-aware and other policies are a few lines each.

### Visualisation

> Needs the `viz` extra: `pip install 'petritype[viz]'`, plus the Graphviz `dot`
> binary on your PATH (`brew install graphviz` / `apt install graphviz`).

Built-in Graphviz rendering shows the graph structure, types, and current token state — including read arcs (dashed) and, in concurrent runs, in-flight transitions highlighted while their bodies run. The example notebooks (marimo) step through and animate execution live.

```python
from petritype.plotting.rustworkx_graph import RustworkxGraph
from petritype.plotting.simple_graphviz import SimpleGraphvizVisualization

# The renderer draws a rustworkx view of the net, built once from the graph
pydigraph = RustworkxGraph.from_executable_graph(graph)

# Static graph image
SimpleGraphvizVisualization.graph(pydigraph)

# Step-by-step animation, inside a notebook (`display` comes from marimo/IPython)
async for step, diagram, fired in SimpleGraphvizVisualization.animate_execution_generator(
    graph, pydigraph
):
    display(diagram)
```

### Output distribution

When a transition has multiple output places, tokens are routed by type matching. For custom routing logic, provide an output distribution function:

```python
def route_result(result) -> dict[str, Any]:
    if result.score > 0.9:
        return {'Approved': result}
    else:
        return {'NeedsReview': result}

FunctionTransitionNode(
    'Classify', classify,
    output_distribution_function=route_result,
)
```

### Token copying

When a transition produces a token that matches multiple output places by type, Petritype raises an error by default — this prevents accidental duplication. If you want the same token to be sent to multiple output places (via `deepcopy`), enable token copying when constructing the graph:

```python
graph = ExecutableGraphOperations.construct_graph([...], allow_token_copying=True)
```

This is useful when the same piece of data needs to flow down multiple independent paths — for example, a configuration token consumed by both a planning stage and a data-fetching stage.

### List-mode transitions

If a transition argument is typed as `list[T]` and the input place holds tokens of type `T`, all tokens are passed as a list in a single call — useful for batch operations.

```python
def summarise(items: list[str]) -> str:
    return f"Processed {len(items)} items"

# All str tokens from 'Items' are passed at once
ArgumentEdgeToTransition('Items', 'Summarise', 'items')
```

### Edges that accept an empty place

Every edge above waits for a token: a transition never fires while one of its input places is empty. For a place a transition only needs to look at when something is there, where none is a perfectly good answer, two edges read **every token, zero or more**, and never hold the transition back. Both need a `list[T]` argument, checked when the graph is built:

- **`DrainEdge`** — takes every token, as a list-mode argument does, but an empty place hands the body `[]` instead of holding it back.
- **`SurveyEdge`** — a deep copy of every token, possibly none; nothing is consumed.

```python
from petritype.core.executable_graph_components import DrainEdge, SurveyEdge

def price_order(order: str, promotions: list[str]) -> tuple[str, list[str]]:
    return order, [f"{promotion} on {order}" for promotion in promotions]

def go(cars: list[str], light: str) -> list[str]:
    return cars

SurveyEdge('Promotions', 'Price Order', 'promotions')  # prices an order whether or not a promotion runs
DrainEdge('Waiting', 'Go', 'cars')                     # every car waiting goes past the light at once
```

A transition whose only argument edge is a drain consumes nothing on an empty place, so `assert_no_source_transitions` counts it as a source. The traffic light's `Go` is one: its guard, green and someone waiting, is what keeps it from firing on an empty queue. The worked examples are `examples/edges_on_empty_places/shop.py` for the survey and `examples/edges_on_empty_places/traffic_light.py` for the drain, beside a plain read; the renderer draws both edges with a hollow arrowhead.

### Read arcs — non-consuming reads

A transition can *read* a place without consuming its tokens — useful for parameters, toggles, or guard state, kept as visible, pokable nodes. Both require a token to be present for the transition to be fireable, but firing never consumes it:

- **`SnapshotEdge`** — the transition receives a deep-copy; the place is untouched.
- **`MutateEdge`** — the transition receives the live tokens and may modify them in place.

```python
from petritype.core.executable_graph_components import SnapshotEdge, MutateEdge

SnapshotEdge('Multiplier', 'Scale', 'factor')   # Scale reads Multiplier, never consumes it
MutateEdge('Counter', 'Scale', 'tally')          # Scale increments Counter in place
```

Read arcs render dashed, distinct from the solid consuming / producing arrows.

### Nets as tokens

A token can hold Petri nets of its own. Declare the place `nested=True`; each token is then an `ExecutableGraph` or any object with a `__petritype_nets__()` method naming the nets it holds. The outer net runs the inner nets as part of running itself (outer transitions first, then inner nets in place, token and net order; `deep_policy="interleaved"` on `construct_graph` or the graph takes the nets in turn instead, so an outer net that can always fire starves none of them), gathers their events into its own log as `Nested` records carrying the path to the net they happened in, and lets its bodies reach inside the tokens they hold:

```python
from petritype.core.nested import NestedNets

class Vehicle(BaseModel):
    name: str
    cabin: ExecutableGraph                       # an inner net, built from a template
    def __petritype_nets__(self):
        return {"cabin": self.cabin}

def board(passenger: str, vehicle: Vehicle) -> None:
    NestedNets.put(vehicle.cabin, "Door", [passenger])   # the only way in: through the token

ListPlaceNode("Depot", Vehicle, [bus], nested=True)
MutateEdge("Depot", "Board Depot", "vehicle")
```

Inner nets never touch the outer net; `passive=True` keeps the engine off them until a body steps them. A net also carries **net parameters** (`construct_graph(..., params={"stopped": True})`), read by guards as `graph.params` and handed to a body that declares a parameter of the same name. `examples/tokens_as_nets/transit.py` is the worked example: buses and ferries whose cabins are nets, each carrying its state as where its door tokens are, in its cabin's `Doors Open` or `Doors Closed`, which the city's guards read, so the net runs its journey on its own.

A renderer marks a token that holds nets with its inner nets' names and token counts (`Vehicle(name='Bus 1', ...) ⊂ cabin: 3`) rather than printing the net; to draw the net inside, render `vehicle.cabin` like any other graph.

Any net whose places all have a JSON form (pydantic models, scalars, nets) can be **saved and loaded**: `graph.export_marking()` / `graph.import_marking(...)` for the marking alone, `NetJson.save(graph)` / `NetJson.restore(saved)` for the whole net (`{template, marking, params, enabled}`), rebuilt through a template registered with `@Templates.template("Cabin")`. A nested token's JSON form carries its inner nets in that saved form, so a vehicle can be written to disk, injected over the protocol and restored, cabin and all.

### Token keys and queries

A place can declare **keys**, named attributes derived from each token, and a net can then be asked for the tokens matching a filter, with their places, ids, keys and values:

```python
from petritype.core.keys import Filter, Query

ListPlaceNode("Forest", Species, [...], keys={"species": lambda s: s.name, "class": lambda s: s.class_})

Query.matches(graph, Filter.where(class_="Bird"))                       # every bird, place by place
Query.matches(graph, Filter.where(class_="Mammal").in_places("Tundra"))  # AND across keys, OR within
Query.distinct(graph, "class")                                           # ['Bird', 'Mammal', ...]
```

Key values are strings, integers or booleans, derived from the value whenever they are read, so a token mutated in place has new keys. A renderer given a filter (`activation_coloured_attr_functions(graph, filter=...)`) lists only the kept tokens, heads each place with `matching / count` and says how many it hides. The same filters drive the net protocol's `query`, `distinct` and per-connection projections (`set_filter`), and **keyed input edges**: `TakeWhereEdge("Forest", "Migrate", "species", where=Filter.where(class_="Bird"))` consumes the oldest matching token and leaves every other token where it is, with its id (`MutateWhereEdge` hands it over live), instead of taking a whole place out and putting the rest back. `examples/token_view_filtering/wildlife.py` is the worked example.

### Decorator for registration

Mark functions as Petri net factories with execution mode metadata, useful for discovery and orchestration tooling.

```python
from petritype import petri_net

@petri_net(name="data-pipeline", mode="batch")
def data_pipeline() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([...])

@petri_net(name="health-check", mode="cron", schedule="*/5 * * * *")
def health_check() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([...])
```

Modes: `manual` (default), `24/7` (continuous), `batch` (run once), `cron` (scheduled).

A net can also declare, beside it, the controls a viewer shows for it and how its places are drawn. The traffic light (`examples/edges_on_empty_places/traffic_light.py`) declares two buttons and fills its light with the light's colour:

```python
from petritype.runtime import ControlSpec, PlaceStyle

@petri_net(
    name="traffic-light",
    controls={
        "car": ControlSpec.inject("Waiting", car, "+ Car", kind="text", config={"placeholder": "name"}),
        "change": ControlSpec.fire("Change", "Change"),
    },
    place_styles={"Light": PlaceStyle(fill="token")},
)
def traffic_light() -> ExecutableGraph:
    return build_traffic_light()
```

The [protocol runner](#net-protocol--run-a-net-as-a-process) serves them in the topology and runs a control when a viewer sends `press` (PROTOCOL.md §5.1, §6.6), so a viewer draws the buttons without knowing the net and the code stays with it: `car` adds the car `car(name, graph)` makes (the name typed, or `Car N` read off the marking), and `change` fires `Change` when it can (the `Fire` command). A token's `.color` is its colour on the wire, and `PlaceStyle(fill="token")` fills a place with the colour of its one token; the light's token is a `Colour`, a `str` whose `color` is the lamp's hex, so its JSON form stays `"red"`. The same control-map renders in a marimo notebook through `petritype.marimo_controls`.

## Runtime — observable, interactive nets

Beyond running a net to completion, `petritype.runtime` turns a net into a **live object you can watch and poke while it runs** — for monitoring, real-time simulations, or interactive tools. The graph is the single source of truth; a `Runner` (a set of functions, no objects to construct) drives it via a passive `RunContext` the caller owns.

```python
from petritype.runtime import Runner, RunContext, ExecutionMode, Extend

ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, observers=(render,))
await Runner.run_to_completion(ctx)   # or Runner.step(ctx) / Runner.run_indefinitely(ctx, tick=0.1)
```

- **One definition, two execution modes** — `SEQUENTIAL` fires one transition fully before the next; `CONCURRENT` runs independent transitions' bodies as overlapping tasks (wall-clock ≈ max instead of sum). Selected by `RunContext.mode` — the only line that changes.
- **Observation** — `observers` are plain callables handed the *live graph* after each state change, so any renderer (a marimo notebook, a web frontend) can redraw at its own pace. In-flight transitions are exposed via `graph.in_flight`, so a renderer could for example highlight them by changing their colour while their bodies run. To know *what fired since you last looked*, snapshot `graph.fired_counts` and diff it at the next notification with `fired_since(previous, current)` — lossless in every mode, unlike `graph.last_fired`, which names only one completion per concurrent batch.
- **Interactive input** — write to a running net by putting typed commands on `RunContext.inbox`, drained between steps: `Extend` / `SetTokens` (places), `SetParam` / `Enable` / `Disable` (transitions). A UI only ever *produces commands* — the runner is the only thing that mutates the net.
- **Real-time** — `run_indefinitely(ctx, tick=...)` drives the net on an internal clock until `ctx.stop`, surviving idle ticks, so you can inject input live.
- **Limits** — `Runner.run(ctx, stop_after_n_firings=N)` paces: it always returns a `RunSummary` whose `quiesced` flag tells "stopped by the limit, call again to continue" from "nothing left to fire". `RunContext.error_after_n_firings` is a run-wide fuse for nets that should quiesce quickly: `TooManyFiringsError` is raised *before* the net fires past it (a net that fires exactly n and quiesces is fine).
- **Offload** — mark a blocking / CPU-bound body `FunctionTransitionNode(..., execution="thread")` and it runs in a thread pool, so it never freezes the loop (and parallelises in concurrent mode).
- **Control-map** — bind UI widgets to nodes declaratively with `{name: ControlSpec}`, kept off the net; `petritype.marimo_controls` renders them and feeds their values to the inbox (needs the `marimo` extra: `pip install 'petritype[marimo]'`).
- **Token ids and events** — every token carries a stable integer id (`place.token_ids`, handed out once and never reused), and with `record_events=True` the graph records `Fired`, `Failed`, `Added`, `Removed`, `Mutated`, `Toggled` and `ParamChanged` for `graph.drain_events()`: what fired, what it consumed and produced, by id, in both execution modes. Off by default, so existing programs see no change.

Runnable examples: `examples/execution_modes/` (sequential vs concurrent, animated) and `examples/interactive/` (live parcel sorters, a read-arc scaler) — open with `uv run --extra examples marimo edit <notebook>`.

## Net protocol — run a net as a process

```bash
python -m petritype.runner --module your.nets            # serve on stdin/stdout
python -m petritype.runner --describe --module your.nets # list the nets it carries
```

`petritype.runner` serves the nets a module declares with `@petri_net` over the
[net protocol](PROTOCOL.md): newline-delimited JSON on stdin and stdout, the
same protocol the Rust port's `petritype-runner` speaks. A host or viewer —
petritype-server, the Bevy visualiser, a test harness — therefore drives
either runtime the same way: `describe`, `load`, `get_state`, `step`,
`start`/`stop`, `inject`, `enable`/`disable`, `set_param`, `get_token`,
`get_log`, with `fired`, `added`, `toggled` and the other notifications as the
net changes. Inner nets of nested tokens are addressed by `path`
(`transit/Depot/#17/cabin`) or by their stable `nested_id`, each with its own
versions and log view, their events wrapped in `nested` notifications. A
`print` in a body goes to stderr; stdout belongs to the protocol. No extra is needed: the runner is the standard library plus
`pydantic`. Conformance is checked by the recorded transcripts under
`tests/protocol/`, which are written to run against both runtimes.

## When to Use This

Petritype is useful when you have stateful data processing where:

1. **Data flows through multiple stages with different representations** — the typed places make the shape of each stage explicit.
2. **Outcomes are not easily predictable** — different result types route to different places, making branching logic visible in the graph rather than hidden in conditionals.
3. **You need to reason about complex processes** — the graph is both the implementation and the documentation.

It has been particularly useful for processes involving many calls to stateful external data sources, where the data flow paths depend on responses and not all paths can be known in advance.

## When Not to Use This

1. **Simple pipelines** — if your processing is a straightforward chain of pure functions, the Petri net overhead adds complexity without benefit.
2. **Order-sensitive processing with shared state** — each transition firing mutates the graph in place. If the order matters and is hard to control, this can be a source of bugs.
3. **Complex net dynamics** — cycles, deadlocks, and infinite loops are all possible in Petri nets and can be difficult to debug. Use the formalism with care.

## Background: What is a Petri Net?

A [Petri net](https://en.wikipedia.org/wiki/Petri_net) is a bipartite directed graph used to model concurrent processes. It was originally developed by Carl Adam Petri in 1962 and has been applied across chemistry, logistics, manufacturing, protocol verification, and many other fields.

The graph has two types of nodes — **places** and **transitions** — connected by directed edges (arcs). Places hold **tokens** representing the state of the system. A transition is fireable when all its input places contain at least one token (and its guard, if any, passes). Petri net texts say "enabled" for what this library calls "fireable"; "enabled" here is the on/off switch set by the `Enable` / `Disable` commands. When a transition fires, it removes tokens from its inputs and adds tokens to its outputs.

What makes Petri nets powerful is that they can model concurrency, synchronisation, and resource contention in a way that is both formally analysable and visually intuitive.

Petritype builds on this foundation by making places typed (so tokens must match a declared Python type) and making transitions executable (so they call real functions). The result is a system where the Petri net is not just a model of your process — it *is* your process.

## Installation

```bash
pip install petritype
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add petritype
```

Requires Python 3.12+. The base install is deliberately light — `pydantic` only.
Optional features live behind extras:

| Extra | Provides | Install |
| --- | --- | --- |
| `viz` | Graphviz rendering (`petritype.plotting`) | `pip install 'petritype[viz]'` |
| `marimo` | Interactive notebook controls (`petritype.marimo_controls`) | `pip install 'petritype[marimo]'` |
| `examples` | Dependencies for running the example notebooks (see [Examples](#examples)) | `pip install 'petritype[examples]'` |

The `viz` extra also needs the Graphviz `dot` binary on your PATH
(`brew install graphviz` / `apt install graphviz`).

## Examples

See the [`examples/`](https://github.com/olenive/petritype/tree/main/examples) directory:

- **Caching** — database retrieval with cache fallback, demonstrating typed routing for cache hits vs misses
- **ML Training** — multi-step model training pipeline with evaluation and retraining loops
- **Time Series** — statistical processing of time series data
- **Transit** (`examples/tokens_as_nets/transit_journey.py`) — nets as tokens: a city whose vehicles hold cabins that are nets of their own, running its journey under plain stepping (each vehicle's state is where its door tokens are, in its cabin's `Doors Open` or `Doors Closed`, which the city's guards read), with the followed bus's cabin drawn beside the city
- **Wildlife** (`examples/token_view_filtering/wildlife_filter.py`) — token keys: species across habitats, drawn through a filter picked from tabs built with `Query.distinct`, with a keyed-edge migration
- **Shop** (`examples/edges_on_empty_places/shop_day.py`) — an edge that accepts an empty place: switch promotions on and off and place orders, and each order is priced with whatever promotions are running through a survey edge, its receipt showing what each one took off. The example day also plays by itself
- **Traffic light** (`examples/edges_on_empty_places/traffic_light_junction.py`) — the edge that takes every token, zero or more: cars wait at a light that cycles red, green and amber (one place, whose token is its colour). Two transitions: `Change` cycles the light by itself, and `Go` drains every car waiting past the light while it is green, reading the light and guarded on green and someone waiting, so a car arriving at green goes straight through. Cars arrive on a schedule or by hand

The notebooks live in the repository — they are not part of the pip package — so
run them from a clone:

```bash
git clone https://github.com/olenive/petritype
cd petritype
uv run --extra examples marimo edit examples/toy/parcel_distribution.py
```
