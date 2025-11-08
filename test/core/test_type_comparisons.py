from unittest import result
from pydantic import BaseModel
import pytest
from petritype.core.type_comparisons import CompareTypes
from typing import Any, Dict, Optional, Union


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
