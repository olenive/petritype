from collections.abc import ItemsView, Iterable, Iterator, Mapping
from types import UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    Tuple,
    Type,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
)


class CompareTypes:

    @staticmethod
    def between_value_and_type(value: Any, type_: Type) -> bool:
        """
        Strict type checking that treats int, float, and bool as distinct types.
        Unlike typeguard's check_type, this does NOT accept int as float,
        and unlike plain isinstance(), this does NOT accept bool as int.
        """
        if type_ is Any:
            return True

        # Handle None and type(None)
        if type_ is None or type_ is type(None):
            return value is None

        # Handle TypeAliasType (PEP 695 `type X = ...`)
        if isinstance(type_, TypeAliasType):
            return CompareTypes.between_value_and_type(value, type_.__value__)

        origin = get_origin(type_)

        # Handle Annotated[T, ...]
        if origin is Annotated:
            args = get_args(type_)
            if args:
                return CompareTypes.between_value_and_type(value, args[0])
            return False

        # Handle Literal[...] with strict type equality (bool != int, int != float)
        if origin is Literal:
            return any(
                type(value) is type(arg) and value == arg
                for arg in get_args(type_)
            )

        # Handle Union types (Union[A, B], Optional[A], A | B)
        if origin in (Union, UnionType):
            return any(
                CompareTypes.between_value_and_type(value, arg)
                for arg in get_args(type_)
            )

        # Handle parameterized generics (tuple, list, dict, set, frozenset,
        # Sequence, Mapping, Collection, Iterable, Callable, etc.)
        if origin is not None:
            if not isinstance(origin, type):
                return False

            if not isinstance(value, origin):
                return False

            args = get_args(type_)

            # Check tuple types before `if not args` because tuple[()] has args == ()
            if origin is tuple:
                if type_ is Tuple:
                    return True
                if not args or args == ((),):
                    return len(value) == 0
                if len(args) == 2 and args[1] is Ellipsis:
                    inner_type = args[0]
                    return all(
                        CompareTypes.between_value_and_type(item, inner_type)
                        for item in value
                    )
                if len(args) != len(value):
                    return False
                return all(
                    CompareTypes.between_value_and_type(v, t)
                    for v, t in zip(value, args)
                )

            if not args:
                return True

            # Check type[T]
            if origin is type and len(args) == 1:
                target = args[0]
                if target is Any:
                    return True
                if isinstance(target, type):
                    if target is int and value is bool:
                        return False
                    return issubclass(value, target)
                if get_origin(target) in (Union, UnionType):
                    return any(
                        CompareTypes.between_value_and_type(value, type[a])
                        for a in get_args(target)
                    )
                return True

            # Check mapping types (dict, Mapping, MutableMapping, defaultdict, etc.)
            if issubclass(origin, Mapping):
                if len(args) == 2:
                    key_type, val_type = args
                    return all(
                        CompareTypes.between_value_and_type(k, key_type)
                        and CompareTypes.between_value_and_type(v, val_type)
                        for k, v in value.items()
                    )
                if len(args) == 1:
                    key_type = args[0]
                    return all(
                        CompareTypes.between_value_and_type(k, key_type)
                        for k in value
                    )
                return True

            # Check ItemsView[K, V]
            if issubclass(origin, ItemsView) and len(args) == 2:
                key_type, val_type = args
                return all(
                    CompareTypes.between_value_and_type(k, key_type)
                    and CompareTypes.between_value_and_type(v, val_type)
                    for k, v in value
                )

            # Check single-parameter iterables (list, set, frozenset, Sequence,
            # Collection, Iterable, deque, KeysView, ValuesView, etc.)
            if issubclass(origin, Iterable) and not issubclass(origin, Iterator) and len(args) == 1:
                # Do not consume one-shot iterators/generators
                if isinstance(value, Iterator):
                    return True
                inner_type = args[0]
                return all(
                    CompareTypes.between_value_and_type(item, inner_type)
                    for item in value
                )

            # Other parameterized generics (e.g. Callable[[int], str], Iterator[int],
            # Container[int], custom Generic[T]): origin check already passed.
            return True

        # Strict check: reject bool when int is expected (bool subclasses int in Python).
        if type_ is int and isinstance(value, bool):
            return False

        return isinstance(value, type_)

    @staticmethod
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

    @staticmethod
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

    @staticmethod
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
