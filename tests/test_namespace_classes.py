"""A function in a class body that takes no ``self`` or ``cls`` is a ``@staticmethod``.

The library groups stateless functions in namespace classes
(``ExecutableGraphOperations.construct_graph(...)``, ``Runner.run(ctx)``). Python
calls such a function through its class without the decorator, but type checkers
do not: pyright reads an undecorated function in a class body as a method and
binds the class to its first parameter, so every call a user makes through the
class is an error in their editor. Hence the house rule (see
``.github/copilot-instructions.md``): every such function is decorated
``@staticmethod``, outermost, above any other decorator.

This walks the package's AST, so it imports nothing and sees every class,
nested ones and ones defined inside functions included.
"""

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "petritype"

# Decorators that make a function something other than a method and need no
# @staticmethod: classmethod and the property family bind the class or the
# instance themselves. (pydantic puts @classmethod under @model_validator, so
# a classmethod is accepted anywhere in the list, not only outermost.)
DESCRIPTORS = {"classmethod", "property", "cached_property", "setter", "getter", "deleter"}


def _decorator_name(decorator: ast.expr) -> str:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _functions_in_class_body(class_node: ast.ClassDef):
    """The functions a class body defines, those under an ``if`` or a ``try`` in
    it included; not those inside its functions, nor those of a nested class,
    which ``ast.walk`` reaches as a class of its own."""
    pending = list(class_node.body)
    while pending:
        node = pending.pop(0)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
        elif isinstance(node, (ast.If, ast.Try, ast.TryStar, ast.With)):
            for block in (node.body, getattr(node, "orelse", []), getattr(node, "finalbody", [])):
                pending.extend(block)
            for handler in getattr(node, "handlers", []):
                pending.extend(handler.body)


def _problem(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    positional = function.args.posonlyargs + function.args.args
    if positional and positional[0].arg in ("self", "cls"):
        return None
    names = [_decorator_name(d) for d in function.decorator_list]
    if names and names[0] == "staticmethod":
        return None
    if "staticmethod" in names:
        return "is a staticmethod, but not outermost"
    if DESCRIPTORS.intersection(names):
        return None
    return "takes no self or cls and is not a staticmethod"


def test_every_function_in_a_class_body_that_takes_no_self_or_cls_is_a_staticmethod():
    modules = sorted(path for path in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in path.parts)
    assert len(modules) > 10, "the glob found nothing to check"
    problems = []
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
            for function in _functions_in_class_body(class_node):
                problem = _problem(function)
                if problem:
                    where = path.relative_to(PACKAGE_ROOT.parent).as_posix()
                    problems.append(f"{where}:{function.lineno} {class_node.name}.{function.name} {problem}")
    assert not problems, (
        "decorate these @staticmethod, outermost (a pydantic validator or serializer "
        "instead takes cls or self, as pydantic expects):\n" + "\n".join(problems)
    )
