"""The import guard in petritype.marimo_controls must distinguish "marimo is not
installed" from "marimo is installed but broken".

Both branches are driven by replacing ``builtins.__import__`` so the tests behave
identically whether or not marimo is present in the running environment.
"""

import builtins
import importlib
import sys

import pytest

MODULE = "petritype.marimo_controls"


def _import_with_marimo_raising(monkeypatch, error: ModuleNotFoundError):
    """Import the module fresh, with ``import marimo`` raising ``error``."""
    for name in list(sys.modules):
        if name == MODULE or name == "marimo" or name.startswith("marimo."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "marimo" or name.startswith("marimo."):
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    return importlib.import_module(MODULE)


class TestMarimoImportGuard:

    def test_absent_marimo_points_at_the_extra(self, monkeypatch):
        absent = ModuleNotFoundError("No module named 'marimo'", name="marimo")
        with pytest.raises(ModuleNotFoundError) as excinfo:
            _import_with_marimo_raising(monkeypatch, absent)

        message = str(excinfo.value)
        assert "petritype[marimo]" in message
        assert excinfo.value.__cause__ is absent

    def test_broken_marimo_install_propagates_untouched(self, monkeypatch):
        """A missing dependency *of* marimo must not be reported as missing marimo:
        the user would already have installed the extra."""
        inner = ModuleNotFoundError("No module named 'narwhals'", name="narwhals")
        with pytest.raises(ModuleNotFoundError) as excinfo:
            _import_with_marimo_raising(monkeypatch, inner)

        assert excinfo.value is inner
        assert excinfo.value.name == "narwhals"
        assert "petritype[marimo]" not in str(excinfo.value)

    def test_module_imports_normally_when_marimo_is_available(self):
        """Guard must be inert on a working install."""
        pytest.importorskip("marimo")
        module = importlib.import_module(MODULE)
        for fn in ("build_controls", "controls_row", "drain_controls"):
            assert hasattr(module, fn)
