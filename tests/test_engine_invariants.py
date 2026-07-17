"""Regression tests for engine bugs found in the July 2026 design review.

Each test encodes intended behavior that the engine at the time violated:

1. ensure_all_token_types_match_place_types iterated the pydantic model
   itself (yielding (field_name, value) tuples), so it validated nothing.
2. add_tokens_to_places discarded the CompareTypes booleans, so its
   check_types=True contract was a no-op. (At the execute_graph level this
   was masked: type-based routing rejects whole-value mistypes and the
   output-distribution path validates at the routing step, so the tests
   target the function directly.)
3. The return-edge construction check read the place type via
   get_type_hints(place).get('type') (always None) instead of place.type,
   so mismatched return edges constructed cleanly.
4. Tokens consumed in stage 1 were silently lost when the transition body
   raised in stage 2; now the failure raises TransitionFailedError carrying
   the consumed tokens. They are deliberately NOT put back by default (the
   body may have mutated them before failing); restore_tokens_on_failure=True
   opts into best-effort restoration.
5. execute_graph(max_transitions=None) crashed on `>=` against None instead
   of running until no transition is enabled.
6. return_index was assigned by the construction helpers but ignored by the
   fire path; indexed return edges now split a tuple result by position.
7. ensure_token_type_matches_place_type treated any list token in a
   ListPlaceNode as a batch of tokens to check element-wise, so a place whose
   element type is itself a list (e.g. list[int]) rejected its own valid
   tokens. add_tokens_to_places already guarded this with get_origin; the
   validation path did not.
"""

import pytest
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    TransitionFailedError,
    function_transition_node_and_output_edges,
)


def identity(x: int) -> int:
    return x


def increment(x: int) -> int:
    return x + 1


def stringify(x: int) -> str:
    return str(x)


def explode(x: int) -> int:
    raise RuntimeError("boom")


def emit_int_and_str() -> tuple[int, str]:
    return (1, "a")


def generate_list_of_integers(n: int) -> list[int]:
    return [100 + x for x in range(n)]


# Un-annotated on purpose, mirroring the pass-through in
# examples/special_cases/returning_empty_list.py that these tests reproduce.
def pass_through_list(lst):
    return lst


class TestTokenSweepValidation:
    """Issue 1: the pre-execution token/place type sweep validates nothing."""

    def _graph_with_mistyped_token(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [1]),
            ArgumentEdgeToTransition('Input', 'Identity', 'x'),
            FunctionTransitionNode('Identity', identity),
            ReturnedEdgeFromTransition('Identity', 'Output'),
            ListPlaceNode('Output', int),
        ])
        # Simulate user code mutating a place after construction.
        graph.place_named('Input').tokens.append("not an int")
        return graph

    def test_check_raises_on_mistyped_token(self):
        graph = self._graph_with_mistyped_token()
        with pytest.raises(TypeError):
            ExecutableGraphCheck.ensure_all_token_types_match_place_types(graph)

    @pytest.mark.asyncio
    async def test_execute_graph_rejects_mistyped_token_up_front(self):
        graph = self._graph_with_mistyped_token()
        with pytest.raises(TypeError):
            await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)


class TestOutputTypeEnforcement:
    """Issue 2: add_tokens_to_places' check_types=True contract is a no-op.

    Both call sites invoke CompareTypes.between_value_and_type and discard
    the returned boolean, so with check_types=True (the default) a mistyped
    token is appended anyway. Through execute_graph this is currently masked
    by type-based routing, so these tests exercise the function directly —
    it is the designated stage-3 defence and callers (present or future) are
    entitled to believe its signature.
    """

    def test_mistyped_scalar_token_raises(self):
        place = ListPlaceNode('Output', int)
        with pytest.raises(TypeError):
            ExecutableGraphOperations.add_tokens_to_places(
                {'Output': "not an int"},
                {'Output': place},
                check_types=True,
            )

    def test_mistyped_list_of_tokens_raises(self):
        place = ListPlaceNode('Output', int)
        with pytest.raises(TypeError):
            ExecutableGraphOperations.add_tokens_to_places(
                {'Output': ["a", "b"]},  # extended element-wise into an int place
                {'Output': place},
                check_types=True,
            )


class TestReturnEdgeConstructionCheck:
    """Issue 3: a return edge with a mismatched type must fail construction."""

    def test_mismatched_return_edge_rejected_at_construction(self):
        with pytest.raises(TypeError):
            ExecutableGraphOperations.construct_graph([
                ListPlaceNode('Input', int, [1]),
                ArgumentEdgeToTransition('Input', 'Stringify', 'x'),
                FunctionTransitionNode('Stringify', stringify),
                ReturnedEdgeFromTransition('Stringify', 'Output'),
                ListPlaceNode('Output', int),  # int place fed a str return
            ])

    def test_control_argument_edge_mismatch_already_rejected(self):
        # Control (passes today): the argument-edge check reads place.type
        # correctly, so the same mismatch on the input side does raise. The
        # return-edge check above should behave identically.
        with pytest.raises(TypeError):
            ExecutableGraphOperations.construct_graph([
                ListPlaceNode('Input', str, ["a"]),
                ArgumentEdgeToTransition('Input', 'Identity', 'x'),
                FunctionTransitionNode('Identity', identity),  # x: int
                ReturnedEdgeFromTransition('Identity', 'Output'),
                ListPlaceNode('Output', int),
            ])


class TestFailedFiringSemantics:
    """Issue 4: a failed firing must not silently destroy consumed tokens.

    Default: the tokens are NOT restored — the body may have mutated them
    before failing, and a place silently holding corrupted tokens is worse
    than a visible loss. Instead the raised TransitionFailedError carries
    them. restore_tokens_on_failure=True opts into best-effort restoration.
    """

    def _exploding_graph(self, **construct_kwargs):
        return ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [42]),
            ArgumentEdgeToTransition('Input', 'Explode', 'x'),
            FunctionTransitionNode('Explode', explode),
            ReturnedEdgeFromTransition('Explode', 'Output'),
            ListPlaceNode('Output', int),
        ], **construct_kwargs)

    @pytest.mark.asyncio
    async def test_failure_raises_error_carrying_consumed_tokens(self):
        graph = self._exploding_graph()
        with pytest.raises(TransitionFailedError) as excinfo:
            await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert excinfo.value.transition_name == 'Explode'
        assert excinfo.value.consumed == {'x': 42}
        assert excinfo.value.restored is False
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        # The loss is visible in the marking, not papered over.
        assert graph.place_named('Input').tokens == []

    @pytest.mark.asyncio
    async def test_restore_flag_on_graph_puts_tokens_back(self):
        graph = self._exploding_graph(restore_tokens_on_failure=True)
        with pytest.raises(TransitionFailedError) as excinfo:
            await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert excinfo.value.restored is True
        assert graph.place_named('Input').tokens == [42]

    @pytest.mark.asyncio
    async def test_restore_flag_as_parameter_overrides_graph(self):
        graph = self._exploding_graph()  # graph default: no restore
        with pytest.raises(TransitionFailedError):
            await ExecutableGraphOperations.execute_graph(
                graph,
                max_transitions=1,
                restore_tokens_on_failure=True,
            )
        assert graph.place_named('Input').tokens == [42]


class TestReturnIndexRouting:
    """Issue 6: return_index is constructed and validated but never executed.

    function_transition_node_and_output_edges(use_return_indices=True)
    builds indexed return edges, and ExecutableGraphCheck has dedicated
    return-index predicates (which nothing calls), but the fire path routes
    the whole result by type matching — a tuple result matches no scalar
    place and firing raises ValueError instead of splitting the tuple by
    index. Note for the fix: the return-edge construction check must become
    index-aware too, or fixing issue 3 will make these nets fail to build.
    """

    @pytest.mark.asyncio
    async def test_tuple_result_is_split_across_indexed_edges(self):
        graph = ExecutableGraphOperations.construct_graph([
            *function_transition_node_and_output_edges(
                name='Emit',
                function=emit_int_and_str,
                output_place_names=['Ints', 'Strs'],
                use_return_indices=True,
            ),
            ListPlaceNode('Ints', int),
            ListPlaceNode('Strs', str),
        ])
        updated_graph, fired = await ExecutableGraphOperations.execute_graph(
            graph,
            max_transitions=1,
        )
        assert fired == 1
        assert updated_graph.place_named('Ints').tokens == [1]
        assert updated_graph.place_named('Strs').tokens == ["a"]


class TestUnboundedExecution:
    """Issue 5: max_transitions=None means run until nothing is enabled."""

    @pytest.mark.asyncio
    async def test_max_transitions_none_runs_to_completion(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input', int, [1, 2, 3]),
            ArgumentEdgeToTransition('Input', 'Inc', 'x'),
            FunctionTransitionNode('Inc', increment),
            ReturnedEdgeFromTransition('Inc', 'Output'),
            ListPlaceNode('Output', int),
        ])
        updated_graph, fired = await ExecutableGraphOperations.execute_graph(
            graph,
            max_transitions=None,
        )
        assert fired == 3
        assert updated_graph.place_named('Input').tokens == []
        assert updated_graph.place_named('Output').tokens == [2, 3, 4]


class TestListTypedPlaceValidation:
    """Issue 7: a place whose element type is a list rejects its own tokens.

    ensure_token_type_matches_place_type sees `isinstance(token, list)` on a
    ListPlaceNode and assumes the list is a batch to validate element-wise. For
    a place declared list[int], the valid token [100] is torn open and 100 is
    compared against list[int], which fails. add_tokens_to_places gets this
    right (it checks `get_origin(place.type) is not list` before extending), so
    the token is written successfully and the *next* execute_graph call dies on
    its up-front sweep — the net appears to stop after one step.

    Mirrors examples/special_cases/returning_empty_list.py.
    """

    def _list_of_lists_graph(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Input Number', int, [1, 2, 0, 3]),
            ArgumentEdgeToTransition('Input Number', 'Generate', 'n'),
            FunctionTransitionNode('Generate', generate_list_of_integers),
            ReturnedEdgeFromTransition('Generate', 'Integer Lists'),
            ListPlaceNode('Integer Lists', list[int]),
            ArgumentEdgeToTransition('Integer Lists', 'Pass Through', 'lst'),
            FunctionTransitionNode('Pass Through', pass_through_list),
            ReturnedEdgeFromTransition('Pass Through', 'Final Output'),
            ListPlaceNode('Final Output', int),
        ])
        return graph

    def test_list_token_in_list_typed_place_is_valid(self):
        place = ListPlaceNode('Integer Lists', list[int], [[100], [100, 101], []])
        ExecutableGraphCheck.ensure_token_type_matches_place_type([100], place)

    def test_sweep_accepts_list_typed_place_holding_its_own_tokens(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode('Integer Lists', list[int], [[100], [100, 101], []]),
            ArgumentEdgeToTransition('Integer Lists', 'Pass Through', 'lst'),
            FunctionTransitionNode('Pass Through', pass_through_list),
            ReturnedEdgeFromTransition('Pass Through', 'Final Output'),
            ListPlaceNode('Final Output', int),
        ])
        ExecutableGraphCheck.ensure_all_token_types_match_place_types(graph)

    def test_genuinely_mistyped_token_in_list_typed_place_still_raises(self):
        """The guard must not degrade into accepting anything list-shaped."""
        place = ListPlaceNode('Integer Lists', list[int])
        with pytest.raises(TypeError):
            ExecutableGraphCheck.ensure_token_type_matches_place_type(["a", "b"], place)

    @pytest.mark.asyncio
    async def test_stepping_one_transition_at_a_time_reaches_completion(self):
        """The bug's visible symptom, as the notebooks hit it.

        The up-front sweep runs once per execute_graph call, so a single
        max_transitions=None call never revalidates the tokens it writes and
        sails through. Stepping — repeated max_transitions=1 calls, which is
        what the notebooks' fire_one does — re-sweeps every call and dies on
        the token the previous step legitimately wrote.
        """
        graph = self._list_of_lists_graph()
        fired_total = 0
        for _ in range(20):
            _updated, fired = await ExecutableGraphOperations.execute_graph(
                graph,
                max_transitions=1,
            )
            if not fired:
                break
            fired_total += fired
        assert fired_total == 8

    @pytest.mark.asyncio
    async def test_net_runs_to_completion_and_empty_list_yields_no_tokens(self):
        graph = self._list_of_lists_graph()
        updated_graph, fired = await ExecutableGraphOperations.execute_graph(
            graph,
            max_transitions=None,
        )
        # 4 Generate firings + 4 Pass Through firings.
        assert fired == 8
        assert updated_graph.place_named('Input Number').tokens == []
        assert updated_graph.place_named('Integer Lists').tokens == []
        # Input 0 produced [], which contributes nothing to Final Output; the
        # other three inputs contribute 1 + 2 + 3 = 6 tokens.
        assert updated_graph.place_named('Final Output').tokens == [
            100, 100, 101, 100, 101, 102,
        ]
