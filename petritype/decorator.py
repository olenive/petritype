"""Decorator for declaring Petri net factory functions.

Usage:
    from petritype import petri_net

    @petri_net(name="api-monitor", mode="24/7")
    def api_monitor_net() -> ExecutableGraph:
        return ExecutableGraphOperations.construct_graph([...])

The decorator attaches metadata to the function as `_petri_net_config`,
which can be discovered by scanning tools at build/deploy time.
"""

from functools import wraps
from typing import Any, Callable, Literal, Optional, TypeVar

ExecutionMode = Literal["manual", "24/7", "batch", "cron"]

F = TypeVar("F", bound=Callable)


def petri_net(
    name: str,
    *,
    mode: ExecutionMode = "manual",
    description: str = "",
    schedule: str | None = None,
    controls: Optional[Any] = None,
    place_styles: Optional[dict[str, Any]] = None,
) -> Callable[[F], F]:
    """Declare a function as a Petri net factory.

    Args:
        name: Unique identifier for this net. Used in URLs and commands.
        mode: Execution mode:
            - "manual": User triggers each step (default)
            - "24/7": Runs continuously, auto-restarts on failure
            - "batch": Runs once to completion, then stops
            - "cron": Runs on schedule (requires `schedule` parameter)
        description: Human-readable description shown in UI.
        schedule: Cron expression for mode="cron" (e.g., "0 9 * * *" for 9am daily).
        controls: The net's control-map, ``{name: ControlSpec}`` in display order, or a
            zero-argument callable returning one (called at every load and reset, so the
            controls of each generation are fresh). Kept off the net; the protocol runner
            serves it as ``topology.controls`` and runs it on ``press`` (PROTOCOL.md §6).
        place_styles: ``{place name: PlaceStyle}``, how a viewer draws those places
            (``PlaceStyle(fill="token")`` fills a place with its one token's colour).

    Returns:
        Decorator that attaches `_petri_net_config` to the function.

    Raises:
        ValueError: If mode="cron" but no schedule provided.

    Example:
        @petri_net(
            name="api-health-monitor",
            mode="24/7",
            description="Monitors API endpoints every 30 seconds",
        )
        def api_health_net() -> ExecutableGraph:
            return ExecutableGraphOperations.construct_graph([
                ListPlaceNode('Endpoints', str, ['https://api.example.com/health']),
                # ...
            ])
    """
    if mode == "cron" and not schedule:
        raise ValueError("mode='cron' requires a schedule parameter")

    if schedule and mode != "cron":
        raise ValueError("schedule parameter is only valid for mode='cron'")

    def decorator(fn: F) -> F:
        config = {
            "name": name,
            "mode": mode,
            "description": description,
            "schedule": schedule,
            "module": None,  # Filled in at discovery time
            "function": fn.__name__,
            "controls": controls,
            "place_styles": place_styles,
        }

        @wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)

        wrapper._petri_net_config = config  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator
