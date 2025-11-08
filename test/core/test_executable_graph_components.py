import pytest
from typing import Optional, Union
from petritype.core.executable_graph_components import ExecutableGraphCheck, ListPlaceNode


class TestExecutableGraphCheck:

    def test_between_value_and_places_all_match(self):
        places = [
            ListPlaceNode(name='place1', type=int),
            ListPlaceNode(name='place2', type=int),
            ListPlaceNode(name='place3', type=int)
        ]
        value = 42
        result = ExecutableGraphCheck.value_and_places_types_match(value, places)
        assert list(result) == places

    def test_between_value_and_places_some_match(self):
        places = [
            ListPlaceNode(name='place1', type=int),
            ListPlaceNode(name='place2', type=str),
            ListPlaceNode(name='place3', type=int)
        ]
        value = 42
        result = ExecutableGraphCheck.value_and_places_types_match(value, places)
        assert list(result) == [places[0], places[2]]

    def test_between_value_and_places_none_match(self):
        places = [
            ListPlaceNode(name='place1', type=str),
            ListPlaceNode(name='place2', type=str),
            ListPlaceNode(name='place3', type=str)
        ]
        value = 42
        result = ExecutableGraphCheck.value_and_places_types_match(value, places)
        assert list(result) == []

    def test_between_value_and_places_with_optional_types(self):
        places = [
            ListPlaceNode(name='place1', type=Optional[int]),
            ListPlaceNode(name='place2', type=Optional[str]),
            ListPlaceNode(name='place3', type=Optional[int])
        ]
        value = None
        result = ExecutableGraphCheck.value_and_places_types_match(value, places)
        assert list(result) == places

        value = 42
        result = ExecutableGraphCheck.value_and_places_types_match(value, places)
        assert list(result) == [places[0], places[2]]

    @pytest.mark.parametrize(
        "value, expected_indices",
        [
            # Note: check_type accepts ints as floats, so int 42 matches Union[str, float]
            (42, [0, 2]),  # We don't want int to match float here.
            (3.14, [1, 2]),  # We don't want float to match int here.
            ("test", [0, 1]),  # matches place1 (Union[int, str]), place2 (Union[str, float])
        ]
    )
    def test_between_value_and_places_with_union_types(self, value, expected_indices):
        places = [
            ListPlaceNode(name='place0', type=Union[int, str]),
            ListPlaceNode(name='place1', type=Union[str, float]),
            ListPlaceNode(name='place2', type=Union[int, float])
        ]
        result = ExecutableGraphCheck.value_and_places_types_match(value, places)
        expected_places = [places[i] for i in expected_indices]
        assert list(result) == expected_places

    def test_value_and_places_types_match_debug_01(self):
        type DBKey = str
        type DBValue = str
        type DBKeyValuePair = tuple[DBKey, DBValue]
        potential_places = (
            ListPlaceNode(name='CachedValueFound', type=DBKeyValuePair, tokens=[]),
            ListPlaceNode(name='KeyForDBRetrieval', type=DBKey, tokens=[])
        )
        expected = (ListPlaceNode(name='KeyForDBRetrieval', type=DBKey, tokens=[]),)
        result = ExecutableGraphCheck.value_and_places_types_match('h_0', potential_places)
        assert result == expected

    def test_value_and_places_types_match_inside_list(self):
        """If the given value is a list and the place is of type ListPlaceNode, check if the inner types match."""
        places = [
            ListPlaceNode(name='place1', type=int),
            ListPlaceNode(name='place2', type=list[int]), # Second inner list so this should not match.
            ListPlaceNode(name='place3', type=int),
            ListPlaceNode(name='place4', type=list[str]),
            ListPlaceNode(name='place5', type=str),
        ]
        value = [42, 43]
        result = ExecutableGraphCheck.value_and_places_types_match(value, places)
        assert list(result) == [places[0], places[2]]

    @pytest.mark.parametrize(
        "value, expected_indices",
        [
            ("hello", [0]),  # str matches place1 (type=str)
            (100, [1]),      # int matches place2 (type=int)
            (3.14, [2]),     # float matches place3 (type=float)
        ]
    )
    def test_value_and_places_types_match_str_int_float_case(self, value, expected_indices):
        places = [
            ListPlaceNode(name='place1', type=str),
            ListPlaceNode(name='place2', type=int),
            ListPlaceNode(name='place3', type=float),
        ]
        result = ExecutableGraphCheck.value_and_places_types_match(value, places)
        expected_places = [places[i] for i in expected_indices]
        assert list(result) == expected_places
