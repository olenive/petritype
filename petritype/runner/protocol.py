"""The wire shapes of the petritype net protocol, version 1 (PROTOCOL.md).

Pydantic models for every object the protocol defines, serialised to plain
dicts ready for ``json.dumps``. Nothing here knows about ``ExecutableGraph``:
``petritype.runner.owner`` builds these from a net, and anything else (a test,
a host written in Python) can read them.

Optional fields are omitted, never ``null`` (PROTOCOL.md §2). ``Wire`` does it
once for every model: a ``None`` value is dropped unless the field is one the
protocol sends as ``null`` (``nullable_fields``) *and* it was given explicitly.
The second condition is what lets ``ParamDescription.default`` be the JSON
value ``null`` when a factory's default is ``None``, and be absent when it has
no default at all.

Token ids are strings on the wire; the runner converts at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, model_serializer


PROTOCOL_VERSION = 1
RUNTIME = "python"


class ErrorCode(StrEnum):
    """The closed set of ``error.code`` values (PROTOCOL.md §3)."""

    UNKNOWN_METHOD = "unknown_method"
    INVALID_PARAMS = "invalid_params"
    NOT_LOADED = "not_loaded"
    UNKNOWN_NET = "unknown_net"
    LOAD_FAILED = "load_failed"
    UNKNOWN_PLACE = "unknown_place"
    UNKNOWN_TRANSITION = "unknown_transition"
    UNKNOWN_TOKEN = "unknown_token"
    NOT_SERIALIZABLE = "not_serializable"
    INVALID_TOKEN = "invalid_token"
    UNSUPPORTED = "unsupported"
    INTERNAL = "internal"


class ProtocolError(Exception):
    """A request that must be answered with an error response. Raised by the
    runner's handlers and turned into ``{"id", "error": {"message", "code"}}``."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- the omit-None base -------------------------------------------------------------------


class Wire(BaseModel):
    """Base of every wire model: drops ``None`` fields when serialised, except a
    field listed in ``nullable_fields`` that was set explicitly."""

    model_config = ConfigDict(extra="forbid")
    nullable_fields: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler):
        data = handler(self)
        return {
            key: value
            for key, value in data.items()
            if value is not None
            or (key in self.nullable_fields and key in self.model_fields_set)
        }

    def wire(self) -> dict:
        """The model as a JSON-ready dict."""
        return self.model_dump(mode="json")


# --- framing --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """One request: ``params`` is kept raw (``{}`` when absent or ``null``) so a
    method can reject a non-object with ``invalid_params`` under the request's id."""

    id: str
    method: str
    params: Any


@dataclass(frozen=True)
class Malformed:
    """A line that is not a request: its ``id`` if one could be read (else ``""``)
    and what was wrong, for an ``invalid_params`` response."""

    id: str
    message: str


class Requests:
    """Functions that read requests."""

    def parse(raw: Any) -> Union[Request, Malformed]:
        """A decoded JSON value as a request, or why it is not one."""
        if not isinstance(raw, dict):
            return Malformed("", "a request must be a JSON object")
        request_id = raw.get("id")
        if not isinstance(request_id, str):
            return Malformed("", 'a request needs a string "id"')
        method = raw.get("method")
        if not isinstance(method, str):
            return Malformed(request_id, 'a request needs a string "method"')
        params = raw.get("params")
        return Request(request_id, method, {} if params is None else params)


class Lines:
    """Functions that build the three kinds of line (PROTOCOL.md §3) as dicts."""

    def result(request_id: str, result: Union[Wire, dict]) -> dict:
        body = result.wire() if isinstance(result, Wire) else result
        return {"id": request_id, "result": body}

    def error(request_id: str, code: ErrorCode, message: str) -> dict:
        return {"id": request_id, "error": {"message": message, "code": str(code)}}

    def notification(notification: "Notification") -> dict:
        params = notification.wire()
        # The stamp every notification carries goes last, as the protocol writes it.
        for key in ("marking_version", "running"):
            params[key] = params.pop(key)
        return {"method": notification.method, "params": params}


# --- describe (§6.1) ------------------------------------------------------------------------


class ParamDescription(Wire):
    nullable_fields: ClassVar[frozenset[str]] = frozenset({"default"})

    name: str
    type_name: str
    required: bool
    default: Any = None


class NetDescription(Wire):
    name: str
    description: Optional[str] = None
    mode: Optional[str] = None
    schedule: Optional[str] = None
    params: list[ParamDescription] = []


class Describe(Wire):
    protocol_version: int = PROTOCOL_VERSION
    runtime: str = RUNTIME
    runtime_version: str
    nets: list[NetDescription]


# --- state (§5) -----------------------------------------------------------------------------


class Place(Wire):
    name: str
    type_name: str
    serializable: bool


class Transition(Wire):
    name: str
    detail: Optional[dict[str, Any]] = None


class Edge(Wire):
    place: str
    transition: str
    direction: Literal["in", "out"]
    kind: Optional[Literal["take", "take_all", "snapshot", "mutate"]] = None
    slot: Optional[int] = None


class Topology(Wire):
    places: list[Place]
    transitions: list[Transition]
    edges: list[Edge]
    hints: Optional[dict[str, Any]] = None


class TransitionState(Wire):
    enabled: bool
    fireable: bool
    in_flight: bool
    priority: Optional[float] = None


class Token(Wire):
    id: str
    label: str
    color: Optional[str] = None
    type_name: Optional[str] = None


class PlaceMarking(Wire):
    count: int
    tokens: list[Token]


class Marking(Wire):
    places: dict[str, PlaceMarking]


class Progress(Wire):
    nullable_fields: ClassVar[frozenset[str]] = frozenset({"last_fired", "last_progress_at"})

    step_count: int
    last_fired: Optional[str] = None
    fired_counts: dict[str, int]
    last_progress_at: Optional[str] = None


class State(Wire):
    protocol_version: int = PROTOCOL_VERSION
    runtime: str = RUNTIME
    net: str
    generation: int
    marking_version: int
    running: bool
    topology: Topology
    transitions: dict[str, TransitionState]
    marking: Marking
    progress: Progress


# --- other results (§6) ---------------------------------------------------------------------


class PlaceTokens(Wire):
    place: str
    marking_version: int
    count: int
    offset: int
    limit: int
    tokens: list[Token]
    has_more: bool


class TokenValue(Wire):
    """``get_token``: ``value`` for ``kind: "json"`` (which may be JSON ``null``),
    ``text`` for ``kind: "text"``."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset({"value"})

    id: str
    place: str
    type_name: str
    kind: Literal["json", "text"]
    value: Any = None
    text: Optional[str] = None


class StepResult(Wire):
    fired: bool
    transition: Optional[str] = None
    failed: Optional[bool] = None
    marking_version: int
    running: bool


class LogEntry(Wire):
    """One ``get_log`` entry (§8), keyed by place names and protocol token ids."""

    seq: int
    transition: str
    started_at: str
    duration_ms: float
    outcome: Literal["fired", "failed"]
    consumed: list[list[str]]
    produced: list[list[str]]
    error: Optional[str] = None
    restored: Optional[bool] = None


# --- notifications (§7) ---------------------------------------------------------------------


class Notification(Wire):
    """Every notification carries ``marking_version`` and ``running``. ``method``
    is the notification's name; ``moves_version`` says whether it is a state
    notification (§7.1), which raises ``marking_version`` by exactly one."""

    method: ClassVar[str]
    moves_version: ClassVar[bool] = False

    marking_version: int
    running: bool

    def line(self) -> dict:
        return Lines.notification(self)


class Fired(Notification):
    """A firing in one piece: legal in version 1 for a runtime that cannot
    observe a body part-way. Neither runner sends it; the Python runner sent it
    until 2026-10-04 and now sends ``Launched`` then ``Distributed``."""

    method: ClassVar[str] = "fired"
    moves_version: ClassVar[bool] = True

    transition: str
    consumed: list[list[str]]
    produced: list[list[str]]
    duration_ms: float


class Launched(Notification):
    """The first piece of a firing: inputs taken, body started."""

    method: ClassVar[str] = "launched"
    moves_version: ClassVar[bool] = True

    transition: str
    consumed: list[list[str]]


class Distributed(Notification):
    """The second piece of a firing: body returned, outputs placed."""

    method: ClassVar[str] = "distributed"
    moves_version: ClassVar[bool] = True

    transition: str
    produced: list[list[str]]
    duration_ms: float


class Failed(Notification):
    method: ClassVar[str] = "failed"
    moves_version: ClassVar[bool] = True

    transition: str
    error: str
    restored: bool
    consumed: list[list[str]]


class Added(Notification):
    method: ClassVar[str] = "added"
    moves_version: ClassVar[bool] = True

    place: str
    id: str
    label: str
    color: Optional[str] = None


class Removed(Notification):
    method: ClassVar[str] = "removed"
    moves_version: ClassVar[bool] = True

    place: str
    id: str


class Mutated(Notification):
    method: ClassVar[str] = "mutated"
    moves_version: ClassVar[bool] = True

    place: str
    id: str
    label: Optional[str] = None


class Toggled(Notification):
    method: ClassVar[str] = "toggled"
    moves_version: ClassVar[bool] = True

    transition: str
    enabled: bool


class ParamChanged(Notification):
    method: ClassVar[str] = "param_changed"
    moves_version: ClassVar[bool] = True

    param: str
    transition: Optional[str] = None


class Reloaded(Notification):
    method: ClassVar[str] = "reloaded"

    generation: int
    reason: Literal["load", "reset", "resync"]


class Fireable(Notification):
    method: ClassVar[str] = "fireable"

    transitions: list[str]


class Idle(Notification):
    method: ClassVar[str] = "idle"

    idle_since: str


class ExecutionStopped(Notification):
    method: ClassVar[str] = "execution_stopped"

    reason: str
    error_type: Optional[str] = None
    message: Optional[str] = None


class Heartbeat(Notification):
    """Both fields ``null`` when no body runs: pass them explicitly."""

    method: ClassVar[str] = "heartbeat"
    nullable_fields: ClassVar[frozenset[str]] = frozenset(
        {"step_started_at", "executing_transition"}
    )

    step_started_at: Optional[str] = None
    executing_transition: Optional[str] = None


class LoadProgress(Notification):
    method: ClassVar[str] = "load_progress"

    step: str
    message: str
