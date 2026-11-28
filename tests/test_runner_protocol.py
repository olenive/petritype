"""``petritype.runner``'s wire shapes, registry and translation, without an owner."""

import types
from typing import Annotated, Optional

import pytest
from pydantic import BaseModel

from petritype import petri_net
from petritype.core.serialisation import JsonForm
from petritype.runner import protocol as wire
from petritype.runner.protocol import ErrorCode, Lines, Malformed, ProtocolError, Request, Requests
from petritype.runner.registry import NetEntries, ParamSpec, Registry
from petritype.runner.translate import Colors, Labels, Serializable


# --- wire shapes ---------------------------------------------------------------------------


def test_optional_fields_are_omitted_never_null():
    assert wire.Token(id="1", label="x").wire() == {"id": "1", "label": "x"}
    assert wire.Token(id="1", label="x", color=None).wire() == {"id": "1", "label": "x"}
    assert wire.Edge(place="P", transition="T", direction="out", slot=0).wire() == {
        "place": "P", "transition": "T", "direction": "out", "slot": 0,
    }


def test_fields_the_protocol_sends_as_null_are_kept_when_given():
    progress = wire.Progress(step_count=0, last_fired=None, fired_counts={}, last_progress_at=None)
    assert progress.wire() == {"step_count": 0, "last_fired": None, "fired_counts": {}, "last_progress_at": None}
    heartbeat = wire.Heartbeat(step_started_at=None, executing_transition=None, marking_version=3, running=False)
    assert heartbeat.line() == {"method": "heartbeat", "params": {
        "step_started_at": None, "executing_transition": None, "marking_version": 3, "running": False,
    }}


def test_a_param_default_of_none_is_json_null_and_no_default_is_absent():
    with_none = wire.ParamDescription(name="p", type_name="int | None", required=False, default=None)
    assert with_none.wire() == {"name": "p", "type_name": "int | None", "required": False, "default": None}
    without = wire.ParamDescription(name="p", type_name="int", required=True)
    assert without.wire() == {"name": "p", "type_name": "int", "required": True}


def test_notification_lines_carry_the_stamp_last():
    line = wire.Toggled(transition="T", enabled=False, marking_version=4, running=True).line()
    assert line == {"method": "toggled", "params": {
        "transition": "T", "enabled": False, "marking_version": 4, "running": True,
    }}
    assert list(line["params"])[-2:] == ["marking_version", "running"]
    assert wire.Toggled.moves_version and not wire.Fireable.moves_version


def test_envelopes():
    assert Lines.result("7", {"a": 1}) == {"id": "7", "result": {"a": 1}}
    assert Lines.error("7", ErrorCode.UNKNOWN_PLACE, "no") == {
        "id": "7", "error": {"message": "no", "code": "unknown_place"},
    }


def test_the_error_codes_are_the_closed_set():
    assert {str(code) for code in ErrorCode} == {
        "unknown_method", "invalid_params", "not_loaded", "unknown_net", "load_failed",
        "unknown_place", "unknown_transition", "unknown_token", "unknown_control", "not_serializable",
        "invalid_token", "closed_net", "unsupported", "internal",
    }


def test_request_parsing():
    assert Requests.parse({"id": "1", "method": "step"}) == Request("1", "step", {})
    assert Requests.parse({"id": "1", "method": "step", "params": None}) == Request("1", "step", {})
    assert Requests.parse({"id": "1", "method": "step", "params": 3}) == Request("1", "step", 3)
    assert Requests.parse([1]) == Malformed("", "a request must be a JSON object")
    assert Requests.parse({"id": 1, "method": "step"}).id == ""
    assert Requests.parse({"id": "9"}) == Malformed("9", 'a request needs a string "method"')


# --- registry ------------------------------------------------------------------------------


def test_from_modules_collects_decorated_factories_once():
    @petri_net(name="alpha", mode="cron", schedule="0 9 * * *")
    def alpha(when: object = object()):
        """First line.

        Rest."""

    @petri_net(name="beta", description="The beta net")
    def beta(limit: int | None = None, **extra):
        pass

    def plain():
        pass

    first = types.ModuleType("first")
    first.alpha, first.plain = alpha, plain
    second = types.ModuleType("second")
    second.beta, second.alpha_again = beta, alpha  # re-exported: counted once
    registry = Registry.from_modules([first, second])
    assert list(registry.entries) == ["alpha", "beta"]
    described = registry.describe().wire()
    assert described["protocol_version"] == 2 and described["runtime"] == "python"
    assert described["nets"] == [
        {"name": "alpha", "description": "First line.", "mode": "cron", "schedule": "0 9 * * *",
         # A default with no JSON form is not sent.
         "params": [{"name": "when", "type_name": "object", "required": False}]},
        {"name": "beta", "description": "The beta net", "mode": "manual",
         "params": [{"name": "limit", "type_name": "int | None", "required": False, "default": None}]},
    ]
    # **extra lets names through that the signature does not declare.
    assert NetEntries.bind(registry.get("beta"), {"limit": 3, "other": "x"}) == {"limit": 3, "other": "x"}


def test_two_factories_with_one_name_are_an_error():
    @petri_net(name="same")
    def one():
        pass

    @petri_net(name="same")
    def two():
        pass

    module = types.ModuleType("m")
    module.one, module.two = one, two
    with pytest.raises(ValueError, match="same"):
        Registry.from_modules([module])


def test_bind_validates_against_annotations():
    class Config(BaseModel):
        size: int

    def factory(config: Config, scale: float = 1.0):
        pass

    entry = NetEntries.from_factory(factory)
    bound = NetEntries.bind(entry, {"config": {"size": 2}, "scale": 3})
    assert bound == {"config": Config(size=2), "scale": 3.0}
    with pytest.raises(ProtocolError) as raised:
        NetEntries.bind(entry, {"config": {"size": "big"}})
    assert raised.value.code is ErrorCode.INVALID_PARAMS
    with pytest.raises(ProtocolError):
        NetEntries.bind(entry, {})


def test_adapter_handles_unhashable_annotation():
    unhashable = Annotated[int, {"meta": "unhashable"}]
    spec = ParamSpec(name="x", type_name="int", required=True, annotation=unhashable)
    adapter = NetEntries.adapter(spec)
    assert adapter is not None
    assert adapter.validate_python(5) == 5


# --- translation ---------------------------------------------------------------------------


def test_label_cut_boundaries():
    assert Labels.cut("a" * 1000) == "a" * 1000
    cut = Labels.cut("a" * 1001)
    assert len(cut) == 1000 and cut == "a" * 997 + "..."


def test_colors():
    class Tok:
        def __init__(self, color):
            self.color = color

    assert Colors.of(Tok("RED")) == "#ef4444"
    assert Colors.of(Tok("#abcdef")) == "#abcdef"
    assert Colors.of(Tok("mauve")) is None
    assert Colors.of(Tok(None)) is None
    assert Colors.of(3) is None


def test_serializable_and_decode():
    class Model(BaseModel):
        a: int

    class Arbitrary(BaseModel):
        model_config = {"arbitrary_types_allowed": True}
        thing: types.SimpleNamespace

    assert Serializable.place_type(Model) and Serializable.place_type(str)
    assert not Serializable.place_type(Arbitrary)
    assert not Serializable.place_type(list[int])
    assert Serializable.decode(Model, {"a": 1}) == Model(a=1)
    assert Serializable.decode(float, 2) == 2.0
    assert Serializable.decode(bool, False) is False
    for place_type, payload in [(bool, "false"), (int, True), (int, 1.5), (str, None), (int, "x")]:
        with pytest.raises(ValueError):
            Serializable.decode(place_type, payload)


def test_a_subclass_of_a_scalar_has_its_scalars_json_form():
    """``class Colour(str)``: serializable, decoded by its own constructor from its
    scalar's JSON type only, encoded as the plain scalar (the traffic light's light)."""
    from petritype.core.serialisation import JsonForm

    class Colour(str):
        def __new__(cls, value):
            if value not in ("red", "green"):
                raise ValueError(f"no colour {value!r}")
            return super().__new__(cls, value)

    class Count(int):
        pass

    assert Serializable.place_type(Colour) and Serializable.place_type(Count)
    red = Serializable.decode(Colour, "red")
    assert type(red) is Colour and red == "red"
    assert JsonForm.encode(red) == "red" and type(JsonForm.encode(red)) is str
    assert type(Serializable.decode(Count, 3)) is Count
    for place_type, payload in [(Colour, 7), (Colour, "purple"), (Colour, None), (Count, "3"), (Count, True)]:
        with pytest.raises(ValueError):
            Serializable.decode(place_type, payload)


def test_json_form_encode_of_scalars_and_their_subclasses():
    """Each JSON scalar encodes as itself, a non-finite float is refused, and a
    subclass of ``int`` encodes as a plain ``int`` (a subclass of ``str``: see
    ``test_a_subclass_of_a_scalar_has_its_scalars_json_form``)."""
    class Score(int):
        pass

    assert JsonForm.encode(True) is True
    assert JsonForm.encode(False) is False
    assert type(JsonForm.encode(42)) is int and JsonForm.encode(42) == 42
    assert type(JsonForm.encode("abc")) is str and JsonForm.encode("abc") == "abc"
    assert type(JsonForm.encode(3.25)) is float and JsonForm.encode(3.25) == 3.25

    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            JsonForm.encode(bad)

    encoded_score = JsonForm.encode(Score(7))
    assert type(encoded_score) is int and encoded_score == 7


class Reading(BaseModel):
    sensor: str
    value: float
    note: Optional[str] = None


class Batch(BaseModel):
    readings: list[Reading]
    limits: dict[str, float] = {}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_model_that_holds_a_float_json_cannot_write_has_no_json_form(bad):
    """Written out, the field would be spelt ``null``, which says something else and
    does not load back; a bare float that is one is refused the same way."""
    with pytest.raises(ValueError, match="value"):
        JsonForm.encode(Reading(sensor="b", value=bad))
    with pytest.raises(ValueError, match=r"readings\[1\]\.value"):
        JsonForm.encode(Batch(readings=[Reading(sensor="a", value=1.0), Reading(sensor="b", value=bad)]))
    with pytest.raises(ValueError, match="limits.high"):
        JsonForm.encode(Batch(readings=[], limits={"low": 0.0, "high": bad}))


def test_a_model_with_an_empty_optional_field_keeps_its_json_form():
    assert JsonForm.encode(Reading(sensor="a", value=1.5)) == {"sensor": "a", "value": 1.5, "note": None}
    assert JsonForm.encode(Batch(readings=[Reading(sensor="a", value=1.5)])) == {
        "readings": [{"sensor": "a", "value": 1.5, "note": None}], "limits": {},
    }


@pytest.mark.parametrize("bad, spelt", [(float("nan"), "nan"), (float("inf"), "inf"), (float("-inf"), "-inf")])
def test_the_label_of_a_model_that_holds_such_a_float_is_its_repr(bad, spelt):
    """The JSON dump writes the field as ``null``, which reads as a value that is
    missing, so the label is the ``repr``, which writes the float."""
    reading = Reading(sensor="b", value=bad)
    assert Labels.of(reading) == repr(reading) == f"Reading(sensor='b', value={spelt}, note=None)"
    batch = Batch(readings=[Reading(sensor="a", value=1.0), reading])
    assert Labels.of(batch) == repr(batch) and f"value={spelt}" in Labels.of(batch)


def test_the_label_of_any_other_model_is_its_json_dump_an_empty_field_included():
    assert Labels.of(Reading(sensor="a", value=1.5)) == '{"sensor":"a","value":1.5,"note":null}'
    assert Labels.of(Reading(sensor="a", value=1.5, note="ok")) == '{"sensor":"a","value":1.5,"note":"ok"}'
