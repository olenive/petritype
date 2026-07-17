"""Unit tests pinning which values ListPlaceNode accepts as a place *type*.

``ListPlaceNode.validate_type_field`` decides what may serve as a place's type.
It must accept anything usable as a type annotation — plain classes,
parameterised generics and unions in both spellings, and PEP 695 aliases — and
reject non-type values (strings, ints, instances).

The check was rewritten (House Keeping item 1) from ``isinstance`` tests against
typing's private ``_GenericAlias`` / ``_UnionGenericAlias`` (deprecated, removed
in Python 3.17) to the public ``typing.get_origin``: every subscripted generic
or union has a non-None origin, a plain class has origin None but is a ``type``,
and a bare PEP 695 alias has origin None so keeps its own ``isinstance`` branch.

That rewrite is a deliberate, documented *broadening*, pinned by
``TestBareUnsubscriptedAliasesNowAccepted``: bare unsubscripted ``typing.List`` /
``Dict`` / ``Callable`` were rejected by the old chain (they are
``_SpecialGenericAlias``, a sibling of ``_GenericAlias``, not a subclass) but
have a non-None origin, so they are now accepted. These aliases are deprecated
since Python 3.9 in favour of ``list`` / ``dict``; accepting an unparameterised
one as a place type is harmless. Nothing that was accepted before is now
rejected — the only behaviour change is this broadening.
"""

from typing import (
    Any,
    Annotated,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Union,
)
import collections.abc as abc

import pytest

from petritype.core.executable_graph_components import ListPlaceNode


# PEP 695 aliases (module scope: the `type` statement is not valid inside a call).
type IntAlias = int
type ListAlias[T] = list[T]


def _place_with_type(place_type):
    return ListPlaceNode(name="P", type=place_type)


class TestAcceptedPlaceTypes:
    """Every form usable as a type annotation must be a valid place type."""

    @pytest.mark.parametrize("place_type", [
        int, str, object, bytes,  # plain classes
        list[int], dict[str, int], tuple[int, ...],  # builtin subscripted generics
        List[int], Dict[str, int], Tuple[int],  # typing subscripted generics
        Callable[[int], str],  # parametrised callable
        abc.Sequence[int],  # subscripted ABC
        Literal[1, 2],  # special form with an origin
        Annotated[int, "meta"],  # annotated
    ])
    def test_accepted(self, place_type):
        assert _place_with_type(place_type).type is place_type

    def test_any_is_accepted(self):
        # Any is a class on modern Python, so it lands on the isinstance(type) branch.
        assert _place_with_type(Any).type is Any


class TestUnionSpellingsBothAccepted:
    """Both union spellings validate as place types.

    On Python 3.14 the two union object types were unified, so a single old
    isinstance(_UnionGenericAlias) check happened to catch both — but nothing
    pinned it. get_origin(...) is non-None for either spelling; these tests keep
    both wired so a future divergence is caught.
    """

    def test_typing_union(self):
        assert _place_with_type(Union[int, str]).type == Union[int, str]

    def test_pep604_union(self):
        assert _place_with_type(int | str).type == (int | str)

    def test_optional(self):
        assert _place_with_type(Optional[int]).type == Optional[int]


class TestPep695AliasesAccepted:
    """PEP 695 `type X = ...` aliases have no origin, so keep a dedicated branch."""

    def test_plain_alias(self):
        assert _place_with_type(IntAlias).type is IntAlias

    def test_generic_alias_subscripted(self):
        assert _place_with_type(ListAlias[int]).type == ListAlias[int]


class TestBareUnsubscriptedAliasesNowAccepted:
    """The deliberate broadening: bare typing.List / Dict / Callable.

    The old private-class chain rejected these (they are _SpecialGenericAlias,
    a sibling of _GenericAlias, not a subclass); get_origin gives them a
    non-None origin, so the new check accepts them. Pinned so the broadening is
    intentional and visible, not an accident to be silently undone.
    """

    @pytest.mark.parametrize("place_type", [List, Dict, Callable])
    def test_bare_alias_accepted(self, place_type):
        assert _place_with_type(place_type).type is place_type


class TestRejectedPlaceTypes:
    """Non-type values must still be rejected at construction."""

    @pytest.mark.parametrize("bad", [
        "int",  # a string, not a type
        42,  # an int instance
        3.14,  # a float instance
        [int, str],  # a list of types is not itself a type
        (int,),  # a tuple is not a type
        {"a": int},  # a dict is not a type
        object(),  # an instance
    ])
    def test_rejected(self, bad):
        with pytest.raises(ValueError):
            _place_with_type(bad)

    def test_lambda_rejected(self):
        with pytest.raises(ValueError):
            _place_with_type(lambda x: x)

    def test_none_rejected(self):
        # type=None takes the explicit NotImplementedError branch, not the ValueError one.
        with pytest.raises((ValueError, NotImplementedError)):
            _place_with_type(None)
