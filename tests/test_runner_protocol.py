"""``petritype.runner``'s wire shapes, registry and translation, without an owner."""

import types

import pytest
from pydantic import BaseModel

from petritype import petri_net
from petritype.runner import protocol as wire
from petritype.runner.protocol import ErrorCode, Lines, Malformed, ProtocolError, Request, Requests
from petritype.runner.registry import NetEntries, Registry
from petritype.runner.translate import Colors, Labels, Serializable, TypeNames


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
        "invalid_token", "unsupported", "internal",
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
    assert described["protocol_version"] == 1 and described["runtime"] == "python"
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


def test_type_names():
    assert TypeNames.of(int) == "int"
    assert TypeNames.of(list[int]) == "list[int]"
    assert TypeNames.of(int | None) == "int | None"
    assert TypeNames.of(dict[str, tuple[int, ...]]) == "dict[str, tuple[int, ...]]"


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
