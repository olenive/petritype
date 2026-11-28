"""``petritype.core.type_names.TypeNames``: how a type or annotation and a callable
are spelt for display, as the runner's topology and the drawings' labels show
them. ``petritype.runner.translate`` re-exports it.

The sample callables here are the ones ``test_plotting`` draws.
"""

import functools
import inspect
from typing import Any, Literal, Optional, Union

from petritype.core.type_names import TypeNames
from petritype.runner import translate

type SampleAlias = int | str


def top_level_step(x: int | str) -> int | str:
    return x


def add_offset(offset: int, value: int | str) -> int | str:
    return value


class CallableProcessor:
    def __call__(self, x: int | str) -> int | str:
        return x

    def process(self, x: int | str) -> int | str:
        return x


class NamedOnlyCallable:
    def __init__(self, name: str):
        self.__name__ = name

    def __call__(self, x: Any) -> Any:
        return x


def test_type_names_reexported_from_translate():
    assert translate.TypeNames is TypeNames


def test_type_names_of_annotations():
    assert TypeNames.of(int) == "int"
    assert TypeNames.of(int | str) == "int | str"
    assert TypeNames.of(int | None) == "int | None"
    assert TypeNames.of(Optional[int]) == "int | None"
    assert TypeNames.of(Union[int, str]) == "int | str"
    assert TypeNames.of(list[int]) == "list[int]"
    assert TypeNames.of(dict[str, tuple[int, ...]]) == "dict[str, tuple[int, ...]]"
    assert TypeNames.of(Any) == "Any"
    assert TypeNames.of(None) == "None"
    assert TypeNames.of(type(None)) == "None"
    assert TypeNames.of(Literal["a", 1]) == "Literal['a', 1]"
    assert TypeNames.of(inspect.Parameter.empty) == "Any"
    assert TypeNames.of("ForwardType") == "ForwardType"
    assert TypeNames.of(SampleAlias) == "SampleAlias"
    assert TypeNames.of(Ellipsis) == "..."


def test_type_names_callable_name():
    assert TypeNames.callable_name(top_level_step) == "top_level_step"
    assert TypeNames.callable_name(len) == "len"
    assert TypeNames.callable_name(CallableProcessor().process) == "CallableProcessor.process"
    assert TypeNames.callable_name(CallableProcessor()) == "CallableProcessor"
    assert TypeNames.callable_name(NamedOnlyCallable("custom_fn")) == "custom_fn"
    assert TypeNames.callable_name(functools.partial(add_offset, 1)) == "partial(add_offset)"
    assert TypeNames.callable_name(functools.partial(functools.partial(add_offset, 1), 2)) == "partial(add_offset)"
    assert TypeNames.callable_name(functools.partial(CallableProcessor())) == "partial(CallableProcessor)"
    assert "<lambda>" in TypeNames.callable_name(lambda x: x)
