"""Render a control-map into marimo widgets wired to a ``RunContext``'s inbox.

A *control-map* is ``{name: ControlSpec}`` (authored by the app, kept off the net). This turns
it into a single reactive ``mo.ui.dictionary`` of widgets, lays them out, and drains interacted
controls into the runtime's inbox via :func:`petritype.runtime.apply_control`. The widgets only
ever *produce commands* — they never touch the graph.

Marimo-specific — import only where marimo is available (the ``marimo`` extra).
"""

try:
    import marimo as mo
except ModuleNotFoundError as exc:  # pragma: no cover - depends on install extras
    raise ModuleNotFoundError(
        "petritype.marimo_controls requires marimo. "
        "Install it with: pip install 'petritype[marimo]'"
    ) from exc

from petritype.runtime import ControlSpec, RunContext, apply_control

_UNSET = object()


def _widget(spec: ControlSpec):
    """Build one marimo widget for a control, by ``kind``."""
    cfg = spec.config
    if spec.kind == "button":
        return mo.ui.run_button(label=spec.label, kind=cfg.get("kind", "neutral"))
    if spec.kind == "toggle":
        return mo.ui.switch(value=cfg.get("value", False), label=spec.label)
    if spec.kind == "slider":
        start = cfg.get("start", 0)
        return mo.ui.slider(
            start, cfg.get("stop", 10), step=cfg.get("step", 1),
            value=cfg.get("value", start), label=spec.label,
        )
    if spec.kind == "select":
        return mo.ui.dropdown(cfg["options"], value=cfg.get("value"), label=spec.label)
    raise ValueError(f"unknown control kind: {spec.kind!r}")


def build_controls(control_map: dict[str, ControlSpec]):
    """One reactive ``mo.ui.dictionary`` of widgets — assign it to a cell variable so marimo
    tracks changes to any of its controls."""
    return mo.ui.dictionary({name: _widget(spec) for name, spec in control_map.items()})


def controls_row(controls, control_map: dict[str, ControlSpec]):
    """An ``hstack`` of the controls in declaration order, for display."""
    return mo.hstack([controls[name] for name in control_map], justify="start")


def drain_controls(
    control_map: dict[str, ControlSpec], controls, ctx: RunContext, last_values: dict
) -> None:
    """Enqueue commands for interacted controls. **Buttons** act when clicked (value truthy);
    **stateful** controls (toggle / slider / select) act when their value *changes* (tracked in
    ``last_values``, which the caller should persist across re-runs)."""
    for name, spec in control_map.items():
        value = controls[name].value
        if spec.kind == "button":
            if value:
                apply_control(ctx, spec, True)
        elif last_values.get(name, _UNSET) != value:
            last_values[name] = value
            apply_control(ctx, spec, value)
