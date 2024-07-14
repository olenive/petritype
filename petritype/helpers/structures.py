from functools import reduce, partial
from typing import Dict


class SafeMerge:

    def _two_dictionaries(dict1: Dict, dict2: Dict, allow_common_key_value_pairs=False) -> Dict:

        def intersection_keys_with_different_tokens(dict1: Dict, dict2: Dict) -> Dict:
            return {key: (dict1[key], dict2[key]) for key in intersection if dict1[key] != dict2[key]}

        keys1 = set(dict1.keys())
        keys2 = set(dict2.keys())
        intersection = keys1.intersection(keys2)
        if len(intersection) == 0:
            return {**dict1, **dict2}
        elif allow_common_key_value_pairs:
            keys_to_different_tokens = intersection_keys_with_different_tokens(dict1, dict2)
            if len(keys_to_different_tokens) == 0:
                return {**dict1, **dict2}
            raise ValueError(
                "Unable to safely merge dictionaries as the following keys are present "
                + f"more than once with different tokens:\n{intersection}"
            )
        else:
            raise ValueError(
                "Unable to safely merge dictionaries as the following keys are present "
                + f"more than once:\n{intersection}"
            )

    def dictionaries(*dicts: Dict, allow_common_key_value_pairs=False) -> Dict:
        try:
            combined = reduce(
                partial(SafeMerge._two_dictionaries, allow_common_key_value_pairs=allow_common_key_value_pairs),
                dicts,
            )
        except ValueError as error:
            raise error
        return combined
