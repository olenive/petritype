# Transition Selection and Activation Functions

This document describes the new `transition_selector` and `activation_function` features added to Petritype.

## Overview

Two new optional fields enable flexible control over which transitions fire:

1. **`activation_function`** on `FunctionTransitionNode` - Transition-level function
2. **`transition_selector`** on `ExecutableGraph` - Graph-level function

Both fields are completely optional and preserve existing behavior when not provided.

## activation_function

An optional function attached to individual transitions. The selector can interpret this however it wants.

### Usage

```python
FunctionTransitionNode(
    name="MyTransition",
    function=my_function,
    activation_function=my_activation,  # Optional
)
```

### Common Patterns

The `activation_function` has no prescribed signature or return type. Selectors determine how to interpret it.

**Pattern 1: Guard (returns bool)**
```python
def my_guard():
    """Returns True if transition can fire."""
    return time.time() > start_time + 5.0

FunctionTransitionNode(
    name="Delayed",
    function=process,
    activation_function=my_guard,
)
```

**Pattern 2: Priority (returns float)**
```python
def calculate_priority():
    """Returns priority score (higher = more important)."""
    return queue_size * 2.5

FunctionTransitionNode(
    name="Process",
    function=process,
    activation_function=calculate_priority,
)
```

**Pattern 3: Context-Aware (takes ExecutableGraph)**
```python
def check_pool_size(graph: ExecutableGraph):
    """Only fire if pool has enough items."""
    pool = graph.place_named("Pool")
    return len(pool.tokens) >= 10

FunctionTransitionNode(
    name="Batch",
    function=batch_process,
    activation_function=check_pool_size,
)
```

**Pattern 4: Countdown Timer (returns seconds remaining)**
```python
def countdown():
    """Returns seconds until ready (0 = ready now)."""
    elapsed = time.time() - start_time
    return max(0, 5.0 - elapsed)

FunctionTransitionNode(
    name="Timed",
    function=process,
    activation_function=countdown,
)
```

## transition_selector

An optional function that chooses which transition to fire from the enabled list.

### Signature

```python
def my_selector(
    graph: ExecutableGraph,
    enabled_transitions: list[FunctionTransitionNode]
) -> Optional[FunctionTransitionNode]:
    """Select which transition to fire.

    Args:
        graph: Full graph context (places, tokens, history, etc.)
        enabled_transitions: Transitions with sufficient input tokens

    Returns:
        Transition to fire, or None to stop execution
    """
    # Your selection logic here
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

If no selector provided, defaults to current behavior: fire last enabled transition (reversed order).

```python
def default_selector(graph: ExecutableGraph, enabled: list[FunctionTransitionNode]):
    """Default: fire last enabled transition."""
    return enabled[-1] if enabled else None
```

## Example Selectors

### Guard-Based Selector

Respects `activation_function` as guards (bool):

```python
def guard_based_selector(graph: ExecutableGraph, enabled: list[FunctionTransitionNode]):
    """Fire first transition whose guard returns True."""
    for t in enabled:
        if t.activation_function is None:
            return t  # No guard = always ready

        # Try calling with graph, fall back to no args
        try:
            result = t.activation_function(graph)
        except TypeError:
            result = t.activation_function()

        if result:  # Guard passed
            return t

    return None  # All guards blocked
```

### Priority-Based Selector

Interprets `activation_function` as priority scores:

```python
def priority_based_selector(graph: ExecutableGraph, enabled: list[FunctionTransitionNode]):
    """Fire highest priority transition."""
    if not enabled:
        return None

    def get_priority(t: FunctionTransitionNode) -> float:
        if not t.activation_function:
            return 0.0
        try:
            return t.activation_function(graph)
        except TypeError:
            return t.activation_function()

    return max(enabled, key=get_priority)
```

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

```python
import time
from petritype.core.executable_graph_components import (
    ExecutableGraph,
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

# Activation functions
fast_start = time.time()
def fast_ready():
    """Fast process always ready."""
    return True

slow_start = time.time()
def slow_ready():
    """Slow process needs 5 second delay."""
    return time.time() - slow_start >= 5.0

# Custom selector
def guard_selector(graph: ExecutableGraph, enabled: list[FunctionTransitionNode]):
    """Fire first transition whose guard passes."""
    for t in enabled:
        if t.activation_function and not t.activation_function():
            continue
        return t
    return None

# Build graph
graph = ExecutableGraphOperations.construct_graph([
    ListPlaceNode('Input', int, [1, 2, 3, 4, 5]),

    # Fast transition - always ready
    ArgumentEdgeToTransition('Input', 'Fast', 'x'),
    FunctionTransitionNode(
        'Fast',
        fast_process,
        activation_function=fast_ready,
    ),
    ReturnedEdgeFromTransition('Fast', 'Output'),

    # Slow transition - needs 5 second delay
    ArgumentEdgeToTransition('Input', 'Slow', 'x'),
    FunctionTransitionNode(
        'Slow',
        slow_process,
        activation_function=slow_ready,
    ),
    ReturnedEdgeFromTransition('Slow', 'Output'),

    ListPlaceNode('Output', int),
])

# Set selector
graph.transition_selector = guard_selector

# Execute - will fire Fast until 5 seconds pass, then Slow becomes available
await ExecutableGraphOperations.execute_graph(
    graph,
    max_transitions=10,
)
```

## Design Philosophy

- **Minimal structure**: No prescribed protocols or base classes
- **Maximum flexibility**: Users define their own semantics
- **Composable**: Mix and match patterns
- **Optional**: Preserves existing behavior when not used
- **Context-aware**: Selectors receive full graph state

## Testing

See `tests/test_transition_selection.py` for comprehensive test examples.

## Future Patterns

Users are encouraged to experiment with new patterns:
- Cost-based selection
- Resource-aware selection
- Deadline-based scheduling
- Probabilistic selection
- Machine learning-based selection
- And more!

The framework doesn't prescribe - it enables.
