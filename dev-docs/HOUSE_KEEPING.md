# House Keeping

Open maintenance tasks, written so an agent (or contributor) with no prior context can
pick any of them up independently. Line numbers are as of 2026-07-17 (commit
`f5a2b56`) — re-locate by symbol name if they have drifted.

## How to verify any change

```bash
uv run pytest tests/ -q                                          # unit tier (fast)
uv run --extra examples pytest tests/test_notebooks.py -m notebooks -q   # notebook tier (~30s)
```

Both must pass. The notebook execution tests pin *exact* per-notebook step counts and,
for `returning_empty_list.py`, the terminal marking — if a count changes, understand
why before updating the number (fewer steps = a stall, more = a new cycle).

---

## 1. Replace private `typing` internals (Python 3.17 breakage)

**Status:** done (2026-07-17). It removed the `'_UnionGenericAlias' is deprecated and
slated for removal in Python 3.17` warning, which would otherwise have become a hard
`ImportError` on Python 3.17.

`ListPlaceNode.validate_type_field` in
`petritype/core/executable_graph_components.py` used to accept a place type via four
`isinstance` checks, two of them against typing's private `_GenericAlias` and
`_UnionGenericAlias`. It now uses the public introspection API:

```python
isinstance(t, type)              # plain classes
or get_origin(t) is not None     # every subscripted generic + union, both spellings
or isinstance(t, TypeAliasType)  # PEP 695 aliases (a bare alias has no origin)
```

The unused `_GenericAlias`, `_UnionGenericAlias`, and `types.GenericAlias` imports were
dropped; `TypeAliasType` and `types.UnionType` are still needed (the latter by the
return-type check further down the file).

This was a deliberate, documented **broadening**, not a byte-for-byte swap: bare
unsubscripted `typing.List` / `Dict` / `Callable` were rejected by the old chain (they
are `_SpecialGenericAlias`, a sibling of `_GenericAlias`, not a subclass) but have a
non-None origin, so they are now accepted. These aliases are deprecated since Python
3.9 in favour of `list` / `dict`, so accepting an unparameterised one is harmless.
Nothing that was accepted before is now rejected.

Tests live in `tests/test_place_type_validation.py` (accepted forms including both union
spellings, PEP 695 aliases, the bare-alias broadening in its own class, and rejected
non-type values). Verified: both tiers pass, and the typing warning is gone (the only
remaining `DeprecationWarning`s are the intentional `activation_function` ones from
item 3, so a blanket `-W error::DeprecationWarning` still trips those five tests).

## 2. Pre-flight sweep is lenient about list tokens resting in scalar places

**Status:** open. **Effort:** small-medium (the design decision is the work).
**Priority:** low — not reachable through any normal execution path.

`execute_graph` starts with a pre-flight sweep,
`ExecutableGraphCheck.ensure_all_token_types_match_place_types` (line 657), which
validates every token already sitting in every place via
`ensure_token_type_matches_place_type` (line 640). That function classifies the value
with `ExecutableGraphCheck.value_disposition_for_place` (line 609) — the single point
of truth for what a value means *at a destination*: a list arriving at a scalar-typed
place is a "batch" to be unpacked element-wise.

The leniency: for a token *already resting in* a place, "batch" is a category error —
whatever sits in a place is by definition one token. So a hand-planted `[1, 2]` in an
`int`-typed place passes the sweep (validated element-wise as a would-be batch)
instead of being flagged as a mistyped token. This state cannot arise normally:
`ListPlaceNode`'s own construction validator rejects it, and deposits unpack batches
before they land — it takes post-construction mutation like
`place.tokens.append([1, 2])` to get there.

**If picked up:** decide whether the sweep should treat every *resident* value as
disposition `"token"` (i.e. stop sharing the deposit-side classification for this one
call site), implement, and add a regression test that plants a list in a scalar place
after construction and asserts the sweep raises `TypeError`. Read the docstring of
`value_disposition_for_place` first — it lists the sites that derive from it, and this
change deliberately carves the sweep out of that list, so update the docstring too.

## 3. Remove `activation_function` (end of deprecation window)

**Status:** scheduled, **blocked on maintainer sign-off** — do not start unprompted.
**Effort:** small.

`FunctionTransitionNode.activation_function` was deprecated in `f5a2b56` in favour of
`guard` (engine-enforced enabling, `(graph) -> bool`) and `priority` (default-selector
ranking, `(graph) -> float`). During the window it stays inert to the engine but
visible to custom selectors, and constructing a node with it emits
`DeprecationWarning`.

When the maintainer decides the window has passed:

- Delete the field and the `_warn_if_activation_function` model validator from
  `FunctionTransitionNode` (`petritype/core/executable_graph_components.py`).
- Update `tests/test_transition_selection.py`: the `TestSelectorWithActivationFunction`
  cases pin the legacy selector-visible behaviour and should be dropped or rewritten
  against `guard`/`priority`. Update
  `tests/test_guards_and_priorities.py::TestActivationFunctionDeprecation` (the
  warning it asserts will no longer exist — replace with a test that the field is
  rejected as unknown).
- Shrink the "Deprecated: activation_function" section of
  `dev-docs/TRANSITION_SELECTION.md` to a changelog note.
- `grep -rn activation_function` across the repo; as of writing, examples are already
  migrated (`01_match_lengths.py` and `parcel_distribution.py` use `guard`).

## 4. Backlog (not defects)

- **Port the two remaining Jupyter notebooks to marimo:**
  `examples/illustrations/hypothetical_web_scrape.ipynb` and
  `examples/time_series_stats/stats_executable_graph.ipynb`. Follow the live-firing
  template used by `examples/toy/` (see `tests/test_notebooks.py`'s module docstring
  for the smoke/execution tier contract; `dev-docs/HEAVY_NOTEBOOKS.md` may apply).
  Marimo notebooks under `examples/` are auto-discovered by the smoke tier; add
  finite ones to `_FIRING_NOTEBOOKS` with their measured step count.
- **Decide the fate of the traffic-monitor experiments:**
  `interactive_traffic_monitor.py` / `interactive_traffic_monitor_notebook.py` are
  excluded from test discovery (`_EXCLUDED_NAMES` in `tests/test_notebooks.py`) as
  pre-migration experiments — port, rewrite on the `petritype.runtime` Runner, or
  delete.
- **README release sections:** the Installation and Examples sections are commented
  out at the bottom of `README.md`, pending a PyPI release.
