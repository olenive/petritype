"""Tests for transition_selector and activation_function features."""

import pytest
import asyncio
from petritype.core.executable_graph_components import (
    ExecutableGraph,
    FunctionTransitionNode,
    ListPlaceNode,
    ArgumentEdgeToTransition,
    ReturnedEdgeFromTransition,
    ExecutableGraphOperations,
)


class TestActivationFunction:
    """Tests for the activation_function field on transitions."""

    def test_activation_function_optional(self):
        """Test that activation_function is optional and defaults to None."""
        transition = FunctionTransitionNode(
            name="Test",
            function=lambda x: x + 1,
        )
        assert transition.activation_function is None

    def test_activation_function_can_be_set(self):
        """Test that activation_function can be set."""
        def my_activation():
            return True

        transition = FunctionTransitionNode(
            name="Test",
            function=lambda x: x + 1,
            activation_function=my_activation,
        )
        assert transition.activation_function == my_activation
        assert transition.activation_function() is True

    def test_activation_function_with_graph_context(self):
        """Test that activation_function can accept graph context."""
        def graph_aware_activation(graph: ExecutableGraph):
            return len(graph.places) > 0

        transition = FunctionTransitionNode(
            name="Test",
            function=lambda x: x + 1,
            activation_function=graph_aware_activation,
        )

        # Create a simple graph to test with
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [1]),
            ArgumentEdgeToTransition('Input', 'Test', 'x'),
            transition,
            ReturnedEdgeFromTransition('Test', 'Output'),
            ListPlaceNode('Output', int),
        ])

        assert transition.activation_function(graph) is True


class TestTransitionSelector:
    """Tests for the transition_selector field on ExecutableGraph."""

    def test_transition_selector_optional(self):
        """Test that transition_selector is optional and defaults to None."""
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [1]),
            FunctionTransitionNode('Test', lambda x: x + 1),
        ])
        assert graph.transition_selector is None

    def test_transition_selector_can_be_set(self):
        """Test that transition_selector can be set on graph."""
        def my_selector(graph, enabled):
            return enabled[0] if enabled else None

        graph_nodes = [
            ListPlaceNode('Input', int, [1]),
            FunctionTransitionNode('Test', lambda x: x + 1),
        ]
        graph = ExecutableGraphOperations.construct_graph(graph_nodes)
        graph.transition_selector = my_selector

        assert graph.transition_selector == my_selector


class TestExecuteGraphWithSelector:
    """Tests for execute_graph with custom transition selectors."""

    @pytest.mark.asyncio
    async def test_default_behavior_preserved(self):
        """Test that default behavior works when no selector provided."""
        def increment(x: int) -> int:
            return x + 1

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [1, 2, 3]),
            ArgumentEdgeToTransition('Input', 'Inc', 'x'),
            FunctionTransitionNode('Inc', increment),
            ReturnedEdgeFromTransition('Inc', 'Output'),
            ListPlaceNode('Output', int),
        ])

        # Execute with default selector
        updated_graph, fired = await ExecutableGraphOperations.execute_graph(
            graph,
            max_transitions=1,
        )

        assert fired == 1
        assert len(updated_graph.place_named('Input').tokens) == 2
        assert len(updated_graph.place_named('Output').tokens) == 1
        # Default behavior picks last token: 3 -> 4
        assert updated_graph.place_named('Output').tokens[0] == 4

    @pytest.mark.asyncio
    async def test_custom_selector_via_parameter(self):
        """Test providing custom selector as parameter."""
        def increment(x: int) -> int:
            return x + 1

        # Custom selector that always picks first enabled
        def first_enabled_selector(graph, enabled):
            return enabled[0] if enabled else None

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [1, 2, 3]),
            ArgumentEdgeToTransition('Input', 'Inc', 'x'),
            FunctionTransitionNode('Inc', increment),
            ReturnedEdgeFromTransition('Inc', 'Output'),
            ListPlaceNode('Output', int),
        ])

        # Execute with custom selector
        updated_graph, fired = await ExecutableGraphOperations.execute_graph(
            graph,
            max_transitions=1,
            transition_selector=first_enabled_selector,
        )

        assert fired == 1
        assert len(updated_graph.place_named('Output').tokens) == 1

    @pytest.mark.asyncio
    async def test_custom_selector_on_graph(self):
        """Test using selector set on graph itself."""
        def increment(x: int) -> int:
            return x + 1

        # Selector that returns None (no transition should fire)
        def no_selector(graph, enabled):
            return None

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [1, 2, 3]),
            ArgumentEdgeToTransition('Input', 'Inc', 'x'),
            FunctionTransitionNode('Inc', increment),
            ReturnedEdgeFromTransition('Inc', 'Output'),
            ListPlaceNode('Output', int),
        ])
        graph.transition_selector = no_selector

        # Execute - should fire 0 transitions
        updated_graph, fired = await ExecutableGraphOperations.execute_graph(
            graph,
            max_transitions=10,
        )

        assert fired == 0
        assert len(updated_graph.place_named('Input').tokens) == 3
        assert len(updated_graph.place_named('Output').tokens) == 0

    @pytest.mark.asyncio
    async def test_selector_parameter_overrides_graph(self):
        """Test that parameter selector overrides graph selector."""
        def increment(x: int) -> int:
            return x + 1

        def graph_selector(graph, enabled):
            return None  # Would fire nothing

        def param_selector(graph, enabled):
            return enabled[0] if enabled else None  # Fire first

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [1]),
            ArgumentEdgeToTransition('Input', 'Inc', 'x'),
            FunctionTransitionNode('Inc', increment),
            ReturnedEdgeFromTransition('Inc', 'Output'),
            ListPlaceNode('Output', int),
        ])
        graph.transition_selector = graph_selector

        # Parameter should override graph selector
        updated_graph, fired = await ExecutableGraphOperations.execute_graph(
            graph,
            max_transitions=1,
            transition_selector=param_selector,
        )

        assert fired == 1
        assert len(updated_graph.place_named('Output').tokens) == 1


class TestSelectorWithActivationFunction:
    """Tests combining selectors with activation functions."""

    @pytest.mark.asyncio
    async def test_selector_uses_activation_function(self):
        """Test selector that respects activation functions."""
        def increment(x: int) -> int:
            return x + 1

        # Guard that blocks firing
        def blocked_guard():
            return False

        # Selector that checks activation functions (guards)
        def guard_aware_selector(graph, enabled):
            for t in enabled:
                if t.activation_function is None:
                    return t
                # Try calling activation
                result = t.activation_function()
                if result:
                    return t
            return None

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [1]),
            ArgumentEdgeToTransition('Input', 'Inc', 'x'),
            FunctionTransitionNode(
                'Inc',
                increment,
                activation_function=blocked_guard,
            ),
            ReturnedEdgeFromTransition('Inc', 'Output'),
            ListPlaceNode('Output', int),
        ])

        # Should fire 0 transitions because guard blocks
        updated_graph, fired = await ExecutableGraphOperations.execute_graph(
            graph,
            max_transitions=1,
            transition_selector=guard_aware_selector,
        )

        assert fired == 0
        assert len(updated_graph.place_named('Input').tokens) == 1
        assert len(updated_graph.place_named('Output').tokens) == 0

    @pytest.mark.asyncio
    async def test_selector_with_priority_activation(self):
        """Test selector that uses activation as priority."""
        def increment(x: int) -> int:
            return x + 1

        def decrement(x: int) -> int:
            return x - 1

        # Priority-based selector
        def priority_selector(graph, enabled):
            if not enabled:
                return None

            def get_priority(t):
                if t.activation_function:
                    return t.activation_function()
                return 0.0

            return max(enabled, key=get_priority)

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [5]),

            # Low priority transition
            ArgumentEdgeToTransition('Input', 'Inc', 'x'),
            FunctionTransitionNode(
                'Inc',
                increment,
                activation_function=lambda: 1.0,  # Low priority
            ),
            ReturnedEdgeFromTransition('Inc', 'Output'),

            # High priority transition
            ArgumentEdgeToTransition('Input', 'Dec', 'x'),
            FunctionTransitionNode(
                'Dec',
                decrement,
                activation_function=lambda: 10.0,  # High priority
            ),
            ReturnedEdgeFromTransition('Dec', 'Output'),

            ListPlaceNode('Output', int),
        ])

        # Should fire Dec (higher priority)
        updated_graph, fired = await ExecutableGraphOperations.execute_graph(
            graph,
            max_transitions=1,
            transition_selector=priority_selector,
        )

        assert fired == 1
        # Decrement fired: 5 - 1 = 4
        assert updated_graph.place_named('Output').tokens[0] == 4

    @pytest.mark.asyncio
    async def test_activation_with_graph_context(self):
        """Test activation function that uses graph context."""
        def process(x: int) -> int:
            return x * 2

        # Activation checks pool size
        def pool_size_activation(graph):
            pool = graph.place_named('Pool')
            return len(pool.tokens) >= 2  # Only fire if 2+ items in pool

        # Guard-aware selector
        def guard_selector(graph, enabled):
            for t in enabled:
                if t.activation_function is None:
                    return t
                try:
                    result = t.activation_function(graph)
                except TypeError:
                    result = t.activation_function()
                if result:
                    return t
            return None

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Pool', int, [1]),  # Start with 1 item
            ArgumentEdgeToTransition('Pool', 'Process', 'x'),
            FunctionTransitionNode(
                'Process',
                process,
                activation_function=pool_size_activation,
            ),
            ReturnedEdgeFromTransition('Process', 'Output'),
            ListPlaceNode('Output', int),
        ])

        # Should not fire (only 1 item in pool, needs 2)
        updated_graph, fired = await ExecutableGraphOperations.execute_graph(
            graph,
            max_transitions=1,
            transition_selector=guard_selector,
        )

        assert fired == 0
        assert len(updated_graph.place_named('Pool').tokens) == 1

        # Add another item to pool
        pool_place = updated_graph.place_named('Pool')
        pool_place.tokens.append(2)

        # Now should fire (2 items in pool)
        updated_graph2, fired2 = await ExecutableGraphOperations.execute_graph(
            updated_graph,
            max_transitions=1,
            transition_selector=guard_selector,
        )

        assert fired2 == 1
        assert len(updated_graph2.place_named('Pool').tokens) == 1
        assert len(updated_graph2.place_named('Output').tokens) == 1
