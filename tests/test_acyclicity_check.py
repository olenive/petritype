"""Static structure checks (SUGGESTIONS.md item 3).

``assert_acyclic`` names token cycles at construction time; its companion
``assert_no_source_transitions`` flags transitions that consume nothing.
Together they are a static termination proof — separately each proves strictly
less, which these tests pin (an acyclic net with a source transition passes the
cycle check and fires forever anyway).
"""

import pytest

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
)


def _passthrough():
    async def body(x: int) -> int:
        return x

    return body


def _chain_components(n_transitions: int):
    """P0 -T1-> P1 -T2-> ... -Tn-> Pn."""
    components = [ListPlaceNode('P0', int, [1])]
    for i in range(1, n_transitions + 1):
        components += [
            ArgumentEdgeToTransition(f'P{i - 1}', f'T{i}', 'x'),
            FunctionTransitionNode(f'T{i}', _passthrough()),
            ReturnedEdgeFromTransition(f'T{i}', f'P{i}'),
            ListPlaceNode(f'P{i}', int),
        ]
    return components


def _loop_components():
    """P1 -T1-> P2 -T2-> P1: a two-transition token loop."""
    return [
        ListPlaceNode('P1', int, [1]),
        ListPlaceNode('P2', int),
        FunctionTransitionNode('T1', _passthrough()),
        FunctionTransitionNode('T2', _passthrough()),
        ArgumentEdgeToTransition('P1', 'T1', 'x'),
        ReturnedEdgeFromTransition('T1', 'P2'),
        ArgumentEdgeToTransition('P2', 'T2', 'x'),
        ReturnedEdgeFromTransition('T2', 'P1'),
    ]


class TestAssertAcyclic:
    def test_linear_chain_passes(self):
        graph = ExecutableGraphOperations.construct_graph(_chain_components(3))
        ExecutableGraphCheck.assert_acyclic(graph)  # does not raise

    def test_token_loop_raises_with_the_cycle_path_named(self):
        graph = ExecutableGraphOperations.construct_graph(_loop_components())
        with pytest.raises(ValueError, match='P1 → T1 → P2 → T2 → P1'):
            ExecutableGraphCheck.assert_acyclic(graph)

    def test_self_loop_raises(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('P1', int, [1]),
            FunctionTransitionNode('T1', _passthrough()),
            ArgumentEdgeToTransition('P1', 'T1', 'x'),
            ReturnedEdgeFromTransition('T1', 'P1'),
        ])
        with pytest.raises(ValueError, match='P1 → T1 → P1'):
            ExecutableGraphCheck.assert_acyclic(graph)

    def test_diamond_is_not_a_false_positive(self):
        # Two paths converging on the same place revisit a finished node —
        # that is reconvergence, not a cycle.
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('P0', int, [1, 2]),
            ListPlaceNode('P1', int),
            FunctionTransitionNode('T1', _passthrough()),
            FunctionTransitionNode('T2', _passthrough()),
            ArgumentEdgeToTransition('P0', 'T1', 'x'),
            ArgumentEdgeToTransition('P0', 'T2', 'x'),
            ReturnedEdgeFromTransition('T1', 'P1'),
            ReturnedEdgeFromTransition('T2', 'P1'),
        ])
        ExecutableGraphCheck.assert_acyclic(graph)  # does not raise

    def test_snapshot_back_reference_passes(self):
        # T1's read of the downstream place moves no tokens, so the "cycle"
        # through it cannot feed itself.
        async def body(x: int, seen: int) -> int:
            return x

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('P0', int, [1]),
            ListPlaceNode('P1', int),
            FunctionTransitionNode('T1', body),
            ArgumentEdgeToTransition('P0', 'T1', 'x'),
            ReturnedEdgeFromTransition('T1', 'P1'),
            SnapshotEdge('P1', 'T1', 'seen'),
        ])
        ExecutableGraphCheck.assert_acyclic(graph)  # does not raise

    def test_mutate_back_reference_passes(self):
        # Conservatively excluded like snapshots: a mutate edge modifies tokens
        # in place but moves none (stated in assert_acyclic's docstring).
        async def body(x: int, live: int) -> int:
            return x

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('P0', int, [1]),
            ListPlaceNode('P1', int),
            FunctionTransitionNode('T1', body),
            ArgumentEdgeToTransition('P0', 'T1', 'x'),
            ReturnedEdgeFromTransition('T1', 'P1'),
            MutateEdge('P1', 'T1', 'live'),
        ])
        ExecutableGraphCheck.assert_acyclic(graph)  # does not raise

    def test_source_transition_net_passes(self):
        # Acyclicity is not termination: a source transition consumes nothing
        # and fires forever in this perfectly acyclic net —
        # assert_no_source_transitions is the companion check that flags it.
        graph = ExecutableGraphOperations.construct_graph(_source_components())
        ExecutableGraphCheck.assert_acyclic(graph)  # does not raise


def _source_components():
    """A source transition (no argument edges) feeding a place — acyclic, never quiesces."""

    async def produce() -> int:
        return 1

    return [
        ListPlaceNode('P1', int),
        FunctionTransitionNode('Produce', produce),
        ReturnedEdgeFromTransition('Produce', 'P1'),
    ]


class TestAssertNoSourceTransitions:
    def test_chain_passes(self):
        graph = ExecutableGraphOperations.construct_graph(_chain_components(3))
        ExecutableGraphCheck.assert_no_source_transitions(graph)  # does not raise

    def test_source_transition_is_flagged_by_name(self):
        graph = ExecutableGraphOperations.construct_graph(_source_components())
        with pytest.raises(ValueError, match='Produce'):
            ExecutableGraphCheck.assert_no_source_transitions(graph)

    def test_snapshot_fed_transition_counts_as_source(self):
        # A read edge consumes nothing, so a transition fed only by one is
        # still a source.
        async def watch(seen: int) -> int:
            return seen

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('P1', int, [1]),
            ListPlaceNode('P2', int),
            FunctionTransitionNode('Watch', watch),
            SnapshotEdge('P1', 'Watch', 'seen'),
            ReturnedEdgeFromTransition('Watch', 'P2'),
        ])
        with pytest.raises(ValueError, match='Watch'):
            ExecutableGraphCheck.assert_no_source_transitions(graph)


class TestConstructGraphExpectAcyclic:
    def test_cyclic_net_fails_at_construction(self):
        with pytest.raises(ValueError, match='cycle'):
            ExecutableGraphOperations.construct_graph(_loop_components(), expect_acyclic=True)

    def test_acyclic_net_constructs(self):
        graph = ExecutableGraphOperations.construct_graph(_chain_components(3), expect_acyclic=True)
        assert graph.place_named('P0').tokens == [1]
