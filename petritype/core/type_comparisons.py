from typing import Any, Type, Union, get_origin, get_args, TypeAliasType
from types import UnionType
from typeguard import check_type, TypeCheckError


class CompareTypes:

    # def using_check_type(value: Any, type_: Type) -> bool:
    #     try:
    #         check_type(value, type_)
    #         return True
    #     except TypeCheckError:
    #         return False

    # def between_value_and_type(value: Any, type_: Type) -> bool:
    #     if type_ is Any:
    #         return True

    #     # Handle None tokens gracefully
    #     if value is None:
    #         if type_ is None:
    #             return True
    #         # Check if the place type is Optional or Union with None
    #         if get_origin(type_) is Union and type(None) in get_args(type_):
    #             return True
    #         return False

    #     # Note: at some point we may need to check .__origin__ as well?
    #     if isinstance(type_, TypeAliasType):
    #         return CompareTypes.between_value_and_type(value, type_.__value__)

    #     # return CompareTypes.using_check_type(value, type_)
    #     return isinstance(value, type_)

    def between_value_and_type(value: Any, type_: Type) -> bool:
        """
        Strict type checking that treats int and float as distinct types.
        Unlike typeguard's check_type, this does NOT accept int as float.
        """
        if type_ is Any:
            return True
    
        # Handle None tokens gracefully
        if value is None:
            if type_ is None:
                return True
            # Check if the place type is Optional or Union with None
            if get_origin(type_) in (Union, UnionType) and type(None) in get_args(type_):
                return True
            return False
    
        # Handle TypeAliasType
        if isinstance(type_, TypeAliasType):
            return CompareTypes.between_value_and_type(value, type_.__value__)
        
        origin = get_origin(type_)
        
        # Handle Union types
        if origin in (Union, UnionType):
            return any(
                CompareTypes.between_value_and_type(value, arg)
                for arg in get_args(type_)
            )
        
        # Handle parameterized generics (list[int], dict[str, int], tuple[str, str], etc.)
        if origin is not None:
            # First check if the value is an instance of the origin type
            if not isinstance(value, origin):
                return False
            
            args = get_args(type_)
            if not args:
                return True
            
            # Check tuple types
            if origin is tuple:
                if not isinstance(value, tuple):
                    return False
                if len(args) != len(value):
                    return False
                return all(
                    CompareTypes.between_value_and_type(v, t)
                    for v, t in zip(value, args)
                )
            
            # Check list types
            if origin is list:
                if not isinstance(value, list):
                    return False
                if not args:
                    return True
                inner_type = args[0]
                return all(
                    CompareTypes.between_value_and_type(item, inner_type)
                    for item in value
                )
            
            # Check dict types
            if origin is dict:
                if not isinstance(value, dict):
                    return False
                if len(args) != 2:
                    return True
                key_type, val_type = args
                return all(
                    CompareTypes.between_value_and_type(k, key_type) and
                    CompareTypes.between_value_and_type(v, val_type)
                    for k, v in value.items()
                )
        
        # Simple type check - STRICT: int and float are different
        # Use type() instead of isinstance() for strict checking
        return type(value) is type_ or isinstance(value, type_)

    def between_annotations(annotation1: Type, annotation2: Type) -> bool:
        if annotation1 == annotation2:
            return True

        # Handle Any.
        if annotation1 is Any or annotation2 is Any:
            return True

        # Handle TypeAliasType.
        if isinstance(annotation1, TypeAliasType):
            return CompareTypes.between_annotations(annotation1.__value__, annotation2)
        if isinstance(annotation2, TypeAliasType):
            return CompareTypes.between_annotations(annotation1, annotation2.__value__)

        return False

    def between_annotations_where_one_maybe_in_list(
        *,
        annotation_not_in_list: Type,
        annotation_maybe_in_list: Type
    ) -> bool:
        """Compare a type annotation that is not in a list with one that may be in a list.

        For example, compare `int` with `list[int]` or `str` with `list[str]`.
        If both annotations are the same (e.g., `int` and `int`), return True.
        """
        origin = get_origin(annotation_maybe_in_list)
        args = get_args(annotation_maybe_in_list)

        if origin is list and len(args) == 1:
            inner_type = args[0]
            return CompareTypes.between_annotations(annotation_not_in_list, inner_type)

        return CompareTypes.between_annotations(annotation_not_in_list, annotation_maybe_in_list)

    def between_annotations_where_both_maybe_in_list(
        annotation1: Type,
        annotation2: Type
    ) -> bool:
        # TODO: Delete this function if it is not needed.
        """Compare two type annotations that may be in lists.

        Handles both symmetric cases (list[T] vs list[T]) and asymmetric cases
        where one is a list and the other is the element type (T vs list[T]).
        
        For Petri nets: checks if a token of type T can be extracted from a 
        place holding list[T], or vice versa.
        """
        origin1 = get_origin(annotation1)
        args1 = get_args(annotation1)

        origin2 = get_origin(annotation2)
        args2 = get_args(annotation2)

        # Case 1: Both are lists - compare element types
        if origin1 is list and origin2 is list and len(args1) == 1 and len(args2) == 1:
            inner_type1 = args1[0]
            inner_type2 = args2[0]
            return CompareTypes.between_annotations(inner_type1, inner_type2)
        
        # Case 2: One is a list, one is not - compare element type with non-list type
        if origin1 is list and origin2 is not list and len(args1) == 1:
            inner_type1 = args1[0]
            return CompareTypes.between_annotations(inner_type1, annotation2)
        
        if origin2 is list and origin1 is not list and len(args2) == 1:
            inner_type2 = args2[0]
            return CompareTypes.between_annotations(annotation1, inner_type2)

        # Case 3: Neither is a list - direct comparison
        return CompareTypes.between_annotations(annotation1, annotation2)