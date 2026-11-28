"""What ``import petritype`` gives: every name ``petritype.__all__`` lists, every
library name the README uses, and the library's errors, each a ``PetritypeError``
that keeps its standard-library base."""

import importlib
import re
from pathlib import Path

import petritype
from petritype import (
    CancellationTimeoutError,
    ClosedNetError,
    HeldByBodyError,
    LaunchNotificationError,
    PetritypeError,
    SubnetHeldError,
    TooManyFiringsError,
    TransitionFailedError,
)
from petritype import decorator, runtime
from petritype.core import executable_graph_components


def test_top_level_public_api_exports():
    for name in petritype.__all__:
        assert hasattr(petritype, name), f"petritype.__all__ lists missing symbol {name!r}"
    assert petritype.ExecutableGraph is executable_graph_components.ExecutableGraph
    assert petritype.Runner is runtime.Runner
    assert petritype.petri_net is decorator.petri_net


def test_petritype_error_hierarchy_preserves_stdlib_bases():
    assert issubclass(ClosedNetError, PetritypeError)
    assert issubclass(ClosedNetError, ValueError)
    assert issubclass(HeldByBodyError, PetritypeError)
    assert issubclass(HeldByBodyError, ValueError)
    assert issubclass(TransitionFailedError, PetritypeError)
    assert issubclass(TransitionFailedError, RuntimeError)
    assert issubclass(LaunchNotificationError, PetritypeError)
    assert issubclass(LaunchNotificationError, RuntimeError)
    assert issubclass(SubnetHeldError, PetritypeError)
    assert issubclass(SubnetHeldError, RuntimeError)
    assert issubclass(TooManyFiringsError, PetritypeError)
    assert issubclass(TooManyFiringsError, RuntimeError)
    assert issubclass(CancellationTimeoutError, PetritypeError)
    assert issubclass(CancellationTimeoutError, RuntimeError)


# The modules a base install can import, whose public names the README may use. The
# plotting modules and the marimo controls need an extra, and the protocol runner is
# a process, not an API: their names stay where they are.
LIBRARY_MODULES = (
    "petritype.runtime",
    "petritype.decorator",
    "petritype.core.executable_graph_components",
    "petritype.core.keys",
    "petritype.core.events",
    "petritype.core.serialisation",
    "petritype.core.type_comparisons",
)
README = Path(__file__).resolve().parent.parent / "README.md"


def library_names() -> dict[str, object]:
    """Every public name one of the library's modules defines, with its object."""
    names: dict[str, object] = {}
    for module_name in LIBRARY_MODULES:
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if not name.startswith("_") and getattr(value, "__module__", None) == module_name:
                names.setdefault(name, value)
    return names


def names_the_readme_uses() -> set[str]:
    """The identifiers in the README's Python blocks and in its backticks (the
    first part of a dotted one, ``NestedNets.put`` giving ``NestedNets``)."""
    text = README.read_text()
    in_code = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", "\n".join(re.findall(r"```python\n(.*?)```", text, flags=re.S)))
    in_ticks = [ticked.split(".")[0] for ticked in re.findall(r"`([A-Za-z_][A-Za-z0-9_.]*)", text)]
    return set(in_code) | set(in_ticks)


def test_every_library_name_the_readme_uses_comes_from_the_top_level():
    library = library_names()
    used = sorted(name for name in names_the_readme_uses() if name in library)
    assert len(used) > 40, "the README's names were not found: has its layout changed?"
    missing = [name for name in used if name not in petritype.__all__]
    assert not missing, f"the README uses these, which `from petritype import ...` does not give: {missing}"
    for name in used:
        assert getattr(petritype, name) is library[name], f"petritype.{name} is not the library's own {name}"


def test_the_readme_imports_library_names_from_the_top_level():
    """A README example imports from ``petritype`` itself; only the plotting modules,
    which need the ``viz`` extra, are imported by their own path."""
    deep = [
        line for line in re.findall(r"^from (petritype\.[\w.]+) import", README.read_text(), flags=re.M)
        if not line.startswith("petritype.plotting")
    ]
    assert not deep, f"README examples import from {sorted(set(deep))}; import those names from `petritype`"
