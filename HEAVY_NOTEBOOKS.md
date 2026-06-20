# Migrating the "heavy" example notebooks to marimo

> Starting point for anyone picking up the Jupyter → marimo migration of the
> remaining complex example notebooks. Read this before touching them.

## 1. Context

We are replacing every Jupyter notebook under `examples/` with an equivalent
[marimo](https://marimo.io) notebook (`.py`). Work happens on the **`marimo`** branch.
marimo notebooks are plain reactive Python files: each cell is a function, cells form a
dataflow DAG (file order does **not** matter), and there is no hidden kernel state.

**Status when this doc was written: 8 / 14 notebooks done** (all the "toy" / finite ones,
plus the two special cases `parcel_distribution` and `state_machines/01_rooms`). The `.ipynb`
originals are kept alongside the new `.py` files until everything is validated.

**The 6 remaining notebooks (this doc):**

| Notebook | Type | Difficulty |
|---|---|---|
| `examples/ml_model/fine_tune.ipynb` | AST graph, **static** (no animation) | Medium |
| `examples/caching/relationship_and_execution_graphs.ipynb` | AST + animation | Hard |
| `examples/caching/execution_subgraph.ipynb` | AST + nested subgraph + animation | **Hardest** |
| `examples/time_series_stats/stats_executable_graph.ipynb` | AST + animation + numpy | Hard |
| `examples/time_series_stats/illustrations_of_distributions.ipynb` | Pure matplotlib (no Petri net) | Medium |
| `examples/illustrations/hypothetical_web_scrape.ipynb` | Animation + artifact (PNG/GIF) export | Medium |

## 2. Environment & how to run

Dependencies are uv-managed. The viz/notebook stack lives in an **`examples` optional
extra** in `pyproject.toml` (`marimo`, `rustworkx`, `matplotlib`). `petritype` itself is an
installed (editable) package, so `import petritype` works from any directory.

```bash
# edit a notebook interactively (this is the only way to test the interactivity)
uv run --extra examples marimo edit examples/<path>.py

# headless run — exercises every cell; our CI-style smoke test
uv run --extra examples marimo export html examples/<path>.py -o /tmp/x.html
```

Always use `uv add` to change dependencies (never ad-hoc `uv pip install` — it won't update
the lockfile, and the next `uv sync` will wipe it). Graphviz's `dot` binary must be on PATH
(`brew install graphviz`).

## 3. The conversion template (already established)

The finite notebooks use a **live-firing** template. Don't reinvent it — copy it from the
canonical reference:

> **`examples/toy/distribution_function/01_coloured_balls.py`** — read this first.

Its model, in one paragraph: build the graph once into a persistent `session` dict
(`{"graph", "pydigraph", "history", "cursor"}`); a single async **engine cell** reads the
control buttons and mutates `session` in place; **Step** fires one transition
(`execute_graph(..., max_transitions=1)`), **Repeat Step** auto-fires until nothing fires,
**◀ Back / Forward ▶** move the history cursor, **↺ Reset** rebuilds. Each successful firing
snapshots the graph (`graphviz_draw` with `RustworkxToGraphviz.activation_coloured_attr_functions`)
into `history` as PNG bytes. The engine renders `mo.vstack([controls, figure])` so the
buttons sit directly above the plot. Figures are shown at **half native width**
(`mo.image(src=png, width=pil.width // 2)`) — full size barely fit the screen.

### Hard-won marimo lessons baked into that template

- **No `mo.state` feedback loops.** An earlier version used a `mo.state` tick + a
  range-growing `mo.ui.slider`; the firing cell read the slider and bumped the tick, so
  firing re-created the slider, which re-ran the firing cell — and **`run_button.value`
  stays `True` on cascade re-runs**, so it fired ~350 times. Lesson: the cell that fires must
  not depend (even transitively) on anything the firing changes. We dropped the slider for
  Back/Forward run-buttons.
- **A `run_button`'s `.value` is `True` for the whole reaction**, not just the first run.
- **One global = one cell.** A name may be assigned in only one cell. Loop/throwaway vars
  must be `_`-prefixed (marimo treats `_name` as cell-local, not a global).
- **You can't read a UI element's `.value` in the cell that creates it.** Define controls in
  one cell, read `.value` in another. (Callbacks via `on_change` are fine in the defining
  cell — that's how `rooms` works.)
- **Display:** make the value the last cell expression, or `mo.output.replace(x)` /
  `mo.output.append(x)` for imperative/animated updates. No `IPython.display`.

## 4. The heavy-notebook playbook (cross-cutting)

These five techniques cover almost everything that makes the heavy notebooks harder than the
toys. Apply them as you convert.

### 4a. Anchor file paths to the repo root
marimo puts the **notebook's own directory** on `sys.path`/CWD, **not** the repo root. The
AST notebooks read source files by repo-root-relative paths like
`os.path.join("examples", "caching", "hypothetical_caching.py")` and
`os.path.join("petritype", "core", "data_structures.py")`. These break under marimo. Add a
repo-root helper and prefix every file read:

```python
@app.cell
def _(mo):
    from pathlib import Path

    def _find_repo_root() -> Path:
        here = Path(mo.notebook_dir())
        for parent in [here, *here.parents]:
            if (parent / "pyproject.toml").exists():
                return parent
        raise RuntimeError("repo root (pyproject.toml) not found")

    REPO_ROOT = _find_repo_root()
    return (REPO_ROOT,)

# then: ParseModule.from_file(path_to_file=str(REPO_ROOT / "examples" / "caching" / "hypothetical_caching.py"), ...)
```

### 4b. Expand `import *`
marimo's static analysis can't resolve wildcard imports. Every heavy notebook has at least
one `from petritype.core.executable_graph_components import *` (and the AST ones a second
star import). Replace with the explicit names actually used
(`ListPlaceNode, FunctionTransitionNode, ArgumentEdgeToTransition, ReturnedEdgeFromTransition,
ExecutableGraphOperations, ExecutableGraph`, …).

### 4c. Import sibling helpers by bare name
Each AST/stats notebook imports a co-located helper module via its full package path, e.g.
`from examples.caching.hypothetical_caching import *`. `examples` is **intentionally not a
shipped package**, but marimo has the notebook's dir on `sys.path`, so import it by bare
module name instead:

```python
# examples/caching/*.ipynb         -> from hypothetical_caching import DBKey, DBKeyValuePair, DBOperations, CacheOperations
# examples/ml_model/fine_tune      -> from hypothetical_training_steps import AvailableData, TrainingData, VisionModel, ...
# examples/time_series_stats/*     -> from hypothetical_time_series import SimulateData, SeriesStatistics, ...
```
Helper modules confirmed present: `examples/caching/hypothetical_caching.py`,
`examples/ml_model/hypothetical_training_steps.py`,
`examples/time_series_stats/hypothetical_time_series.py`.

### 4d. Delete debugger breakpoints
`execution_subgraph` and `stats_executable_graph` contain stray `import pdb; pdb.set_trace()`
lines. These will hang marimo. Remove them.

### 4e. matplotlib display & cross-cell variable reuse
- Replace `plt.show()` with returning the figure (make `plt.gcf()` the cell's last
  expression, or build with `fig, ax = plt.subplots(...)` and end the cell on `fig`). marimo
  renders matplotlib figures natively.
- Jupyter notebooks freely **redefine** the same variable across cells (`rng`, `values`,
  `measurement_times`, …). marimo forbids this. Either merge those cells, or `_`-prefix the
  intermediate names so each is cell-local. This is the main pain in
  `illustrations_of_distributions`.

## 5. Per-notebook breakdown

### `ml_model/fine_tune.ipynb` — ✅ DONE (was the easiest *mechanically*, but the example was broken)
- **What it does:** AST-extracts types/functions from `hypothetical_training_steps.py`, builds
  a *relationship* graph (`RustworkxToGraphviz.digraph`), then builds a large fine-tuning
  *executable* graph and renders it **statically** (one `graphviz_draw`, with custom node-label
  functions). **No animation loop.**
- **Complications:** 2× `import *`; `from examples.ml_model.hypothetical_training_steps import *`;
  one repo-root file read in `ParseModule.from_file`.
- **Approach:** Apply 4a–4c. No live-firing engine needed — it's static displays, so each
  graphviz figure is just a cell's last expression (wrap in the half-width `mo.image` helper).
  Good first conversion to validate the playbook.

### `caching/relationship_and_execution_graphs.ipynb` — Hard
- **What it does:** Builds the relationship graph for the caching domain **and** the cache
  executable graph (DBKey → cache check → DB retrieval → cache write), then animates execution.
- **Complications:** `import *`; `from examples.caching.hypothetical_caching import ...` (this
  one is already partly explicit); 3× repo-root file reads; one animation loop
  (`clear_output`/`display`).
- **Approach:** 4a–4c, then split into a "relationship graph" section (static figures) and an
  "executable graph" section using the live-firing template.

### `caching/execution_subgraph.ipynb` — **Hardest**
- **What it does:** Everything in the previous notebook **plus** wraps a whole executable graph
  inside a transition function (a *sub-graph as a transition*) and runs an outer graph over it.
  Two separate animation loops; directly mutates graph internals
  (`graph.places += (...)`, `graph.return_edges += (...)`); reads `petritype/core/*.py` source to
  build descriptive strings; uses `allow_token_copying=True`.
- **Complications:** 2× `import *`; `from examples.caching.hypothetical_caching import *`; 3×
  repo-root file reads (incl. `petritype/core/data_structures.py`,
  `petritype/core/executable_graph_components.py`); **`import pdb; pdb.set_trace()` to delete**;
  vestigial `*_description` string-building cells that aren't used downstream (candidate to drop
  — see open questions); two `await` animation loops.
- **Approach:** Budget the most time here. Do 4a–4d. Consider splitting the outer-graph
  animation and the inner sub-graph animation into clearly separated sections, each with its own
  live-firing engine, or keep the inner run as a static "what one firing of the sub-graph does"
  illustration.

### `time_series_stats/stats_executable_graph.ipynb` — Hard
- **What it does:** AST relationship graph + a time-series-statistics executable graph
  (branches that copy parameters, generate intervals, compute stats), then animates it. Uses
  numpy and the `hypothetical_time_series` helper.
- **Complications:** 2× `import *`; `from examples.time_series_stats.hypothetical_time_series import (...)`;
  repo-root file read; **`import pdb; pdb.set_trace()` to delete**; numpy; animation loop.
- **Approach:** 4a–4d + live-firing template.

### `time_series_stats/illustrations_of_distributions.ipynb` — Medium (different kind of hard)
- **What it does:** **No Petri net.** Generates a noisy sine-wave time series and plots it,
  rolling-window weights, and an exponential moving average — pure matplotlib (≈30 `plt.` calls
  across 4 plots).
- **Complications:** `from examples.time_series_stats.hypothetical_time_series import ...`
  (SimulateData, SeriesStatistics); `plt.show()` ×4; **heavy cross-cell variable redefinition**
  (`rng`, `values`, `measurement_times`, `start_time`/`end_time` redefined in several cells) —
  the main marimo violation here.
- **Approach:** 4c + 4e. Compute the shared series **once** in a single upstream cell and reuse
  it (don't redefine); give each plot cell its own `_`-prefixed locals; end each plot cell on
  the figure object instead of `plt.show()`.

### `illustrations/hypothetical_web_scrape.ipynb` — Medium
- **What it does:** A *simulated* (no network) scrape pipeline: Scrape → Classify →
  Special-cases → Successes/Failures, animated. It **also writes artifacts** — saves every
  animation frame to `images/docs/illustrations/readme_example/step_NNN.png` and stitches them
  into `animation.gif` (this is the GIF shown in the README).
- **Complications:** standard animation (`clear_output`/`display`); path-anchored **file writes**
  (`os.makedirs` + `open(... , "wb")` relative to repo root); a Pillow GIF-build step; a big
  defensive `hasattr` ladder for diagram types (can be simplified — `graphviz_draw` always
  returns a PIL image).
- **Approach:** Live-firing template for the pipeline. Anchor the image output dir to repo root
  (4a). Keep the PNG/GIF export, but **gate it behind a "Generate GIF" run-button** so it
  doesn't write files on every reactive run. Simplify the save logic to the PIL path only.

## 6. Other known gotchas

- **`construct_graph` type checking is exact-equality + `Any` only.** Its comparator
  (`CompareTypes.between_annotations`) does **not** understand subtypes or union membership.
  Union/tuple *return* types pass the return-edge check (lenient), but a union *argument*
  type **fails** the argument-edge check (e.g. a place typed `EvaluationData` will not match a
  parameter typed `EvaluationData | TestData`). For a genuinely polymorphic argument, type it
  `Any`. Argument-*name* mismatches and routing ambiguity are execution-time only, so they
  don't block construction for a render-only notebook. **Several example graphs are
  type-inconsistent with the current helper signatures** — `fine_tune` had to be repaired (its
  `.ipynb` was never run) by editing helper signatures into a coherent set; expect the other
  AST notebooks may need similar fixes to construct.
- **Noisy debug prints:** `ExecutableGraphOperations.execute_graph` prints lines like
  `argument_origin is list` / `token to pass as single` **unconditionally** — `verbose=False`
  does not silence them. They'll clutter marimo cell output exactly as in Jupyter. Out of scope
  to fix here, but worth a separate library change.
- **In-place graph mutation:** executable graphs are mutated by execution. Always build a fresh
  graph in a self-contained `build_graph()` so reactive re-runs start clean; never share node /
  token objects between two graph instances.
- **`marimo convert nb.ipynb -o nb.py`** gives a starting scaffold but is broken as-is (it keeps
  `display`/`time.sleep` and drops `clear_output`); only use it for the cell boilerplate, then
  hand-fix.

## 7. Verification checklist (per notebook)

1. `uv run --extra examples marimo export html <nb> -o /tmp/x.html` → no `error` / `exception`
   / `traceback` / `not defined` / `cycle`; output contains a `data:image/png`.
2. `uv run --extra examples marimo edit <nb>` and actually click through: Step fires once,
   Repeat Step animates then halts, Back/Forward scrub, Reset returns to the initial state.
3. Confirm no files are written on plain load (web_scrape: artifact export only on button click).
4. Keep the `.ipynb` original until the whole set is validated.

## 7a. Automated notebook tests

There is a gated pytest suite in `tests/test_notebooks.py` (off by default):

```bash
# run ONLY the notebook suite — NOTE: both extras are required
uv run --extra examples --extra dev pytest -m notebooks
```

`pytest` is in the `dev` extra and marimo/rustworkx/matplotlib are in `examples`; with only
`--extra examples`, uv runs pytest from an ephemeral env without the project deps and you get
`ModuleNotFoundError: No module named 'marimo'`. A plain `pytest` deselects this suite
(`addopts = "-m 'not notebooks'"` in `pyproject.toml`).

- **Smoke tier** — auto-discovers every marimo notebook under `examples/` (so a newly converted
  heavy notebook joins automatically) and asserts `marimo export html` exits 0 and renders a
  `data:image/png`. The two `interactive_traffic_monitor*` experiments are excluded.
- **Execution tier** — an explicit list (`_FIRING_NOTEBOOKS`) of the finite live-firing
  notebooks; pulls each notebook's own `session`/`fire_one` from `app.run()` and drives the
  graph to a terminal state, asserting ≥1 fire, termination, and a valid PNG per step. **When
  you convert a heavy notebook that uses the live-firing template, add it to
  `_FIRING_NOTEBOOKS`.** Static / perpetual / user-driven notebooks (fine_tune, parcel, rooms)
  stay smoke-only.

## 8. Open questions / decisions

- **`execution_subgraph` description-string cells** (`data_structures_description`,
  `executable_graph_description`) build big strings from source code that don't appear to feed
  the graph or the display — keep faithfully, or drop as dead weight?
- **web_scrape GIF export** — keep it in the notebook (behind a button) so the README asset can
  be regenerated, or move artifact generation to a separate script?
- **Button-layout consistency** — the 6 finite notebooks + parcel + rooms now render controls
  directly above the figure; keep that convention for the heavy ones too (yes, recommended).
