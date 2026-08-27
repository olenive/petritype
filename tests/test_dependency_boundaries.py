"""Every shipped module may only import the third-party packages its extra provides.

ci.yml import-smokes a base install, but that only catches *import-time*
violations: a lazy `import rustworkx` inside a function body ships happily and
fails at runtime for a base user. This walks the AST instead, so imports nested
in functions, methods and `if` blocks are caught too, and it runs locally.

The map below is the dependency contract. Widening an entry is a deliberate act:
it means that module now needs an extra it did not before.
"""

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "petritype"

# module path (relative to petritype/) -> third-party roots it may import
EXTRA_DEPENDENCIES = {
    "plotting": {"rustworkx", "matplotlib", "PIL"},
    "marimo_controls.py": {"marimo"},
}
BASE_DEPENDENCIES = {"pydantic"}


def _third_party_roots(path: Path) -> set[str]:
    """Every top-level module name imported anywhere in the file, including
    inside function bodies, that is neither stdlib nor petritype itself."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import, always internal
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return {
        r for r in roots
        if r != "petritype" and r not in sys.stdlib_module_names
    }


def _allowed_for(path: Path) -> set[str]:
    relative = path.relative_to(PACKAGE_ROOT)
    allowed = set(BASE_DEPENDENCIES)
    for key, extra in EXTRA_DEPENDENCIES.items():
        if relative.parts[0] == key or relative.as_posix() == key:
            allowed |= extra
    return allowed


ALL_MODULES = sorted(PACKAGE_ROOT.rglob("*.py"))


def test_the_package_has_modules_to_check():
    """Guards against the glob silently matching nothing."""
    assert len(ALL_MODULES) > 10


@pytest.mark.parametrize("path", ALL_MODULES, ids=lambda p: str(p.name))
def test_module_only_imports_what_its_extra_provides(path):
    allowed = _allowed_for(path)
    used = _third_party_roots(path)
    forbidden = used - allowed
    assert not forbidden, (
        f"{path.relative_to(PACKAGE_ROOT)} imports {sorted(forbidden)}, which the "
        f"base install does not provide. Allowed here: {sorted(allowed)}. "
        "Either move the module behind an extra or add the dependency to the map."
    )


def test_core_never_reaches_for_an_extra():
    """The headline invariant, stated once so a failure reads clearly."""
    offenders = {}
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT)
        if relative.parts[0] in ("plotting",) or relative.as_posix() == "marimo_controls.py":
            continue
        extra_only = _third_party_roots(path) - BASE_DEPENDENCIES
        if extra_only:
            offenders[relative.as_posix()] = sorted(extra_only)
    assert not offenders, f"base install would break for: {offenders}"
