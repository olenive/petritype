from typing import Any, Type, Union, get_origin, get_args, TypeAliasType
from typeguard import check_type, TypeCheckError


class CompareTypes:

    def using_check_type(value: Any, type_: Type) -> bool:
        try:
            check_type(value, type_)
            return True
        except TypeCheckError:
            return False

    def between_value_and_type(value: Any, expected: Type) -> bool:
        if expected is Any:
            return True

        # Handle None tokens gracefully
        if value is None:
            if expected is None:
                return True
            # Check if the place type is Optional or Union with None
            if get_origin(expected) is Union and type(None) in get_args(expected):
                return True
            return False

        # Note: at some point we may need to check .__origin__ as well?         
        if isinstance(expected, TypeAliasType):
            return CompareTypes.between_value_and_type(value, expected.__value__)

        return CompareTypes.using_check_type(value, expected)
