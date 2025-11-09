import pytest
from typing import Optional, Union
from petritype.core.executable_graph_components import ExecutableGraphCheck, ExecutableGraphOperations, ListPlaceNode


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


class TestExecutableGraphOperations:

    def test_add_tokens_to_places_single_token_to_single_place(self):
        """Test adding a single token to a single place."""
        place = ListPlaceNode(name='place1', type=int, tokens=[])
        place_names_to_nodes = {'place1': place}
        tokens_to_add = {'place1': 42}
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, place_names_to_nodes, allow_token_copying=False
        )
        expected = {'place1': place}
        assert place.tokens == [42]
        assert result == expected

    def test_add_tokens_to_places_list_tokens_to_single_place_extend(self):
        """Test adding a list of tokens to a single place (should extend)."""
        place = ListPlaceNode(name='place1', type=int, tokens=[1, 2, 3])
        place_names_to_nodes = {'place1': place}
        tokens_to_add = {'place1': [42, 43, 44]}
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, place_names_to_nodes, allow_token_copying=False
        )
        expected = {'place1': place}
        assert place.tokens == [1, 2, 3, 42, 43, 44]
        assert result == expected

    @pytest.mark.parametrize(
        "initial_tokens, expected_tokens",
        (
            ([], [[42, 43, 44]]),
            ([[], []], [[], [], [42, 43, 44]]),
            ([[1, 2, 3]], [[1, 2, 3], [42, 43, 44]]),
            ([[1, 2], [3, 4]], [[1, 2], [3, 4], [42, 43, 44]]),
            ([[1, 2, 3], [4, 5]], [[1, 2, 3], [4, 5], [42, 43, 44]])
        )
    )
    def test_add_tokens_to_places_list_type_to_single_place_append(self, initial_tokens, expected_tokens):
        """Test adding a list to a place that expects list type (should append)."""
        place = ListPlaceNode(name='place1', type=list[int], tokens=initial_tokens)
        place_names_to_nodes = {'place1': place}
        tokens_to_add = {'place1': [42, 43, 44]}
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, place_names_to_nodes, allow_token_copying=False
        )
        expected = {'place1': place}
        assert place.tokens == expected_tokens
        assert result == expected

    def test_add_tokens_to_places_raises_when_a_token_is_added_to_multiple_places_without_copying(self):
        place1 = ListPlaceNode(name='place1', type=int, tokens=[])
        place2 = ListPlaceNode(name='place2', type=int, tokens=[])
        the_token = [43]
        place_names_to_nodes = {'place1': place1, 'place2': place2}
        with pytest.raises(
            RuntimeError,
            match="Token is being added to multiple places but token copying is not allowed."
        ):
            ExecutableGraphOperations.add_tokens_to_places(
                {'place1': the_token, 'place2': the_token}, place_names_to_nodes, allow_token_copying=False
            )

    @pytest.mark.parametrize(
        "token_value, place_type, verify_deepcopy",
        [
            (42, int, False),
            ("hello", str, False),
            ([1, 2, 3], list[int], True),
            ({'key': 'value'}, dict, True),
            ((1, 2), tuple, False),
            ([{'a': 1}, {'b': 2}], list[dict[str, int]], True),
        ]
    )
    def test_add_tokens_to_places_single_token_to_multiple_places_with_copying(
        self, token_value, place_type, verify_deepcopy
    ):
        """Test adding the same token to multiple places with copying enabled."""
        place1 = ListPlaceNode(name='place1', type=place_type, tokens=[])
        place2 = ListPlaceNode(name='place2', type=place_type, tokens=[])
        place_names_to_nodes = {'place1': place1, 'place2': place2}
        tokens_to_add = {'place1': token_value, 'place2': token_value}
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, place_names_to_nodes, allow_token_copying=True
        )
        assert place1.tokens == [token_value]
        assert place2.tokens == [token_value]
        assert len(result) == 2
        # Verify they are different objects (deepcopied) for mutable types
        if verify_deepcopy:
            assert place1.tokens[0] is not place2.tokens[0]

    @pytest.mark.parametrize(
        "place_configs, tokens_dict, expected_results",
        [
            # Different basic types
            (
                [('place1', int), ('place2', str)],
                {'place1': 42, 'place2': 'hello'},
                {'place1': [42], 'place2': ['hello']}
            ),
            (
                [('place1', str), ('place2', str)],
                {'place1': "123", 'place2': 'hello'},
                {'place1': ["123"], 'place2': ['hello']}
            ),
            # Three places with different types
            (
                [('place1', int), ('place2', str), ('place3', float)],
                {'place1': 100, 'place2': 'world', 'place3': 3.14},
                {'place1': [100], 'place2': ['world'], 'place3': [3.14]}
            ),
            # Mixed with lists
            (
                [('place1', list[int]), ('place2', int)],
                {'place1': [1, 2, 3], 'place2': 99},
                {'place1': [[1, 2, 3]], 'place2': [99]}
            ),
            # Multiple places with mixed types
            (
                [('place1', str), ('place2', dict), ('place3', list[int])],
                {'place1': 'test', 'place2': {'x': 1}, 'place3': [7, 8]},
                {'place1': ['test'], 'place2': [{'x': 1}], 'place3': [[7, 8]]}
            ),
        ]
    )
    def test_add_tokens_to_places_different_tokens_to_multiple_places(
        self, place_configs, tokens_dict, expected_results
    ):
        """Test adding different tokens to multiple places."""
        places = {name: ListPlaceNode(name=name, type=ptype, tokens=[]) 
                  for name, ptype in place_configs}
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_dict, places, allow_token_copying=False
        )
        for place_name, expected_tokens in expected_results.items():
            assert places[place_name].tokens == expected_tokens
        assert len(result) == len(expected_results)

    @pytest.mark.parametrize(
        "list_value, place_type",
        [
            ([42, 43], int),  # Simple int list
            (['a', 'b', 'c'], str),  # String list
            ([1.5, 2.5, 3.5], float),  # Float list
            ([1, 2, 3, 4, 5], int),  # Longer list
            ([[1, 2], [3, 4]], list),  # Nested lists
            ([{'x': 1}, {'y': 2}], dict),  # List of dicts
        ]
    )
    def test_add_tokens_to_places_list_tokens_to_multiple_places_with_copying(
        self, list_value, place_type
    ):
        """Test adding lists to multiple places with copying enabled."""
        place1 = ListPlaceNode(name='place1', type=place_type, tokens=[])
        place2 = ListPlaceNode(name='place2', type=place_type, tokens=[])
        place_names_to_nodes = {'place1': place1, 'place2': place2}
        tokens_to_add = {'place1': list_value, 'place2': list_value}
        
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, place_names_to_nodes, allow_token_copying=True
        )
        
        assert place1.tokens == list_value
        assert place2.tokens == list_value
        # Verify they are different list objects
        assert place1.tokens is not place2.tokens
        assert len(result) == 2

    @pytest.mark.parametrize(
        "initial_tokens, place_type, allow_copying",
        [
            ([], int, False),  # Empty place, no copying
            ([], str, True),  # Empty place, with copying
            ([1, 2, 3], int, False),  # Place with existing tokens, no copying
            (['a', 'b'], str, True),  # Place with existing tokens, with copying
            ([[1, 2]], list, False),  # Place with nested structure
        ]
    )
    def test_add_tokens_to_places_empty_dict(self, initial_tokens, place_type, allow_copying):
        """Test with empty token dict - should not modify places."""
        place = ListPlaceNode(name='place1', type=place_type, tokens=initial_tokens.copy())
        place_names_to_nodes = {'place1': place}
        tokens_to_add = {}
        
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, place_names_to_nodes, allow_token_copying=allow_copying
        )
        
        assert place.tokens == initial_tokens
        assert len(result) == 0

    @pytest.mark.parametrize(
        "place_type, initial_tokens, allow_copying",
        [
            (Optional[int], [], False),  # Empty place with optional int
            (Optional[str], [], True),  # Empty place with optional str, copying enabled
            (Optional[int], [1, 2], False),  # Place with existing tokens
            (Optional[list], [[1, 2]], True),  # Optional list type
            (Optional[dict], [{'key': 'val'}], False),  # Place with existing dict
            (Optional[float], [], True),  # Optional float
        ]
    )
    def test_add_tokens_to_places_none_token(self, place_type, initial_tokens, allow_copying):
        """Test handling None tokens (should skip)."""
        place = ListPlaceNode(name='place1', type=place_type, tokens=initial_tokens.copy())
        place_names_to_nodes = {'place1': place}
        tokens_to_add = {'place1': None}
        
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, place_names_to_nodes, allow_token_copying=allow_copying
        )
        
        # None should be skipped, tokens unchanged
        assert place.tokens == initial_tokens
        assert len(result) == 0

    @pytest.mark.parametrize(
        "initial_tokens, new_token, place_type, expected_tokens",
        [
            ([1, 2, 3], 42, int, [1, 2, 3, 42]),  # Adding int to existing ints
            (['a', 'b'], 'c', str, ['a', 'b', 'c']),  # Adding str to existing strs
            ([1.0, 2.0], 3.5, float, [1.0, 2.0, 3.5]),  # Adding float to existing floats
            ([10], 20, int, [10, 20]),  # Single existing token
            ([], 99, int, [99]),  # Empty initial (edge case)
            ([1, 2, 3, 4, 5], 6, int, [1, 2, 3, 4, 5, 6]),  # Many existing tokens
            ([{'a': 1}], {'b': 2}, dict, [{'a': 1}, {'b': 2}]),  # Dicts
            ([[1, 2]], [3, 4], list[int], [[1, 2], [3, 4]]),  # Lists
        ]
    )
    def test_add_tokens_to_places_existing_tokens(
        self, initial_tokens, new_token, place_type, expected_tokens
    ):
        """Test adding tokens to a place that already has tokens."""
        place = ListPlaceNode(name='place1', type=place_type, tokens=initial_tokens.copy())
        place_names_to_nodes = {'place1': place}
        tokens_to_add = {'place1': new_token}
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, place_names_to_nodes, allow_token_copying=False
        )
        assert place.tokens == expected_tokens
        assert len(result) == 1

    @pytest.mark.parametrize(
        "original_object, place_type, modify_key, modify_value, original_value",
        [
            # Nested dict
            (
                {'key': 'value', 'nested': {'inner': 42}},
                dict,
                'key',
                'modified',
                'value'
            ),
            # Dict with list values
            (
                {'items': [1, 2, 3], 'name': 'test'},
                dict,
                'name',
                'changed',
                'test'
            ),
            # List with nested dicts
            (
                [{'a': 1}, {'b': 2}, {'c': 3}],
                list[dict[str, int]],
                0,  # Modify first element
                {'a': 999},
                {'a': 1}
            ),
            # List with nested lists
            (
                [[1, 2], [3, 4], [5, 6]],
                list[list[int]],
                1,  # Modify second element
                [99, 99],
                [3, 4]
            ),
            # Complex nested structure
            (
                {'data': [{'x': [1, 2]}, {'y': [3, 4]}]},
                dict,
                'data',
                [],
                [{'x': [1, 2]}, {'y': [3, 4]}]
            ),
        ]
    )
    def test_add_tokens_to_places_complex_objects_with_copying(
        self, original_object, place_type, modify_key, modify_value, original_value
    ):
        """Test copying complex objects with deep copy verification."""
        place1 = ListPlaceNode(name='place1', type=place_type, tokens=[])
        place2 = ListPlaceNode(name='place2', type=place_type, tokens=[])
        place_names_to_nodes = {'place1': place1, 'place2': place2}
        tokens_to_add = {'place1': original_object, 'place2': original_object}
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, place_names_to_nodes, allow_token_copying=True
        )
        assert place1.tokens == [original_object]
        assert place2.tokens == [original_object]
        # Verify deep copy - modify one shouldn't affect the other
        place1.tokens[0][modify_key] = modify_value
        assert place2.tokens[0][modify_key] == original_value
        assert len(result) == 2

    @pytest.mark.parametrize(
        "model_params, modify_attr, modify_value, original_value",
        [
            # Simple Pydantic model
            ({'a': 1, 'b': 'test'}, 'a', 999, 1),
            # Different values
            ({'a': 42, 'b': 'hello'}, 'b', 'modified', 'hello'),
            # Another set
            ({'a': 100, 'b': 'world'}, 'a', 0, 100),
        ]
    )
    def test_add_tokens_to_places_with_pydantic_models_with_copying(
        self, model_params, modify_attr, modify_value, original_value
    ):
        """Test adding tokens that are Pydantic models with copying enabled."""
        from pydantic import BaseModel

        class MyModel(BaseModel):
            a: int
            b: str

        original_model = MyModel(**model_params)
        place1 = ListPlaceNode(name='place1', type=MyModel, tokens=[])
        place2 = ListPlaceNode(name='place2', type=MyModel, tokens=[])
        place_names_to_nodes = {'place1': place1, 'place2': place2}
        tokens_to_add = {'place1': original_model, 'place2': original_model}
        
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, place_names_to_nodes, allow_token_copying=True
        )
        
        assert place1.tokens == [original_model]
        assert place2.tokens == [original_model]
        # Verify deep copy - modify one shouldn't affect the other
        setattr(place1.tokens[0], modify_attr, modify_value)
        assert getattr(place2.tokens[0], modify_attr) == original_value
        assert len(result) == 2

    @pytest.mark.parametrize(
        "place_configs_and_models",
        [
            # Two places with different model instances
            [
                ('place1', {'a': 1, 'b': 'test'}),
                ('place2', {'a': 2, 'b': 'hello'})
            ],
            # Three places with different model instances
            [
                ('place1', {'a': 10, 'b': 'first'}),
                ('place2', {'a': 20, 'b': 'second'}),
                ('place3', {'a': 30, 'b': 'third'})
            ],
            # Two places with more diverse values
            [
                ('place1', {'a': 100, 'b': 'alpha'}),
                ('place2', {'a': 200, 'b': 'beta'})
            ],
        ]
    )
    def test_add_tokens_to_places_with_pydantic_models_sans_copying(
        self, place_configs_and_models
    ):
        """Test adding different Pydantic model instances to multiple places without copying."""
        from pydantic import BaseModel

        class MyModel(BaseModel):
            a: int
            b: str

        # Create different model instances for each place
        places = {}
        tokens_to_add = {}
        expected_values = {}
        
        for place_name, model_params in place_configs_and_models:
            model_instance = MyModel(**model_params)
            places[place_name] = ListPlaceNode(name=place_name, type=MyModel, tokens=[])
            tokens_to_add[place_name] = model_instance
            expected_values[place_name] = model_params
        
        result = ExecutableGraphOperations.add_tokens_to_places(
            tokens_to_add, places, allow_token_copying=False
        )
        
        # Verify each place has the correct token
        for place_name, expected_params in expected_values.items():
            assert len(places[place_name].tokens) == 1
            assert places[place_name].tokens[0].a == expected_params['a']
            assert places[place_name].tokens[0].b == expected_params['b']
        
        assert len(result) == len(place_configs_and_models)
        
        # Verify that modifying one place's token doesn't affect others
        first_place = list(places.keys())[0]
        places[first_place].tokens[0].a = 9999
        places[first_place].tokens[0].b = 'modified'
        
        # Check that other places remain unchanged
        for i, (place_name, expected_params) in enumerate(expected_values.items()):
            if i > 0:  # Skip the first place we just modified
                assert places[place_name].tokens[0].a == expected_params['a']
                assert places[place_name].tokens[0].b == expected_params['b']
