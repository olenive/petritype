"""Tests that run the marimo example notebooks.

Two tiers, both gated behind the ``notebooks`` marker (deselected by default — run with
``pytest -m notebooks``). They require the project's ``examples`` extra to be installed
(``uv sync --extra examples``), which provides marimo, rustworkx and matplotlib.

* **Smoke** (every marimo notebook): run it headlessly via ``marimo export html`` and assert
  it executes without error and produces at least one rendered plot. This proves the notebook
  loads, builds, and draws its initial figure — but because the interactive notebooks only
  fire on button clicks, a headless export renders just the first frame.

* **Execution** (the finite live-firing notebooks): pull the notebook's own ``build_graph`` /
  ``fire_one`` / ``session`` out of ``app.run()`` and drive the real graph from its initial
  state to completion, asserting that at least one transition fires, the run terminates, and a
  valid PNG plot is recorded for the initial state and for every fired step.
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

# Pre-existing experiments that are not part of the Jupyter -> marimo migration.
_EXCLUDED_NAMES = {
    "interactive_traffic_monitor.py",
    "interactive_traffic_monitor_notebook.py",
}

# 8-byte PNG signature; every rendered figure should start with it.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _marimo_notebooks() -> list[Path]:
    """Every marimo notebook under examples/ (auto-discovers ones added later)."""
    notebooks = []
    for path in sorted(EXAMPLES_DIR.rglob("*.py")):
        if path.name in _EXCLUDED_NAMES:
            continue
        if "marimo.App(" in path.read_text(encoding="utf-8"):
            notebooks.append(path)
    return notebooks


# Finite notebooks built on the live-firing template (Step / Repeat Step / ...). These run a
# graph from an initial state to a terminal state, so they can be driven to completion.
# (parcel_distribution is perpetual and rooms is user-driven, so they are smoke-tested only.)
_FIRING_NOTEBOOKS = [
    EXAMPLES_DIR / "toy" / "distribution_function" / "01_coloured_balls.py",
    EXAMPLES_DIR / "toy" / "match_up_tokens" / "01_match_lengths.py",
    EXAMPLES_DIR / "toy" / "match_up_tokens" / "02_move_unmatched.py",
    EXAMPLES_DIR / "toy" / "one_to_many" / "01_distribute_by_types.py",
    EXAMPLES_DIR / "toy" / "one_to_many" / "01_fill_place.py",
    EXAMPLES_DIR / "special_cases" / "returning_empty_list.py",
]


def _notebook_id(path: Path) -> str:
    return str(path.relative_to(EXAMPLES_DIR))


def _load_notebook(path: Path):
    """Import a notebook by file path (names start with digits, so not importable by name)."""
    spec = importlib.util.spec_from_file_location(f"_nb_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.notebooks
@pytest.mark.parametrize("notebook", _marimo_notebooks(), ids=_notebook_id)
def test_notebook_runs_and_renders(notebook: Path, tmp_path: Path) -> None:
    """Headlessly execute the whole notebook and confirm it renders a plot without error."""
    out_html = tmp_path / "out.html"
    result = subprocess.run(
        [sys.executable, "-m", "marimo", "export", "html", str(notebook), "-o", str(out_html)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{notebook.name} failed to execute headlessly:\n{result.stdout}\n{result.stderr}"
    )
    assert out_html.exists(), f"{notebook.name}: no HTML output produced"
    assert "data:image/png" in out_html.read_text(encoding="utf-8"), (
        f"{notebook.name}: ran but rendered no plot"
    )


@pytest.mark.notebooks
@pytest.mark.parametrize("notebook", _FIRING_NOTEBOOKS, ids=_notebook_id)
def test_notebook_fires_to_completion(notebook: Path) -> None:
    """Drive the notebook's own graph from its first state to its last, checking plots."""
    module = _load_notebook(notebook)
    _outputs, defs = module.app.run()

    assert {"session", "fire_one"} <= set(defs), (
        f"{notebook.name}: expected live-firing template (session + fire_one)"
    )
    session = defs["session"]
    fire_one = defs["fire_one"]

    # The initial state must already have a rendered plot.
    assert session["history"], f"{notebook.name}: no initial frame recorded"
    assert session["history"][0][1].startswith(_PNG_MAGIC), (
        f"{notebook.name}: initial frame is not a valid PNG"
    )

    frames_before = len(session["history"])
    steps = 0
    for _ in range(1000):
        if not asyncio.run(fire_one(session)):
            break
        steps += 1
    else:
        pytest.fail(f"{notebook.name}: did not reach a terminal state within 1000 steps")

    assert steps >= 1, f"{notebook.name}: no transitions fired"
    # One plot recorded per successfully fired step.
    assert len(session["history"]) == frames_before + steps, (
        f"{notebook.name}: expected {steps} new frames, got {len(session['history']) - frames_before}"
    )
    assert session["history"][-1][1].startswith(_PNG_MAGIC), (
        f"{notebook.name}: final frame is not a valid PNG"
    )
