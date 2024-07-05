from pydantic import BaseModel
from petritype.core.type_comparisons import CompareTypes
from typing import Dict, Optional, Union


class TestCompareTypes:

    def test_between_value_and_type_given_none_int(self):
        value = None
        expected = int
        assert not CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_int(self):
        value = 42
        expected = int
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_str(self):
        value = "a string"
        expected = str
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_dict(self):
        value = {"key": "value"}
        expected = dict
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_dict_with_str_int_true(self):
        value = {"key": 42}
        expected = dict[str, int]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_dict_with_str_int_false_0(self):
        value = {"key": 42}
        expected = str
        assert not CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_dict_with_str_int_false_1(self):
        value = {"key": 42}
        expected = list
        assert not CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_dict_with_str_int_false_2(self):
        value = {"key": 42}
        expected = dict[str, str]
        assert not CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_generic_dict_with_str_int_false_2(self):
        value = {"key": 42}
        expected = Dict[str, str]
        assert not CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_list(self):
        value = [1, 2, 3]
        expected = list
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_list_with_int(self):
        value = [1, 2, 3]
        expected = list[int]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_list_with_str(self):
        value = ["a", "b", "c"]
        expected = list[str]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_list_with_mixed(self):
        value = [1, "a", 3.0]
        expected = list[Union[int, str, float]]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_optional_int_none(self):
        value = None
        expected = Optional[int]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_optional_int_value(self):
        value = 42
        expected = Optional[int]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_union(self):
        value = 42
        expected = Union[int, str]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_given_union_str(self):
        value = "a string"
        expected = Union[int, str]
        assert CompareTypes.between_value_and_type(value, expected)

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
        """check_type accepts ints as floats in this case"""
        value = 42
        expected = Union[str, float]
        assert CompareTypes.between_value_and_type(value, expected)

    def test_between_value_and_type_with_type_alias_int(self):
        type MyAlias = int
        assert CompareTypes.between_value_and_type(123, MyAlias)
        assert not CompareTypes.between_value_and_type("abc", MyAlias)

    def test_between_value_and_type_with_type_alias_tuple_str_int(self):
        type MyAlias = tuple[str, int]
        assert CompareTypes.between_value_and_type(("abc", 123), MyAlias)
        assert not CompareTypes.between_value_and_type("efg", MyAlias)

