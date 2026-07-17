"""Unit tests pinning every copy of the list-value disposition rule.

The engine interprets a list value produced by a transition via the
(value, destination-place-type) pair:

  - the place type is itself a list type -> the value IS the single token
    (an empty list is a real token there);
  - the place type is scalar -> a list value is a BATCH unpacked element-wise
    (an empty list is an empty batch and deposits nothing).

That destination-dependent rule was once implemented independently at four
sites, which is how they drifted (a validator/deposit divergence stalled the
returning_empty_list notebook at one step, July 2026). It now lives in one
place — ExecutableGraphCheck.value_disposition_for_place — from which the
other sites derive:

  1. CompareTypes.between_value_and_type — element-wise generic matching
     (place-agnostic foundation, unchanged)
  2. ExecutableGraphCheck.value_and_places_types_match — output routing
  3. ExecutableGraphCheck.ensure_token_type_matches_place_type — validation
  4. ExecutableGraphOperations.add_tokens_to_places — stage-3 deposit

One class per site pins its semantics under the rule above; the consistency
class asserts the sites agree pairwise. Divergences these tests originally
exposed (all fixed by the consolidation):

  - routing matched a batch of list-tokens to a list-typed place that the
    deposit then rejected;
  - a place typed bare ``list`` was treated as a batch destination by the
    validator and the deposit, though under the rule a list value there is a
    single token (``get_origin(list)`` is None, so the origin-based guards
    missed it).

On top of the per-site rules, stage 2 must arbitrate a [] result among
multiple output places. [] type-matches every place, but only a deposit at a
list-typed place is observable, so ambiguity is counted over the list-typed
matches — see TestStage2EmptyListArbitration for the three cases.

Out of scope on purpose: the input side. An empty place disables a
transition even when the argument is list-typed and could accept [] — that
is classic Petri-net enablement semantics, not a copy of this rule.
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
)
from petritype.core.type_comparisons import CompareTypes


def _deposit(value, place: ListPlaceNode) -> list:
    """Route a single value into a single place through the stage-3 deposit."""
    ExecutableGraphOperations.add_tokens_to_places(
        {place.name: value},
        {place.name: place},
        check_types=True,
    )
    return place.tokens


class TestCompareTypesOnListValues:
    """Site 1: the raw value-vs-type comparison.

    This layer is place-agnostic: it answers "is this value a T", nothing
    more. The batch leniency (an empty list acceptable at a scalar place)
    belongs to the layers above, so [] vs int must be False here.
    """

    def test_empty_list_matches_any_parameterized_list(self):
        assert CompareTypes.between_value_and_type([], list[int])
        assert CompareTypes.between_value_and_type([], list[str])

    def test_empty_list_matches_bare_list(self):
        assert CompareTypes.between_value_and_type([], list)

    def test_empty_list_does_not_match_scalar(self):
        assert not CompareTypes.between_value_and_type([], int)

    def test_populated_list_checked_element_wise(self):
        assert CompareTypes.between_value_and_type([1, 2], list[int])
        assert not CompareTypes.between_value_and_type(["a"], list[int])

    def test_list_of_lists_is_not_a_list_of_ints(self):
        assert not CompareTypes.between_value_and_type([[1], [2]], list[int])


class TestRoutingOnListValues:
    """Site 2: value_and_places_types_match decides candidate output places."""

    def test_empty_list_matches_scalar_place(self):
        place = ListPlaceNode('P', int)
        assert ExecutableGraphCheck.value_and_places_types_match([], (place,)) == (place,)

    def test_empty_list_matches_list_typed_place(self):
        place = ListPlaceNode('P', list[int])
        assert ExecutableGraphCheck.value_and_places_types_match([], (place,)) == (place,)

    def test_empty_list_matches_every_candidate(self):
        # Routing stays type-level by design: [] is depositable at every
        # place, so every place matches. What that means for a transition
        # with several output places is stage 2's call — it counts ambiguity
        # over the list-typed matches only, since a [] deposit anywhere else
        # is a no-op. Pinned in TestStage2EmptyListArbitration.
        places = (ListPlaceNode('A', int), ListPlaceNode('B', list[int]), ListPlaceNode('C', str))
        assert ExecutableGraphCheck.value_and_places_types_match([], places) == places

    def test_batch_of_scalars_matches_scalar_place(self):
        place = ListPlaceNode('P', int)
        assert ExecutableGraphCheck.value_and_places_types_match([100], (place,)) == (place,)

    def test_whole_value_matches_list_typed_place(self):
        place = ListPlaceNode('P', list[int])
        assert ExecutableGraphCheck.value_and_places_types_match([100], (place,)) == (place,)

    def test_whole_value_matches_bare_list_place(self):
        place = ListPlaceNode('P', list)
        assert ExecutableGraphCheck.value_and_places_types_match([1, 2], (place,)) == (place,)

    def test_ambiguous_value_matches_both_interpretations(self):
        # [100] is simultaneously a batch of ints and a single list[int]
        # token; routing reports both and stage 2 arbitrates via
        # allow_token_copying. Pins current behaviour.
        scalar = ListPlaceNode('Scalar', int)
        listy = ListPlaceNode('Listy', list[int])
        matched = ExecutableGraphCheck.value_and_places_types_match([100], (scalar, listy))
        assert set(p.name for p in matched) == {'Scalar', 'Listy'}

    def test_batch_of_list_tokens_does_not_match_list_typed_place(self):
        """A list-typed place takes the value as ONE token, never a batch.

        [[1, 2], [3]] is not a list[int], and the deposit refuses it with a
        TypeError — so routing claiming a match just moves the failure later
        and makes it inconsistent. Routing should report no match.
        """
        place = ListPlaceNode('P', list[int])
        assert ExecutableGraphCheck.value_and_places_types_match([[1, 2], [3]], (place,)) == ()


class TestValidatorOnListValues:
    """Site 3: ensure_token_type_matches_place_type, as used by the
    pre-execution sweep and the output-distribution path."""

    def test_scalar_token_in_scalar_place(self):
        ExecutableGraphCheck.ensure_token_type_matches_place_type(1, ListPlaceNode('P', int))
        with pytest.raises(TypeError):
            ExecutableGraphCheck.ensure_token_type_matches_place_type("a", ListPlaceNode('P', int))

    def test_empty_batch_valid_at_scalar_place(self):
        ExecutableGraphCheck.ensure_token_type_matches_place_type([], ListPlaceNode('P', int))

    def test_batch_checked_element_wise_at_scalar_place(self):
        ExecutableGraphCheck.ensure_token_type_matches_place_type([1, 2], ListPlaceNode('P', int))
        with pytest.raises(TypeError):
            ExecutableGraphCheck.ensure_token_type_matches_place_type(["a"], ListPlaceNode('P', int))

    def test_list_tokens_checked_whole_at_list_typed_place(self):
        place = ListPlaceNode('P', list[int])
        ExecutableGraphCheck.ensure_token_type_matches_place_type([], place)
        ExecutableGraphCheck.ensure_token_type_matches_place_type([1, 2], place)
        with pytest.raises(TypeError):
            ExecutableGraphCheck.ensure_token_type_matches_place_type(["a"], place)
        with pytest.raises(TypeError):
            ExecutableGraphCheck.ensure_token_type_matches_place_type([[1], [2]], place)

    def test_empty_list_token_valid_in_bare_list_place(self):
        ExecutableGraphCheck.ensure_token_type_matches_place_type([], ListPlaceNode('P', list))

    def test_whole_list_token_valid_in_bare_list_place(self):
        """Bare ``list`` is a list type, so [1, 2] is a valid single token.

        The guard keys on get_origin(place.type), which is None for bare
        list, so the value is torn open as a batch and its int items are
        compared against ``list``.
        """
        ExecutableGraphCheck.ensure_token_type_matches_place_type([1, 2], ListPlaceNode('P', list))


class TestDepositOnListValues:
    """Site 4: add_tokens_to_places, the stage-3 deposit."""

    def test_batch_extends_scalar_place(self):
        assert _deposit([1, 2], ListPlaceNode('P', int)) == [1, 2]

    def test_empty_batch_deposits_nothing_at_scalar_place(self):
        assert _deposit([], ListPlaceNode('P', int)) == []

    def test_mistyped_batch_into_scalar_place_raises(self):
        with pytest.raises(TypeError):
            _deposit(["a"], ListPlaceNode('P', int))

    def test_scalar_token_appends_to_scalar_place(self):
        assert _deposit(1, ListPlaceNode('P', int)) == [1]

    def test_none_deposits_nothing(self):
        assert _deposit(None, ListPlaceNode('P', int)) == []

    def test_empty_list_is_a_real_token_at_list_typed_place(self):
        assert _deposit([], ListPlaceNode('P', list[int])) == [[]]

    def test_list_value_appends_whole_at_list_typed_place(self):
        assert _deposit([1, 2], ListPlaceNode('P', list[int])) == [[1, 2]]

    def test_batch_of_list_tokens_into_list_typed_place_raises(self):
        # The value is taken as ONE token and [[1, 2], [3]] is not a
        # list[int]; batch deposit into list-typed places does not exist.
        with pytest.raises(TypeError):
            _deposit([[1, 2], [3]], ListPlaceNode('P', list[int]))

    def test_empty_list_is_a_real_token_at_bare_list_place(self):
        assert _deposit([], ListPlaceNode('P', list)) == [[]]

    def test_list_value_appends_whole_at_bare_list_place(self):
        """Bare ``list`` is a list type, so [1, 2] is the token itself.

        The append-vs-extend guard special-cases bare list only for the
        empty value; a populated one falls into the extend branch and its
        int items are rejected against ``list``.
        """
        assert _deposit([1, 2], ListPlaceNode('P', list)) == [[1, 2]]


# (value, place type) pairs covering both interpretations at every place kind.
_CONSISTENCY_CASES = [
    pytest.param([], int, id="empty-into-int"),
    pytest.param([], list[int], id="empty-into-list[int]"),
    pytest.param([], list, id="empty-into-bare-list"),
    pytest.param([100], int, id="batch-into-int"),
    pytest.param([100], list[int], id="whole-into-list[int]"),
    pytest.param([1, 2], list, id="whole-into-bare-list"),
    pytest.param([[1, 2], [3]], list[int], id="list-batch-into-list[int]"),
    pytest.param(["a"], int, id="mistyped-into-int"),
]


class TestCrossSiteConsistency:
    """The sites must agree: a value routed somewhere must be depositable
    there, and the validator's verdict must match the deposit's."""

    @pytest.mark.parametrize("value,place_type", _CONSISTENCY_CASES)
    def test_routing_match_implies_deposit_accepts(self, value, place_type):
        place = ListPlaceNode('P', place_type)
        matched = ExecutableGraphCheck.value_and_places_types_match(value, (place,))
        if not matched:
            return  # nothing routed, nothing to deposit — consistent
        _deposit(value, place)  # must not raise

    @pytest.mark.parametrize("value,place_type", _CONSISTENCY_CASES)
    def test_validator_verdict_agrees_with_deposit(self, value, place_type):
        def outcome(action) -> bool:
            try:
                action()
            except TypeError:
                return False
            return True

        validator_accepts = outcome(
            lambda: ExecutableGraphCheck.ensure_token_type_matches_place_type(
                value, ListPlaceNode('P', place_type)
            )
        )
        deposit_accepts = outcome(lambda: _deposit(value, ListPlaceNode('P', place_type)))
        assert validator_accepts == deposit_accepts


def emit_empty_int_list(x: int) -> list[int]:
    return []


def emit_empty_int_or_str_list(x: int) -> list[int] | list[str]:
    return []


# Un-annotated on purpose: no honest annotation covers "an empty batch for
# whichever scalar place" — `-> list[int]` fails the construction check
# against the str-typed output place. The scenario needs runtime routing.
def emit_empty(x):
    return []


def _graph_returning_empty(function, outputs, **construct_kwargs):
    """A minimal net: one int trigger token feeds `function`, whose []
    result stage 2 must arbitrate among `outputs` ((name, type) pairs)."""
    components = [
        ListPlaceNode('Trigger', int, [1]),
        ArgumentEdgeToTransition('Trigger', 'Emit', 'x'),
        FunctionTransitionNode('Emit', function),
    ]
    for name, place_type in outputs:
        components.append(ReturnedEdgeFromTransition('Emit', name))
        components.append(ListPlaceNode(name, place_type))
    return ExecutableGraphOperations.construct_graph(components, **construct_kwargs)


class TestStage2EmptyListArbitration:
    """Stage-2 arbitration of a [] result among multiple output places.

    [] type-matches every output place, but a deposit at a scalar-typed
    place is a no-op (disposition "nothing"), so genuine ambiguity is
    counted over the places where the deposit is observable — the
    list-typed ones:

      - no list-typed match      -> nothing to deposit anywhere: the firing
                                    succeeds and deposits nothing;
      - one list-typed match     -> fully determined: the [] token lands
                                    there, no allow_token_copying needed;
      - several list-typed matches -> genuinely ambiguous: the
                                    multiple-match error, arbitrated by
                                    allow_token_copying as usual.

    Stage 2 wraps its errors, so failures surface as TransitionFailedError
    with the routing ValueError as __cause__.
    """

    @pytest.mark.asyncio
    async def test_single_scalar_output_deposits_nothing(self):
        graph = _graph_returning_empty(emit_empty_int_list, [('Out', int)])
        graph, fired = await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert fired == 1
        assert graph.place_named('Out').tokens == []

    @pytest.mark.asyncio
    async def test_all_scalar_outputs_deposit_nothing(self):
        graph = _graph_returning_empty(emit_empty, [('Ints', int), ('Strs', str)])
        graph, fired = await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert fired == 1
        assert graph.place_named('Ints').tokens == []
        assert graph.place_named('Strs').tokens == []

    @pytest.mark.asyncio
    async def test_single_list_typed_output_receives_the_token(self):
        graph = _graph_returning_empty(
            emit_empty_int_list, [('Scalars', int), ('Lists', list[int])]
        )
        graph, fired = await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert fired == 1
        assert graph.place_named('Scalars').tokens == []
        assert graph.place_named('Lists').tokens == [[]]

    @pytest.mark.asyncio
    async def test_multiple_list_typed_outputs_raise_without_copying(self):
        graph = _graph_returning_empty(
            emit_empty_int_or_str_list, [('IntLists', list[int]), ('StrLists', list[str])]
        )
        with pytest.raises(TransitionFailedError) as excinfo:
            await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert isinstance(excinfo.value.__cause__, ValueError)
        assert "multiple matching" in str(excinfo.value.__cause__)

    @pytest.mark.asyncio
    async def test_scalar_matches_do_not_rescue_genuine_ambiguity(self):
        # A scalar place among the candidates neither adds ambiguity nor
        # removes it — two list-typed matches still conflict.
        graph = _graph_returning_empty(
            emit_empty_int_or_str_list,
            [('Ints', int), ('IntLists', list[int]), ('StrLists', list[str])],
        )
        with pytest.raises(TransitionFailedError) as excinfo:
            await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert isinstance(excinfo.value.__cause__, ValueError)

    @pytest.mark.asyncio
    async def test_multiple_list_typed_outputs_copy_with_flag(self):
        graph = _graph_returning_empty(
            emit_empty_int_or_str_list,
            [('IntLists', list[int]), ('StrLists', list[str])],
            allow_token_copying=True,
        )
        graph, fired = await ExecutableGraphOperations.execute_graph(graph, max_transitions=1)
        assert fired == 1
        assert graph.place_named('IntLists').tokens == [[]]
        assert graph.place_named('StrLists').tokens == [[]]
