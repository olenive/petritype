"""The runner validates wire input with JSON semantics in strict mode.

User-supplied data reaches pydantic at three wire boundaries only: ``inject``
decoding a token for a pydantic-model place, ``set_param`` decoding a keyword's
value, and ``load`` decoding a factory's parameters. The wire is JSON, so each
validates as ``model_validate_json`` / ``TypeAdapter.validate_json`` with
``strict=True`` would: no ``"9"`` for an ``int``, no ``9`` for a ``str``, no
``1`` for a ``bool``, no ``true`` for an ``int``, all the way down nested
models, while what JSON has no other way to say (an ISO 8601 string for a
``datetime``) still decodes. Pydantic's default lax mode would coerce all of
these, as the first test shows.

``Owner`` is driven directly, as in ``test_runner_owner.py``.
"""

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 3, 9, 15, 2, 413000, tzinfo=timezone.utc)
ISO = "2026-10-03T09:15:02Z"


class Inner(BaseModel):
    k: int


class Reading(BaseModel):
    n: int = 0
    s: str = ""
    b: bool = False
    f: float = 0.0
    at: datetime | None = None
    inner: Inner | None = None


# What the factory and the transition body last received, by keyword.
SEEN: dict = {}


def tune(
    reading: Reading,
    n: int = 0,
    s: str = "",
    b: bool = False,
    f: float = 0.0,
    at: datetime | None = None,
    inner: Inner | None = None,
) -> Reading:
    SEEN["set_param"] = dict(n=n, s=s, b=b, f=f, at=at, inner=inner)
    return reading


@petri_net(name="readings")
def readings(
    n: int = 0,
    s: str = "",
    b: bool = False,
    f: float = 0.0,
    at: datetime | None = None,
    inner: Inner | None = None,
):
    SEEN["load"] = dict(n=n, s=s, b=b, f=f, at=at, inner=inner)
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", Reading, [Reading()]),
        ArgumentEdgeToTransition("In", "Tune", "reading"),
        FunctionTransitionNode("Tune", tune),
        ReturnedEdgeFromTransition("Tune", "Out"),
        ListPlaceNode("Out", Reading),
    ])


class Harness:
    """Assigns ids "1", "2", ... and lands every launch inline."""

    def __init__(self):
        self.owner = Owner(Registry.from_factories([readings]))
        self.next_id = 0

    async def send(self, method, params=None):
        self.next_id += 1
        request = {"id": str(self.next_id), "method": method}
        if params is not None:
            request["params"] = params
        out = await self.owner.handle(request, NOW)
        lines = list(out.lines)
        while out.launch is not None:
            out = await self.owner.completed(await out.launch.coroutine, NOW)
            lines.extend(out.lines)
        return lines


# --- the three boundaries: each sends ``{field: value}`` and answers the last line ----------


async def via_inject(field, value):
    h = Harness()
    assert "result" in (await h.send("load", {"net": "readings"}))[-1]
    lines = await h.send("inject", {"place": "Out", "token": {field: value}})
    if "result" in lines[-1]:
        [token] = await h.send("get_token", {"id": lines[-1]["result"]["id"]})
        return lines[-1], token["result"]["value"][field]
    return lines[-1], None


async def via_set_param(field, value):
    h = Harness()
    assert "result" in (await h.send("load", {"net": "readings"}))[-1]
    lines = await h.send("set_param", {"transition": "Tune", "param": field, "value": value})
    if "result" in lines[-1]:
        SEEN.pop("set_param", None)
        assert (await h.send("step"))[-1]["result"]["fired"] is True
        return lines[-1], SEEN["set_param"][field]
    return lines[-1], None


async def via_load(field, value):
    h = Harness()
    SEEN.pop("load", None)
    lines = await h.send("load", {"net": "readings", "params": {field: value}})
    if "result" in lines[-1]:
        return lines[-1], SEEN["load"][field]
    return lines[-1], None


BOUNDARIES = [
    pytest.param(via_inject, "invalid_token", id="inject"),
    pytest.param(via_set_param, "invalid_params", id="set_param"),
    pytest.param(via_load, "invalid_params", id="load"),
]


def assert_rejected(line, code, *fragments):
    assert "error" in line, line
    assert line["error"]["code"] == code, line
    for fragment in fragments:
        assert fragment in line["error"]["message"], line


# --- the oversight ---------------------------------------------------------------------------


async def test_lax_mode_coerces_a_numeric_string_which_is_why_the_runner_must_not_use_it():
    assert Reading.model_validate({"n": "9"}).n == 9
    assert Reading.model_validate({"b": 1}).b is True
    assert Reading.model_validate({"inner": {"k": "9"}}).inner == Inner(k=9)


# --- rejected ----------------------------------------------------------------------------------


@pytest.mark.parametrize("boundary, code", BOUNDARIES)
async def test_a_numeric_string_for_an_int_is_rejected(boundary, code):
    line, _ = await boundary("n", "9")
    assert_rejected(line, code, "valid integer")


@pytest.mark.parametrize("boundary, code", BOUNDARIES)
async def test_a_number_for_a_str_is_rejected(boundary, code):
    line, _ = await boundary("s", 9)
    assert_rejected(line, code, "valid string")


@pytest.mark.parametrize("boundary, code", BOUNDARIES)
async def test_a_number_for_a_bool_is_rejected(boundary, code):
    line, _ = await boundary("b", 1)
    assert_rejected(line, code, "valid boolean")


@pytest.mark.parametrize("boundary, code", BOUNDARIES)
async def test_a_bool_for_an_int_is_rejected(boundary, code):
    line, _ = await boundary("n", True)
    assert_rejected(line, code, "valid integer")


@pytest.mark.parametrize("boundary, code", BOUNDARIES)
async def test_strictness_reaches_a_nested_model(boundary, code):
    line, _ = await boundary("inner", {"k": "9"})
    assert_rejected(line, code, "valid integer")


@pytest.mark.parametrize("boundary, code", BOUNDARIES)
async def test_a_date_only_string_or_a_unix_number_for_a_datetime_is_rejected(boundary, code):
    for value in ("2026-10-03", 1700000000):
        line, _ = await boundary("at", value)
        assert_rejected(line, code, "valid datetime")


# --- accepted ----------------------------------------------------------------------------------


@pytest.mark.parametrize("boundary, code", BOUNDARIES)
async def test_an_int_for_a_float_is_accepted_as_a_float(boundary, code):
    """Strict JSON mode accepts a JSON integer for ``float`` (JSON has one number
    type) and makes it a ``float``."""
    line, value = await boundary("f", 1)
    assert "result" in line, line
    assert value == 1.0 and type(value) is float


@pytest.mark.parametrize("boundary, code", BOUNDARIES)
async def test_an_iso_8601_string_for_a_datetime_is_accepted(boundary, code):
    """JSON has no datetime, so the string is the only way a wire can send one."""
    line, value = await boundary("at", ISO)
    assert "result" in line, line
    expected = datetime(2026, 10, 3, 9, 15, 2, tzinfo=timezone.utc)
    if boundary is via_inject:
        assert datetime.fromisoformat(value) == expected  # get_token answers JSON
    else:
        assert value == expected and type(value) is datetime


@pytest.mark.parametrize("boundary, code", BOUNDARIES)
async def test_well_typed_values_and_a_nested_model_are_accepted(boundary, code):
    for field, value in [("n", 9), ("s", "nine"), ("b", True), ("f", 1.5)]:
        line, seen = await boundary(field, value)
        assert "result" in line, (field, line)
        assert seen == value and type(seen) is type(value), (field, seen)
    line, seen = await boundary("inner", {"k": 9})
    assert "result" in line, line
    assert seen == ({"k": 9} if boundary is via_inject else Inner(k=9))
