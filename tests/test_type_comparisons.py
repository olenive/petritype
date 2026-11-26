import collections
import collections.abc as cabc
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import (
    Annotated,
    Any,
    Callable,
    Collection,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    Literal,
    Mapping,
    MutableMapping,
    MutableSequence,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from pydantic import BaseModel
import pytest

from petritype.core.type_comparisons import CompareTypes


class TestCompareTypes:

    @pytest.mark.parametrize(
        "value, type_to_compare_against, should_match",
        [
            # Basic types
            (None, int, False),
            (42, int, True),
            ("a string", str, True),
            ({"key": "value"}, dict, True),

            # Dict with type parameters
            ({"key": 42}, dict[str, int], True),
            ({"key": 42}, str, False),
            ({"key": 42}, list, False),
            ({"key": 42}, dict[str, str], False),
            ({"key": 42}, Dict[str, str], False),

            # Lists
            ([1, 2, 3], list, True),
            ([1, 2, 3], list[int], True),
            (["a", "b", "c"], list[str], True),
            ([1, "a", 3.0], list[Union[int, str, float]], True),

            # Optional types
            (None, Optional[int], True),
            (42, Optional[int], True),

            # Union types
            (42, Union[int, str], True),
            ("a string", Union[int, str], True),
        ]
    )
    def test_between_value_and_type_multiple_cases(self, value, type_to_compare_against, should_match):
        result = CompareTypes.between_value_and_type(value, type_to_compare_against)
        assert result == should_match

    def test_between_value_and_type_between_int_and_float(self):
        """We don't want to treat int as a subset of float here."""
        value = 100
        type_to_compare_against = float
        assert not CompareTypes.between_value_and_type(value, type_to_compare_against)

    def test_between_value_and_type_given_custom_base_model(self):
        class MyModel(BaseModel):
            a: int
            b: str

        value = MyModel(a=1, b="test")
        expected = MyModel
        assert CompareTypes.between_value_and_type(value, expected)

    # def test_between_value_and_type_given_list_place_node(self):
    #     value = ListPlaceNode()
    #     expected = ListPlaceNode
    #     assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_dict_with_custom_base_model(self):
        class MyModel(BaseModel):
            a: int
            b: str

        value = {"key": MyModel(a=1, b="test")}
        expected = Dict[str, MyModel]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_with_tuple_key_to_dict_0_false(self):
        value = {"key": "value"}
        expected = dict[tuple[str], str]
        assert not CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_with_tuple_key_to_dict_0_true(self):
        value = {("key",): "value"}
        expected = dict[tuple[str], str]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_with_tuple_key_to_dict_0_false_1(self):
        value = {(123,): "value"}
        expected = dict[str, str]
        assert not CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_union_str_float_given_str(self):
        value = "a string"
        expected = Union[str, float]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_union_str_float_given_float(self):
        value = 3.14
        expected = Union[str, float]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_union_str_float_given_int(self):
        """check_type accepts ints as floats in this case but we don't want that here.

        This was the reason to switch to our own implementation of type checking.
        """
        value = 42
        expected = Union[str, float]
        assert not CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_with_type_alias_int(self):
        type MyAlias = int
        assert CompareTypes.between_value_and_type(123, MyAlias)
        assert not CompareTypes.between_value_and_type("abc", MyAlias)

    def test_between_value_and_type_with_type_alias_tuple_str_int(self):
        type MyAlias = tuple[str, int]
        assert CompareTypes.between_value_and_type(("abc", 123), MyAlias)
        assert not CompareTypes.between_value_and_type("efg", MyAlias)

    def test_between_value_and_type_with_list_of_floats(self):
        assert CompareTypes.between_value_and_type([0.1, 0.2, 0.3], list[float])

    def test_between_value_and_type_with_list_of_lists_of_floats(self):
        assert CompareTypes.between_value_and_type([[0.1], [0.2, 0.3]], list[list[float]])

    @pytest.mark.parametrize(
        "annotation1, annotation2, expected_result",
        [
            (int, int, True),
            (str, str, True),
            (int, str, False),
            (int, Any, True),
            (Any, str, True),
            (list[int], list[int], True),
            (list[int], list[str], False),
            (Dict[str, int], Dict[str, int], True),
            (Dict[str, int], Dict[str, str], False),
            (Optional[int], Optional[int], True),
            (Optional[int], Optional[str], False),
            (int, Optional[int], False),
            (Optional[int], str, False),
        ]
    )
    def test_between_annotations_with_simple_types(self, annotation1, annotation2, expected_result):
        result = CompareTypes.between_annotations(annotation1, annotation2)
        assert result == expected_result

    @pytest.mark.parametrize(
        "annotation_not_in_list, annotation_maybe_in_list, expected_result",
        [
            (int, list[int], True),
            (str, list[str], True),
            (int, list[str], False),
            (str, list[int], False),
            (int, int, True),
            (str, str, True),
            (int, list[Optional[int]], False),
            (Optional[int], list[Optional[int]], True),
            (Optional[str], list[Optional[str]], True),
            (int, list[Optional[str]], False),
            (Optional[int], list[Optional[str]], False),
        ]
    )
    def test_between_annotations_where_one_maybe_in_list(
        self, annotation_not_in_list, annotation_maybe_in_list, expected_result
    ):
        result = CompareTypes.between_annotations_where_one_maybe_in_list(
            annotation_not_in_list=annotation_not_in_list,
            annotation_maybe_in_list=annotation_maybe_in_list,
        )
        assert result == expected_result

    @pytest.mark.parametrize(
        "annotation1, annotation2, expected_result",
        [
            (Optional[int], Optional[int], True),
            (Optional[str], Optional[str], True),
            (Optional[int], list[str], False),
            (Optional[int], int, False),
            (int, Optional[int], False),
            (list[int], tuple[int], False),
            (Optional[int], list[int], False),
            (Optional[int], list[Optional[int]], True),
            (Optional[int], list[Optional[str]], False),
        ]
    )
    def test_between_annotations_where_both_maybe_in_list(self, annotation1, annotation2, expected_result):
        result = CompareTypes.between_annotations_where_both_maybe_in_list(annotation1, annotation2)
        assert result == expected_result


class TestBoolVsIntStrictness:

    @pytest.mark.parametrize(
        "value, expected_type, should_match",
        [
            # bool is NOT int
            (True, int, False),
            (False, int, False),
            (True, float, False),
            (False, float, False),
            # bool matches bool, Any, object, and unions containing bool
            (True, bool, True),
            (False, bool, True),
            (True, Any, True),
            (False, Any, True),
            (True, object, True),
            (False, object, True),
            (True, int | bool, True),
            (False, int | bool, True),
            (True, Union[int, bool], True),
            (True, Optional[bool], True),
            (True, int | str, False),
            (False, int | None, False),
            # int is NOT bool
            (0, bool, False),
            (1, bool, False),
            (0, int, True),
            (1, int, True),
            # bool rejected inside containers of int, accepted inside containers of bool
            ([True, False], list[int], False),
            ([1, True], list[int], False),
            ([True, False], list[bool], True),
            ((True,), tuple[int, ...], False),
            ((1, False), tuple[int, int], False),
            ((True, False), tuple[bool, ...], True),
            ({True}, set[int], False),
            ({True}, set[bool], True),
            (frozenset({False}), frozenset[int], False),
            ({"a": True}, dict[str, int], False),
            ({True: "a"}, dict[int, str], False),
            ({"a": True}, dict[str, bool], True),
            ([True], Sequence[int], False),
            ({"a": False}, Mapping[str, int], False),
        ],
    )
    def test_bool_vs_int(self, value, expected_type, should_match):
        assert CompareTypes.between_value_and_type(value, expected_type) == should_match

    def test_non_bool_int_subclass_matches_int(self):
        class MyInt(int):
            pass

        assert CompareTypes.between_value_and_type(MyInt(7), int)

    def test_type_alias_to_int_rejects_bool(self):
        type Count = int
        assert CompareTypes.between_value_and_type(1, Count)
        assert not CompareTypes.between_value_and_type(True, Count)
        assert not CompareTypes.between_value_and_type(False, Count)


class TestNoneAndNoneType:

    @pytest.mark.parametrize(
        "value, expected_type, should_match",
        [
            (None, None, True),
            (None, type(None), True),
            (None, Optional[int], True),
            (None, int | None, True),
            (None, Union[str, None], True),
            (None, Any, True),
            (None, object, True),
            (None, Literal[None], True),
            (None, Literal[1, None], True),
            (None, Literal[1, "a"], False),
            (None, int, False),
            (None, bool, False),
            (None, str, False),
            (None, list[int], False),
            (0, None, False),
            (0, type(None), False),
            (False, None, False),
            (False, type(None), False),
            ("", type(None), False),
            ([], type(None), False),
        ],
    )
    def test_none_and_none_type(self, value, expected_type, should_match):
        assert CompareTypes.between_value_and_type(value, expected_type) == should_match

    def test_none_with_type_aliases(self):
        type OptInt = int | None
        type JustNone = None
        type JustNoneType = type(None)

        assert CompareTypes.between_value_and_type(None, OptInt)
        assert CompareTypes.between_value_and_type(42, OptInt)
        assert not CompareTypes.between_value_and_type("x", OptInt)
        assert CompareTypes.between_value_and_type(None, JustNone)
        assert not CompareTypes.between_value_and_type(1, JustNone)
        assert CompareTypes.between_value_and_type(None, JustNoneType)
        assert not CompareTypes.between_value_and_type(1, JustNoneType)


class TestTuples:

    @pytest.mark.parametrize(
        "value, expected_type, should_match",
        [
            # Variadic tuples
            ((1, 2, 3), tuple[int, ...], True),
            ((), tuple[int, ...], True),
            ((1,), tuple[int, ...], True),
            ((1, "a"), tuple[int, ...], False),
            ((True,), tuple[int, ...], False),
            ([1, 2, 3], tuple[int, ...], False),
            ((1, 2, 3), Tuple[int, ...], True),
            ((), Tuple[int, ...], True),
            ((1, "a"), Tuple[int, ...], False),
            # Empty tuple type
            ((), tuple[()], True),
            ((1,), tuple[()], False),
            ([], tuple[()], False),
            ((), Tuple[()], True),
            ((1,), Tuple[()], False),
            # Bare unparameterized tuple / Tuple
            ((), tuple, True),
            ((1, "a"), tuple, True),
            ((), Tuple, True),
            ((1, "a"), Tuple, True),
            ([1, 2], Tuple, False),
            # Fixed-length tuples
            ((1, "a"), tuple[int, str], True),
            ((1,), tuple[int, str], False),
            ((1, "a", 2), tuple[int, str], False),
            ((True, "a"), tuple[int, str], False),
            # Nested variadic tuples
            ([(1, 2), (), (3,)], list[tuple[int, ...]], True),
            ([(1, 2), ("x",)], list[tuple[int, ...]], False),
        ],
    )
    def test_tuple_types(self, value, expected_type, should_match):
        assert CompareTypes.between_value_and_type(value, expected_type) == should_match


class TestParameterizedGenerics:

    @pytest.mark.parametrize(
        "value, expected_type, should_match",
        [
            # set / Set
            ({1, 2, 3}, set[int], True),
            (set(), set[int], True),
            ({1, "a"}, set[int], False),
            ({True}, set[int], False),
            ([1, 2], set[int], False),
            ({1, 2}, Set[int], True),
            ({1, "a"}, Set[int], False),
            ({1, "a"}, Set, True),
            # frozenset / FrozenSet
            (frozenset({"a", "b"}), frozenset[str], True),
            (frozenset(), frozenset[str], True),
            (frozenset({"a", 1}), frozenset[str], False),
            ({"a", "b"}, frozenset[str], False),
            (frozenset({"a"}), FrozenSet[str], True),
            (frozenset({1}), FrozenSet[str], False),
            (frozenset({1, "a"}), FrozenSet, True),
            # collections.abc.Set / MutableSet
            ({1, 2}, cabc.Set[int], True),
            (frozenset({1, 2}), cabc.Set[int], True),
            ([1, 2], cabc.Set[int], False),
            ({1, 2}, cabc.MutableSet[int], True),
            (frozenset({1, 2}), cabc.MutableSet[int], False),
            # Sequence / MutableSequence
            ([1, 2, 3], Sequence[int], True),
            ((1, 2, 3), Sequence[int], True),
            (range(3), Sequence[int], True),
            (b"abc", Sequence[int], True),
            (b"abc", Sequence[str], False),
            ("abc", Sequence[str], True),
            ("abc", Sequence[int], False),
            ([1, "a"], Sequence[int], False),
            ([True], Sequence[int], False),
            ({1, 2}, Sequence[int], False),
            ([1, 2], cabc.Sequence[int], True),
            ([1, 2], Sequence, True),
            ([1, 2], MutableSequence[int], True),
            ((1, 2), MutableSequence[int], False),
            # Mapping / MutableMapping
            ({"a": 1, "b": 2}, Mapping[str, int], True),
            ({}, Mapping[str, int], True),
            (MappingProxyType({"a": 1}), Mapping[str, int], True),
            (collections.OrderedDict([("a", 1)]), Mapping[str, int], True),
            (collections.defaultdict(int, {"a": 1}), Mapping[str, int], True),
            ({"a": "x"}, Mapping[str, int], False),
            ({"a": True}, Mapping[str, int], False),
            ({1: 2}, Mapping[str, int], False),
            ([("a", 1)], Mapping[str, int], False),
            ({"a": 1}, cabc.Mapping[str, int], True),
            ({"a": 1}, Mapping, True),
            ({"a": 1}, MutableMapping[str, int], True),
            (MappingProxyType({"a": 1}), MutableMapping[str, int], False),
            # Collection
            ([1, 2], Collection[int], True),
            ((1, 2), Collection[int], True),
            ({1, 2}, Collection[int], True),
            (frozenset({1, 2}), Collection[int], True),
            ({"a": 1}, Collection[str], True),
            ({"a": 1}, Collection[int], False),
            ([1, "a"], Collection[int], False),
            (123, Collection[int], False),
            # Iterable
            ([1, 2], Iterable[int], True),
            ((1, 2), Iterable[int], True),
            ({1, 2}, Iterable[int], True),
            ({"a": 1}, Iterable[str], True),
            ({"a": 1}, Iterable[int], False),
            ([1, "a"], Iterable[int], False),
            ([True], Iterable[int], False),
            (123, Iterable[int], False),
            # deque, Counter, defaultdict, dict views
            (collections.deque([1, 2]), collections.deque[int], True),
            (collections.deque([1, "a"]), collections.deque[int], False),
            (collections.Counter(["a", "b"]), collections.Counter[str], True),
            (collections.Counter([1, 2]), collections.Counter[str], False),
            (collections.defaultdict(int, {"a": 1}), collections.defaultdict[str, int], True),
            (collections.defaultdict(str, {"a": "x"}), collections.defaultdict[str, int], False),
            ({"a": 1}.keys(), cabc.KeysView[str], True),
            ({"a": 1}.keys(), cabc.KeysView[int], False),
            ({"a": 1}.values(), cabc.ValuesView[int], True),
            ({"a": 1}.values(), cabc.ValuesView[str], False),
            ({"a": 1}.items(), cabc.ItemsView[str, int], True),
            ({"a": 1}.items(), cabc.ItemsView[str, str], False),
            # Annotated
            (42, Annotated[int, "positive"], True),
            (True, Annotated[int, "positive"], False),
            ("x", Annotated[int, "positive"], False),
            (None, Annotated[int | None, "nullable"], True),
            # Callable, Container, type[T]
            (lambda x: str(x), Callable[[int], str], True),
            (42, Callable[[int], str], False),
            ([1, 2], cabc.Container[int], True),
            (42, cabc.Container[int], False),
            (int, type[int], True),
            (str, type[int], False),
            (bool, type[int], False),
            (bool, type[bool], True),
            (42, type[int], False),
            # Nested combinations
            ({"a": {1, 2}}, dict[str, set[int]], True),
            ({"a": {2, True}}, dict[str, set[int]], False),
            ({"k": (1, 2)}, Mapping[str, Sequence[Literal[1, 2]]], True),
            ({"k": (True,)}, Mapping[str, Sequence[Literal[1, 2]]], False),
        ],
    )
    def test_parameterized_generics(self, value, expected_type, should_match):
        assert CompareTypes.between_value_and_type(value, expected_type) == should_match

    def test_iterable_does_not_consume_iterator(self):
        it = iter([1, 2, 3])
        assert CompareTypes.between_value_and_type(it, Iterable[int])
        assert CompareTypes.between_value_and_type(it, Iterator[int])
        assert not CompareTypes.between_value_and_type(it, Collection[int])
        assert list(it) == [1, 2, 3]

    def test_custom_generic_does_not_crash(self):
        class Box[T]:
            def __init__(self, item: T):
                self.item = item

        assert CompareTypes.between_value_and_type(Box(1), Box[int])
        assert not CompareTypes.between_value_and_type(1, Box[int])


class TestLiteralTypes:

    @pytest.mark.parametrize(
        "value, expected_type, should_match",
        [
            (1, Literal[1], True),
            (2, Literal[1], False),
            (1, Literal[1, 2, 3], True),
            ("a", Literal["a", "b"], True),
            ("c", Literal["a", "b"], False),
            (b"a", Literal[b"a"], True),
            ("a", Literal[b"a"], False),
            # Strict bool vs int equality
            (True, Literal[1], False),
            (False, Literal[0], False),
            (1, Literal[True], False),
            (0, Literal[False], False),
            (True, Literal[True], True),
            (False, Literal[False], True),
            (1.0, Literal[1], False),
            # None in Literal
            (None, Literal[None], True),
            (None, Literal[1, None], True),
            (1, Literal[None], False),
        ],
    )
    def test_literal_values(self, value, expected_type, should_match):
        assert CompareTypes.between_value_and_type(value, expected_type) == should_match

    def test_literal_enum_strictness(self):
        class Color(Enum):
            RED = "red"
            BLUE = "blue"

        class Code(IntEnum):
            OK = 1
            ERR = 2

        assert CompareTypes.between_value_and_type(Color.RED, Literal[Color.RED])
        assert not CompareTypes.between_value_and_type(Color.BLUE, Literal[Color.RED])
        assert not CompareTypes.between_value_and_type("red", Literal[Color.RED])
        assert CompareTypes.between_value_and_type(Code.OK, Literal[Code.OK])
        assert not CompareTypes.between_value_and_type(1, Literal[Code.OK])
        assert not CompareTypes.between_value_and_type(Code.OK, Literal[1])

