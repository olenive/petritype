import pytest
from petritype.helpers.structures import SafeMerge


class TestSafeMerge:

    def test_dictionaries_returns_expected_dictionary(self):
        dict1 = {'a': 1, 'b': 2, 'c': 3}
        dict2 = {'d': 1, 'e': 2, 'f': 0}
        expected = {**dict1, **dict2}
        result = SafeMerge.dictionaries(dict1, dict2)
        assert result == expected

    def test_dictionaries_raises_if_dictionary_keys_overlap(self):
        dict1 = {'a': 1, 'b': 2, 'c': 3}
        dict2 = {'d': 1, 'e': 2, 'f': 0, 'b': 22}
        with pytest.raises(ValueError):
            SafeMerge.dictionaries(dict1, dict2)

    def test_dictionaries_works_for_three_dictionaries(self):
        dict1 = {'a': 1, 'b': 2, 'c': 3}
        dict2 = {'d': 1, 'e': 2, 'f': 0}
        dict3 = {'g': 2, 'h': 3, 'i': 4}
        expected = {**dict1, **dict2, **dict3}
        result = SafeMerge.dictionaries(dict1, dict2, dict3)
        assert result == expected

    def test_dictionaries_raises_with_three_dictionaries(self):
        dict1 = {'a': 1, 'b': 2, 'c': 3}
        dict2 = {'d': 1, 'e': 2, 'f': 0}
        dict3 = {'g': 2, 'h': 3, 'i': 4, 'c': 1000, 'd': 2000}
        with pytest.raises(ValueError):
            SafeMerge.dictionaries(dict1, dict2, dict3)

    def test_dictionaries_allows_common_key_value_pairs(self):
        dict1 = {'a': 1, 'b': 2, 'c': 3}
        dict2 = {'d': 1, 'e': 2, 'f': 0, 'b': 2}
        expected = {**dict1, **dict2}
        result = SafeMerge.dictionaries(dict1, dict2, allow_common_key_value_pairs=True)
        assert result == expected

    def test_dictionaries_allows_common_key_value_pairs_with_three_dictionaries(self):
        dict1 = {'a': 1, 'b': 2, 'c': 3}
        dict2 = {'d': 1, 'e': 2, 'f': 0, 'b': 2}
        dict3 = {'g': 2, 'h': 3, 'i': 4, 'c': 3}
        expected = {**dict1, **dict2, **dict3}
        result = SafeMerge.dictionaries(dict1, dict2, dict3, allow_common_key_value_pairs=True)
        assert result == expected

    def test_dictionaries_raises_if_common_key_value_pairs_not_allowed(self):
        dict1 = {'a': 1, 'b': 2, 'c': 3}
        dict2 = {'d': 1, 'e': 2, 'f': 0, 'b': 2}
        with pytest.raises(ValueError):
            SafeMerge.dictionaries(dict1, dict2)

    def test_dictionaries_raises_if_common_key_value_pairs_not_allowed_with_three_dictionaries(self):
        dict1 = {'a': 1, 'b': 2, 'c': 3}
        dict2 = {'d': 1, 'e': 2, 'f': 0, 'b': 2}
        dict3 = {'g': 2, 'h': 3, 'i': 4, 'c': 1000}
        with pytest.raises(ValueError):
            SafeMerge.dictionaries(dict1, dict2, dict3)
