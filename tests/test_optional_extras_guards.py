"""Guards for extras-gated modules must distinguish an absent extra from a
broken install of one.

Reporting "install petritype[marimo]" when marimo *is* installed but one of its
own dependencies is missing sends the user to fix something they already did.
The guards are presence checks precisely so the real import that follows raises
its own error untouched.

Both branches are driven by patching, so the tests behave identically whether or
not the optional packages are present in the running environment.
"""

import builtins
import importlib
import sys

import pytest

from petritype import _optional


class TestRequire:

    def test_passes_silently_when_the_module_is_present(self):
        _optional.require("pydantic", "core")  # always installed

    def test_names_the_extra_when_absent(self, monkeypatch):
        monkeypatch.setattr(_optional, "find_spec", lambda name: None)
        with pytest.raises(ModuleNotFoundError) as excinfo:
            _optional.require("rustworkx", "viz")
        message = str(excinfo.value)
        assert "rustworkx" in message
        assert "petritype[viz]" in message
        assert excinfo.value.name == "rustworkx"


def _reimport(module: str, monkeypatch, *, present: bool, import_error=None):
    """Import `module` fresh with its optional dependency present/absent."""
    for name in list(sys.modules):
        if name.startswith(("petritype.marimo_controls", "petritype.plotting", "marimo")):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(_optional, "find_spec", lambda name: object() if present else None)
    if import_error is not None:
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.split(".")[0] == import_error.name_root:
                raise import_error
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
    return importlib.import_module(module)


class TestMarimoControlsGuard:

    def test_absent_marimo_points_at_the_extra(self, monkeypatch):
        with pytest.raises(ModuleNotFoundError) as excinfo:
            _reimport("petritype.marimo_controls", monkeypatch, present=False)
        assert "petritype[marimo]" in str(excinfo.value)

    def test_broken_marimo_install_propagates_untouched(self, monkeypatch):
        inner = ModuleNotFoundError("No module named 'narwhals'", name="narwhals")
        inner.name_root = "marimo"  # what the fake importer intercepts
        with pytest.raises(ModuleNotFoundError) as excinfo:
            _reimport("petritype.marimo_controls", monkeypatch, present=True, import_error=inner)
        assert excinfo.value is inner
        assert "petritype[marimo]" not in str(excinfo.value)


class TestPlottingGuards:

    @pytest.mark.parametrize("module", [
        "petritype.plotting.rustworkx_graph",
        "petritype.plotting.rustworkx_to_graphviz",
        "petritype.plotting.simple_graphviz",
    ])
    def test_absent_viz_dependency_points_at_the_extra(self, module, monkeypatch):
        with pytest.raises(ModuleNotFoundError) as excinfo:
            _reimport(module, monkeypatch, present=False)
        assert "petritype[viz]" in str(excinfo.value)
