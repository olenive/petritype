"""Each node and edge class tells a type checker the constructor it has at runtime.

``PositionalArgsBaseModel.__init__`` takes a class's fields by position, in order,
those in ``_keyword_only`` by keyword alone; pydantic tells a type checker that a
model takes every field by keyword alone. So each class declares the constructor
it has under ``if TYPE_CHECKING:``, where nothing runs, and that declaration is
written beside the fields, so it can drift from them. This reads each class's
declaration from its source and compares it with the class's fields: their names
and order, which are keyword-only, their defaults and their annotations. It also
checks the declaration against the runtime: the class refuses one positional
argument more than the declaration takes by position.

``tests/typecheck/user_code.py``, checked by pyright (``just typecheck``), is
what shows that a checker reads these declarations.
"""

import ast
import inspect
import sys
from dataclasses import dataclass

import pytest
from pydantic.fields import FieldInfo

import petritype
from petritype.core.executable_graph_components import PositionalArgsBaseModel

# Where a declaration deliberately takes more than its field's annotation says,
# because a validator turns the argument into the field's type: (class, parameter)
# to the annotation the declaration gives.
WIDER_THAN_THE_FIELD = {
    # ``_keys_as_extractors`` turns a sequence of attribute names into the dict.
    ("ListPlaceNode", "keys"):
        "Optional[Union[dict[str, Callable[[Any], Any]], list[str], tuple[str, ...]]]",
    # pydantic turns a list of port names into the tuple, and refuses a bare string,
    # which a ``Sequence[str]`` would let a type checker pass.
    **{
        (edge, "reaches"): "Union[tuple[str, ...], list[str]]"
        for edge in (
            "ArgumentEdgeToTransition", "DrainEdge", "MutateEdge", "MutateWhereEdge", "ReadAllEdge", "ReadEdge",
            "ReadWhereEdge", "SnapshotEdge", "SurveyEdge", "TakeWhereEdge",
        )
    },
}


def _classes() -> list[type[PositionalArgsBaseModel]]:
    """Every class of the package built on ``PositionalArgsBaseModel``, however
    deep; a test's own subclass is not the package's to declare."""
    found, pending = [], list(PositionalArgsBaseModel.__subclasses__())
    while pending:
        cls = pending.pop(0)
        pending.extend(cls.__subclasses__())
        if cls.__module__.startswith("petritype.") and cls not in found:
            found.append(cls)
    return found


CLASSES = _classes()


def _class_node(cls: type) -> ast.ClassDef:
    tree = ast.parse(inspect.getsource(sys.modules[cls.__module__]))
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls.__name__)


def _declared_init(cls: type) -> ast.FunctionDef | None:
    """The ``__init__`` the class body declares under ``if TYPE_CHECKING:``."""
    for node in _class_node(cls).body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            for inner in node.body:
                if isinstance(inner, ast.FunctionDef) and inner.name == "__init__":
                    return inner
    return None


def _field_annotation(cls: type, name: str) -> str:
    """A field's annotation as written, in the nearest class of the MRO whose body
    declares it."""
    for owner in cls.__mro__:
        if not owner.__module__.startswith("petritype."):
            continue
        for node in _class_node(owner).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                return ast.unparse(node.annotation)
    raise AssertionError(f"no annotation found for {cls.__name__}.{name}")


@dataclass
class Parameter:
    name: str
    keyword_only: bool
    annotation: str
    default: ast.expr | None


def _parameters(init: ast.FunctionDef) -> list[Parameter]:
    arguments = init.args
    assert not arguments.posonlyargs and arguments.vararg is None and arguments.kwarg is None, (
        "a declared constructor takes its fields by name, with no *args, **kwargs or positional-only part"
    )
    assert arguments.args and arguments.args[0].arg == "self"
    positional = arguments.args[1:]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
    parameters = [
        Parameter(argument.arg, False, ast.unparse(argument.annotation) if argument.annotation else "", default)
        for argument, default in zip(positional, defaults)
    ]
    parameters += [
        Parameter(argument.arg, True, ast.unparse(argument.annotation) if argument.annotation else "", default)
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
    ]
    return parameters


def _default_problem(field: FieldInfo, default: ast.expr | None) -> str | None:
    if field.is_required():
        return None if default is None else "the field is required, so the parameter has no default"
    if default is None:
        return "the field has a default, so the parameter needs one"
    if field.default_factory is not None:
        is_ellipsis = isinstance(default, ast.Constant) and default.value is Ellipsis
        return None if is_ellipsis else "the field has a default factory, written ... in the declaration"
    try:
        value = ast.literal_eval(default)
    except ValueError:
        return f"the default {ast.unparse(default)} is not a literal"
    if type(value) is not type(field.default) or value != field.default:
        return f"the default is {ast.unparse(default)}, the field's is {field.default!r}"
    return None


def test_the_classes_are_found():
    """The search finds the public node and edge classes, so a test below that
    passes is not passing on nothing."""
    names = {cls.__name__ for cls in CLASSES}
    public = {
        name for name in petritype.__all__
        if isinstance(getattr(petritype, name), type) and issubclass(getattr(petritype, name), PositionalArgsBaseModel)
    }
    assert len(public) == 14
    assert public <= names


@pytest.mark.parametrize("cls", CLASSES, ids=lambda cls: cls.__name__)
def test_the_declared_constructor_is_the_fields(cls):
    init = _declared_init(cls)
    assert init is not None, (
        f"{cls.__name__} declares no __init__ under `if TYPE_CHECKING:`, so a type checker takes every "
        f"field by keyword alone; declare the one PositionalArgsBaseModel gives it"
    )
    parameters = _parameters(init)
    fields = cls.model_fields
    keyword_only = cls._keyword_only

    expected = [name for name in fields if name not in keyword_only] + [name for name in fields if name in keyword_only]
    assert [parameter.name for parameter in parameters] == expected, "the parameters are the fields, in order"
    for parameter in parameters:
        where = f"{cls.__name__}.__init__ parameter {parameter.name!r}"
        assert parameter.keyword_only == (parameter.name in keyword_only), (
            f"{where}: keyword-only exactly when the field is in _keyword_only"
        )
        problem = _default_problem(fields[parameter.name], parameter.default)
        assert problem is None, f"{where}: {problem}"
        annotation = WIDER_THAN_THE_FIELD.get((cls.__name__, parameter.name), _field_annotation(cls, parameter.name))
        assert parameter.annotation == annotation, f"{where}: annotated {parameter.annotation!r}, expected {annotation!r}"


@pytest.mark.parametrize("cls", CLASSES, ids=lambda cls: cls.__name__)
def test_the_runtime_takes_as_many_by_position_as_declared(cls):
    init = _declared_init(cls)
    assert init is not None
    by_position = sum(not parameter.keyword_only for parameter in _parameters(init))
    with pytest.raises(TypeError, match=rf"takes at most {by_position} positional arguments \({by_position + 1} given\)"):
        cls(*range(by_position + 1))
