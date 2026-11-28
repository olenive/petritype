"""Static structure checks (SUGGESTIONS.md item 3).

``assert_acyclic`` names token cycles at construction time; its companion
``assert_no_source_transitions`` flags transitions that consume nothing.
Together they are a static termination proof — separately each proves strictly
less, which these tests pin (an acyclic net with a source transition passes the
cycle check and fires forever anyway).
"""

import pytest

from petritype.core.events import Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    DrainEdge,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    SubnetPlaceNode,
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
        # A snapshot edge consumes nothing, so a transition fed only by one is
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


# --- through a subnet's ports (since 2026-11-28) -----------------------------------------------


def _pipe():
    """A subnet ``In → Pass → Out``, both of them ports."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode('In', int, port=True),
        ListPlaceNode('Out', int, port=True),
        FunctionTransitionNode('Pass', _passthrough()),
        ArgumentEdgeToTransition('In', 'Pass', 'x'),
        ReturnedEdgeFromTransition('Pass', 'Out'),
    ])


def _deeper_pipe():
    """A subnet whose way from ``In`` to ``Out`` goes through a pipe of its own:
    ``In → Down → Inner/In``, then ``Inner/Out → Up → Out``."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode('In', int, port=True),
        ListPlaceNode('Out', int, port=True),
        SubnetPlaceNode('Inner', _pipe()),
        FunctionTransitionNode('Down', _passthrough()),
        FunctionTransitionNode('Up', _passthrough()),
        ArgumentEdgeToTransition('In', 'Down', 'x'),
        ReturnedEdgeFromTransition('Down', Port('Inner', 'In')),
        ArgumentEdgeToTransition(Port('Inner', 'Out'), 'Up', 'x'),
        ReturnedEdgeFromTransition('Up', 'Out'),
    ])


def _back_through(subnet):
    """``Back`` takes from the subnet's ``Out`` and puts into its ``In``."""
    return [
        SubnetPlaceNode('S', subnet),
        FunctionTransitionNode('Back', _passthrough()),
        ArgumentEdgeToTransition(Port('S', 'Out'), 'Back', 'x'),
        ReturnedEdgeFromTransition('Back', Port('S', 'In')),
    ]


class TestThroughASubnetsPorts:
    """Until 2026-11-28 the check stopped at a subnet's ports, so a cycle that left
    the net at one port and came back at another passed."""

    @pytest.mark.parametrize('subnet', [_pipe, _deeper_pipe], ids=['one level', 'two levels'])
    def test_a_cycle_through_a_subnet_is_found_and_named(self, subnet):
        graph = ExecutableGraphOperations.construct_graph(_back_through(subnet()))
        with pytest.raises(ValueError) as found:
            ExecutableGraphCheck.assert_acyclic(graph)
        assert str(found.value) == (
            "Token flow contains a cycle: Port(place='S', port='Out') → Back → Port(place='S', port='In') → "
            "(inside subnet place 'S') → Port(place='S', port='Out')"
        )
        with pytest.raises(ValueError, match='cycle'):
            ExecutableGraphOperations.construct_graph(_back_through(subnet()), expect_acyclic=True)

    def test_a_cycle_through_two_subnets_is_found(self):
        graph = ExecutableGraphOperations.construct_graph([
            SubnetPlaceNode('A', _pipe()),
            SubnetPlaceNode('B', _deeper_pipe()),
            FunctionTransitionNode('Over', _passthrough()),
            FunctionTransitionNode('Back', _passthrough()),
            ArgumentEdgeToTransition(Port('A', 'Out'), 'Over', 'x'),
            ReturnedEdgeFromTransition('Over', Port('B', 'In')),
            ArgumentEdgeToTransition(Port('B', 'Out'), 'Back', 'x'),
            ReturnedEdgeFromTransition('Back', Port('A', 'In')),
        ])
        with pytest.raises(ValueError, match="inside subnet place 'A'.*inside subnet place 'B'|"
                                             "inside subnet place 'B'.*inside subnet place 'A'"):
            ExecutableGraphCheck.assert_acyclic(graph)

    def test_a_pipeline_through_a_subnet_passes(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Start', int, [1]),
            ListPlaceNode('End', int),
            SubnetPlaceNode('S', _deeper_pipe()),
            FunctionTransitionNode('Enter', _passthrough()),
            FunctionTransitionNode('Leave', _passthrough()),
            ArgumentEdgeToTransition('Start', 'Enter', 'x'),
            ReturnedEdgeFromTransition('Enter', Port('S', 'In')),
            ArgumentEdgeToTransition(Port('S', 'Out'), 'Leave', 'x'),
            ReturnedEdgeFromTransition('Leave', 'End'),
        ], expect_acyclic=True)
        assert ExecutableGraphCheck.port_paths(graph.subnet('S')) == {'In': ['Out'], 'Out': []}

    def test_a_way_back_inside_the_subnet_to_the_port_it_left_from_is_a_cycle(self):
        loop = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('In', int, port=True),
            ListPlaceNode('Middle', int),
            FunctionTransitionNode('Out', _passthrough()),
            FunctionTransitionNode('Back', _passthrough()),
            ArgumentEdgeToTransition('In', 'Out', 'x'),
            ReturnedEdgeFromTransition('Out', 'Middle'),
            ArgumentEdgeToTransition('Middle', 'Back', 'x'),
            ReturnedEdgeFromTransition('Back', 'In'),
        ])
        assert ExecutableGraphCheck.port_paths(loop) == {'In': ['In']}
        graph = ExecutableGraphOperations.construct_graph([SubnetPlaceNode('S', loop)])
        with pytest.raises(ValueError, match=r"Port\(place='S', port='In'\) → \(inside subnet place 'S'\) → "
                                             r"Port\(place='S', port='In'\)"):
            ExecutableGraphCheck.assert_acyclic(graph)

    def test_a_drain_or_a_snapshot_inside_the_subnet_leads_nowhere(self):
        """As in the net around: they move no token guaranteed."""
        for edge in (DrainEdge('In', 'Pass', 'xs'), SnapshotEdge('In', 'Pass', 'xs')):
            async def drain(xs: list[int]) -> int:
                return len(xs)

            subnet = ExecutableGraphOperations.construct_graph([
                ListPlaceNode('In', int, port=True),
                ListPlaceNode('Out', int, port=True),
                FunctionTransitionNode('Pass', drain),
                edge,
                ReturnedEdgeFromTransition('Pass', 'Out'),
            ])
            assert ExecutableGraphCheck.port_paths(subnet) == {'In': [], 'Out': []}
            ExecutableGraphCheck.assert_acyclic(ExecutableGraphOperations.construct_graph(_back_through(subnet)))

    def test_a_cycle_inside_a_subnet_through_none_of_its_ports_is_the_subnets_own(self):
        inner = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('In', int, port=True), *_loop_components(),
        ])
        ExecutableGraphCheck.assert_acyclic(
            ExecutableGraphOperations.construct_graph([SubnetPlaceNode('S', inner)])
        )  # does not raise
        with pytest.raises(ValueError, match='P1 → T1 → P2 → T2 → P1'):
            ExecutableGraphCheck.assert_acyclic(inner)
