"""Short display names for Python type annotations and callables."""

from __future__ import annotations

import functools
import inspect
import re
import types
from typing import Any, Literal, TypeAliasType, Union, get_args, get_origin


class TypeNames:
    """Functions that spell Python types and callables for display."""

    @staticmethod
    def of(annotation: Any) -> str:
        """Short text for a type or annotation: class names without module paths,
        generics with their arguments, unions with ``|``. A string annotation
        (postponed evaluation) is returned as written."""
        if annotation is inspect.Parameter.empty:
            return "Any"
        if isinstance(annotation, str):
            return annotation
        if annotation is None or annotation is type(None):
            return "None"
        if annotation is Ellipsis:
            return "..."
        if isinstance(annotation, TypeAliasType):
            return annotation.__name__
        origin, args = get_origin(annotation), get_args(annotation)
        if origin is Union or origin is types.UnionType:
            return " | ".join(TypeNames.of(arg) for arg in args)
        if origin is Literal:
            return f"Literal[{', '.join(repr(arg) for arg in args)}]"
        if origin is not None and args:
            return f"{TypeNames.of(origin)}[{', '.join(TypeNames.of(arg) for arg in args)}]"
        name = getattr(annotation, "__name__", None)
        if isinstance(name, str):
            return name
        return re.sub(r"(?:\w+\.)+(\w+)", r"\1", str(annotation))

    @staticmethod
    def callable_name(fn: Any) -> str:
        """Readable name for a callable: ``__qualname__`` when present,
        ``partial(<inner>)`` for ``functools.partial``, ``__name__`` otherwise,
        falling back to the callable's class name (for callable instances)."""
        if isinstance(fn, functools.partial):
            return f"partial({TypeNames.callable_name(fn.func)})"
        qualname = getattr(fn, "__qualname__", None)
        if isinstance(qualname, str) and qualname:
            return qualname
        name = getattr(fn, "__name__", None)
        if isinstance(name, str) and name:
            return name
        return type(fn).__name__
