# Transition Selection: Guards, Priorities, and Selectors

This document describes the three levers that control which transition fires next.

## Overview

Every firing is decided in two steps:

1. **Enablement** — the engine collects the transitions that *may* fire: every input
   place has tokens **and** the transition's `guard` (if any) passes. This is part of
   the net's semantics and applies in every execution mode.
2. **Selection** — one enabled transition is picked to *actually* fire, by the
   `transition_selector`. The default selector honours each transition's `priority`.

Three optional fields hook into this:

| Field | Lives on | Step | Signature | Meaning |
|---|---|---|---|---|
| `guard` | `FunctionTransitionNode` | 1 — enablement | `(ExecutableGraph) -> bool` | Engine-enforced enabling condition |
| `priority` | `FunctionTransitionNode` | 2 — selection | `(ExecutableGraph) -> float` | Hint read by the default selector |
| `transition_selector` | `ExecutableGraph` | 2 — selection | `(graph, enabled) -> transition \| None` | Full control over what fires |

All are optional; a net using none of them behaves as before (first enabled
transition in definition order fires).

## guard

An enabling condition the engine checks alongside token availability, in every
execution mode — sequential `execute_graph` and both concurrent runner loops. A
transition whose guard returns `False` is simply not enabled: it never reaches the
selector, and in Petri net terms this is a transition guard in the coloured-net
sense.

```python
def batch_ready(graph) -> bool:
    """Only enabled once the pool is full."""
    return len(graph.place_named("Pool").tokens) >= 10

FunctionTransitionNode("Batch", batch_process, guard=batch_ready)
```

Guards receive the live graph, so they can read the whole marking. A common use is
graceful termination of a cycle that would otherwise churn forever — see
`examples/toy/match_up_tokens/01_match_lengths.py`, where the guard disables the
matching transition once no match remains.

**Keep guards cheap and side-effect-free.** A guard runs on every enabled-discovery
sweep (every loop iteration, for every transition that has one), so it should be a
quick predicate over the marking — never a place to do work, mutate state, or block.
`guard=None` costs nothing: the check is skipped entirely.

## priority

A selection hint: the default selector fires the enabled transition with the highest
priority. Transitions without one score `0.0`, and ties fall back to definition
order — so a net where nobody sets a priority keeps the first-enabled-in-definition-
order behaviour.

```python
def queue_pressure(graph) -> float:
    """Drain the longest queue first."""
    return len(graph.place_named("Queue").tokens)

FunctionTransitionNode("Drain", drain, priority=queue_pressure)
```

Priorities also receive the live graph, so they can depend on the current marking.
They are only called on transitions that already passed enablement, once per firing
decision — cheaper ground than a guard, but the same advice applies: read, don't
work. A constant priority is just `lambda graph: 5.0`.

Note that priority is *policy*, not semantics: a custom `transition_selector`
replaces the default and is free to ignore `priority`. In the concurrent runner
there is no selection step at all — every enabled transition launches — so
priorities have no effect there (guards still do).

## transition_selector

For full control over selection, replace the selector itself.

### Signature

```python
def my_selector(
    graph: ExecutableGraph,
    enabled_transitions: list[FunctionTransitionNode]
) -> Optional[FunctionTransitionNode]:
    """Select which transition to fire.

    Args:
        graph: Full graph context (places, tokens, history, etc.)
        enabled_transitions: Transitions that passed enablement (tokens + guard),
            in definition order

    Returns:
        Transition to fire, or None to stop execution
    """
    return enabled_transitions[0] if enabled_transitions else None
```

### Setting the Selector

**Option 1: On the graph**
```python
graph = ExecutableGraphOperations.construct_graph([...])
graph.transition_selector = my_selector
```

**Option 2: As parameter to execute_graph**
```python
await ExecutableGraphOperations.execute_graph(
    graph,
    transition_selector=my_selector,
)
```

Parameter selector overrides graph selector if both provided.

### Default Behavior

If no selector is provided, the default fires the **highest-priority enabled
transition**, with ties broken by definition order (see `priority` above). Guards
have already been applied by the time any selector runs — a selector never sees a
guard-blocked transition. (Tokens within a place are consumed FIFO — oldest first.)

## Example Selectors

### Random Selector

```python
import random

def random_selector(graph: ExecutableGraph, enabled: list[FunctionTransitionNode]):
    """Fire random enabled transition."""
    return random.choice(enabled) if enabled else None
```

### Round-Robin Selector

```python
def round_robin_selector(graph: ExecutableGraph, enabled: list[FunctionTransitionNode]):
    """Fire transitions in round-robin order."""
    if not enabled:
        return None

    # Use graph history to determine last fired
    if graph.transition_history:
        last_name = graph.transition_history[-1].name
        names = [t.name for t in enabled]
        if last_name in names:
            idx = names.index(last_name)
            return enabled[(idx + 1) % len(enabled)]

    return enabled[0]
```

### Context-Aware Selector

```python
def bottleneck_aware_selector(graph: ExecutableGraph, enabled: list[FunctionTransitionNode]):
    """Prioritize transitions that clear bottlenecks."""
    if not enabled:
        return None

    # Find place with most tokens
    bottleneck = max(graph.places, key=lambda p: len(p.tokens))

    # Prefer transitions that consume from bottleneck
    for t in enabled:
        # Check if this transition takes input from bottleneck
        for edge in graph.argument_edges:
            if edge.transition_node_name == t.name and edge.place_node_name == bottleneck.name:
                return t

    # No bottleneck consumer, just pick first
    return enabled[0]
```

## Complete Example

Two transitions compete for the same input; the slow one is held back by a guard
until five seconds have passed. No custom selector needed.

```python
import time
from petritype.core.executable_graph_components import (
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ArgumentEdgeToTransition,
    ReturnedEdgeFromTransition,
)

# Domain functions
def fast_process(x: int) -> int:
    return x * 2

def slow_process(x: int) -> int:
    time.sleep(1)
    return x * 3

# Guard: Slow is not enabled for the first 5 seconds
start = time.time()
def slow_ready(graph) -> bool:
    return time.time() - start >= 5.0

# Build graph
graph = ExecutableGraphOperations.construct_graph([
    ListPlaceNode('Input', int, [1, 2, 3, 4, 5]),

    # Fast transition - always enabled while Input has tokens
    ArgumentEdgeToTransition('Input', 'Fast', 'x'),
    FunctionTransitionNode('Fast', fast_process),
    ReturnedEdgeFromTransition('Fast', 'Output'),

    # Slow transition - guard holds it back for 5 seconds
    ArgumentEdgeToTransition('Input', 'Slow', 'x'),
    FunctionTransitionNode('Slow', slow_process, guard=slow_ready),
    ReturnedEdgeFromTransition('Slow', 'Output'),

    ListPlaceNode('Output', int),
])

# Execute - fires Fast until 5 seconds pass, then Slow becomes enabled too
await ExecutableGraphOperations.execute_graph(
    graph,
    max_transitions=10,
)
```

## Deprecated: activation_function

`FunctionTransitionNode.activation_function` predates `guard` and `priority` and
conflated both roles: it had no prescribed signature or meaning, and — crucially —
**the engine never consulted it**. It only did anything if a custom selector chose
to read it, so attaching a "guard" with the default selector silently did nothing.

It is deprecated (constructing a node with it emits a `DeprecationWarning`) and
will be removed in a future release. Until then it keeps its old behaviour: inert
to the engine, visible to custom selectors.

Migration:

| Old pattern | Replacement |
|---|---|
| `() -> bool` guard + guard-honouring selector | `guard=lambda graph: ...` (no selector needed) |
| `() -> float` priority + priority selector | `priority=lambda graph: ...` (no selector needed) |
| Countdown timer (`seconds remaining`) | `guard=lambda graph: time.time() >= deadline` |
| Anything selector-specific | Keep the custom selector; read your own fields |

Both replacements take the graph as their single argument — no more probing with
`try: fn(graph) except TypeError: fn()`.

## Design Philosophy

- **Semantics vs policy**: `guard` is part of the net's meaning (enforced by the
  engine everywhere); `priority` and selectors are scheduling policy on top.
- **Minimal structure**: selectors are plain functions; no protocols or base classes.
- **Optional**: nets using none of the hooks behave exactly as before.
- **Context-aware**: guards, priorities, and selectors all receive the live graph.

## Testing

See `tests/test_guards_and_priorities.py` for guard/priority semantics and
`tests/test_transition_selection.py` for selector behaviour.

## Future Patterns

Users are encouraged to experiment with new patterns:
- Cost-based selection
- Resource-aware selection
- Deadline-based scheduling
- Probabilistic selection
- Machine learning-based selection
- And more!

The framework doesn't prescribe - it enables.
