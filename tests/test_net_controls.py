"""Net-declared controls and place styles: ``ControlSpec``'s constructors and the
``Fire`` command in the runtime, the decorator and ``NetEntry`` carrying them, the
wire ``Control`` and ``places[].style`` in the topology (PROTOCOL.md §5.1), and
``press`` in the Owner (§6): the value checked by kind, ``unknown_control``,
``nothing`` on a held fire, ``pressed`` with the events of what it did.

The traffic light's own controls are tested beside its other tests, in
``tests/test_empty_place_edges.py``.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.runner import protocol as wire
from petritype.runner.owner import Owner
from petritype.runner.registry import NetEntries, Registry
from petritype.runtime import (
    ControlSpec, Disable, Enable, Extend, Fire, PlaceStyle, RunContext, Runner, SetTokens, TooManyFiringsError,
    apply_control,
)

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


# --- a small net: numbers go from In to Out through Move, which only an even number may take ---


def move(n: int) -> int:
    return n


def even_first(graph) -> bool:
    tokens = graph.place_named("In").tokens
    return bool(tokens) and tokens[0] % 2 == 0


def build_numbers(seed=(2,)):
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("In", int, list(seed)),
        ListPlaceNode("Out", int),
        FunctionTransitionNode("Move", move, guard=even_first),
        ArgumentEdgeToTransition("In", "Move", "n"),
        ReturnedEdgeFromTransition("Move", "Out"),
    ], record_events=True)


def numbered(value, graph):
    """``make`` for ``add``: the value given, or one more than every number in the net."""
    if value is not True:
        return value
    return 1 + sum(len(place.tokens) for place in graph.places)


CONTROLS = {
    "add": ControlSpec.inject("In", numbered, "Add"),
    "move": ControlSpec.fire("Move", "Move"),
    "switch": ControlSpec.enable("Move", "Move on"),
    "amount": ControlSpec("In", "slider", "Amount", lambda n: Extend("In", [int(n)]),
                          {"start": 0, "stop": 10, "step": 2, "value": 4}),
    "pick": ControlSpec("Out", "select", "Pick", lambda choice: SetTokens("Out", [len(choice)]),
                        {"options": ["a", "bb"], "value": "a"}),
    "say": ControlSpec("In", "text", "Say", lambda text: Extend("In", [len(text)]) if text else None,
                       {"placeholder": "words", "value": "ignored on the wire"}),
    "idle": ControlSpec("", "button", "Idle", lambda _clicked: None, {"kind": "warn"}),
    "boom": ControlSpec("Move", "button", "Boom", lambda _clicked: 1 / 0),
    "stray": ControlSpec("Move", "button", "Stray", lambda _clicked: Fire("Nowhere")),
}


@petri_net(name="numbers", controls=CONTROLS, place_styles={"Out": PlaceStyle(fill="token")})
def numbers():
    return build_numbers()


@petri_net(name="odd", controls={"move": ControlSpec.fire("Move", "Move")})
def odd():
    return build_numbers(seed=(3,))


@petri_net(name="plain")
def plain():
    return build_numbers()


async def send_all(owner, requests):
    """Each request in turn, each launch awaited and landed; the lines each caused."""
    out_lines = []
    for n, (method, params) in enumerate(requests, 1):
        out = await owner.handle({"id": str(n), "method": method, "params": params or {}}, NOW)
        lines = list(out.lines)
        while out.launch is not None:
            out = await owner.completed(await out.launch.coroutine, NOW)
            lines.extend(out.lines)
        out_lines.append(lines)
    return out_lines


async def loaded(*factories, net="numbers"):
    owner = Owner(Registry.from_factories(factories or [numbers]))
    await send_all(owner, [("load", {"net": net})])
    return owner


async def press(owner, **params):
    (lines,) = await send_all(owner, [("press", params)])
    return lines


def methods(lines):
    return [line.get("method") or ("error" if "error" in line else "result") for line in lines]


def marking(owner):
    return {place.name: list(place.tokens) for place in owner._loaded.outer.graph.places}


# --- the runtime ---------------------------------------------------------------------------------


class TestTheConstructors:

    def test_inject_adds_what_make_makes(self):
        spec = ControlSpec.inject("Waiting", lambda value: f"Car {value}", "+ Car", kind="text",
                                  config={"placeholder": "name"})
        assert (spec.node, spec.kind, spec.label, spec.action) == ("Waiting", "text", "+ Car", "inject")
        assert spec.config == {"placeholder": "name"}
        assert ControlSpec.command(spec, "7") == Extend("Waiting", ["Car 7"])

    def test_inject_hands_make_the_net_when_it_takes_it(self):
        spec = ControlSpec.inject("In", numbered, "Add")
        assert spec.kind == "button" and spec.config == {}
        graph = build_numbers(seed=(2, 4))
        assert ControlSpec.command(spec, True, graph) == Extend("In", [3])
        assert ControlSpec.command(spec, 9, graph) == Extend("In", [9])

    def test_fire_is_a_button_giving_fire(self):
        spec = ControlSpec.fire("Change", "Change")
        assert (spec.node, spec.kind, spec.label, spec.action, spec.config) == ("Change", "button", "Change", "fire", {})
        assert ControlSpec.command(spec, True) == Fire("Change")

    def test_enable_is_a_toggle_giving_enable_or_disable(self):
        spec = ControlSpec.enable("Sort", "Sorter on", value=False)
        assert (spec.node, spec.kind, spec.action, spec.config) == ("Sort", "toggle", "enable", {"value": False})
        assert ControlSpec.command(spec, True) == Enable("Sort")
        assert ControlSpec.command(spec, False) == Disable("Sort")
        assert ControlSpec.enable("Sort", "Sorter on").config == {"value": True}

    def test_a_hand_written_spec_keeps_working_and_has_no_action(self):
        spec = ControlSpec("Inbox", "button", "New", lambda clicked: Extend("Inbox", [1]) if clicked else None)
        assert spec.action is None
        assert ControlSpec.command(spec, True, build_numbers()) == Extend("Inbox", [1])
        assert ControlSpec.command(spec, False) is None

    def test_apply_control_hands_the_net_and_enqueues(self):
        async def run():
            ctx = RunContext(graph=build_numbers(seed=(2, 4, 6)), inbox=asyncio.Queue())
            command = apply_control(ctx, CONTROLS["add"], True)
            assert command == Extend("In", [4]) and ctx.inbox.get_nowait() == command
            assert apply_control(ctx, CONTROLS["idle"], True) is None and ctx.inbox.empty()

        asyncio.run(run())


class TestTheFireCommand:

    @staticmethod
    def drained(ctx, *commands):
        for command in commands:
            ctx.inbox.put_nowait(command)
        return Runner.drain_inbox(ctx)

    def test_drain_fires_a_fireable_transition_once(self):
        async def run():
            graph = build_numbers(seed=(2, 4))
            ctx = RunContext(graph=graph, inbox=asyncio.Queue())
            assert self.drained(ctx, Fire("Move")) == 1
            assert [p.tokens for p in graph.places] == [[4], [2]], "one firing, not a run"
            assert graph.fired_counts == {"Move": 1} and graph.last_fired == "Move" and ctx.n_fired == 1

        asyncio.run(run())

    def test_drain_fires_outside_an_event_loop_too(self):
        graph = build_numbers()
        ctx = RunContext(graph=graph, inbox=asyncio.Queue())
        assert self.drained(ctx, Fire("Move")) == 1
        assert [p.tokens for p in graph.places] == [[], [2]]

    def test_a_transition_that_is_not_fireable_is_left_alone(self):
        async def run():
            graph = build_numbers(seed=(3,))
            ctx = RunContext(graph=graph, inbox=asyncio.Queue(), strict=True)
            assert self.drained(ctx, Fire("Move")) == 0, "held by its guard: nothing, and no error"
            graph.place_named("In").tokens.insert(0, 2)
            assert self.drained(ctx, Disable("Move"), Fire("Move")) == 1, "the Disable applied, the Fire not"
            assert [p.tokens for p in graph.places] == [[2, 3], []] and graph.fired_counts == {}

        asyncio.run(run())

    def test_commands_apply_in_order_around_a_fire(self):
        async def run():
            graph = build_numbers(seed=())
            ctx = RunContext(graph=graph, inbox=asyncio.Queue())
            assert self.drained(ctx, Extend("In", [8]), Fire("Move"), Extend("In", [6])) == 3
            assert [p.tokens for p in graph.places] == [[6], [8]]

        asyncio.run(run())

    def test_an_unknown_transition_is_a_bad_command(self):
        ctx = RunContext(graph=build_numbers(), inbox=asyncio.Queue())
        assert self.drained(ctx, Fire("Nowhere")) == 0
        ctx.strict = True
        with pytest.raises(ValueError, match="unknown transition"):
            self.drained(ctx, Fire("Nowhere"))

    def test_a_fire_counts_against_the_fuse(self):
        ctx = RunContext(graph=build_numbers(seed=(2, 4)), inbox=asyncio.Queue(), error_after_n_firings=1)
        assert self.drained(ctx, Fire("Move")) == 1
        with pytest.raises(TooManyFiringsError):
            self.drained(ctx, Fire("Move"))
        assert ctx.graph.place_named("In").tokens == [4], "refused before taking anything"

    def test_step_drains_a_fire_then_fires_as_usual(self):
        async def run():
            graph = build_numbers(seed=(2, 4))
            ctx = RunContext(graph=graph, inbox=asyncio.Queue())
            ctx.inbox.put_nowait(Fire("Move"))
            assert await Runner.step(ctx) == 1
            assert graph.fired_counts == {"Move": 2}, "the pressed firing, then the step's own"

        asyncio.run(run())


def test_the_decorator_and_the_entry_carry_the_declarations():
    config = numbers._petri_net_config
    assert config["controls"] is CONTROLS and config["place_styles"] == {"Out": PlaceStyle(fill="token")}
    entry = NetEntries.from_factory(numbers)
    assert entry.controls is CONTROLS and entry.place_styles == {"Out": PlaceStyle("token")}
    assert NetEntries.controls(entry) == CONTROLS and NetEntries.controls(entry) is not CONTROLS
    bare = NetEntries.from_factory(plain)
    assert bare.controls is None and bare.place_styles is None and NetEntries.controls(bare) == {}


# --- the wire --------------------------------------------------------------------------------------


class TestTheTopology:

    @pytest.mark.asyncio
    async def test_controls_and_styles_are_in_the_topology(self):
        owner = await loaded()
        (lines,) = await send_all(owner, [("get_state", None)])
        topology = lines[-1]["result"]["topology"]
        assert topology["controls"] == [
            {"name": "add", "label": "Add", "kind": "button", "node": "In", "action": "inject"},
            {"name": "move", "label": "Move", "kind": "button", "node": "Move", "action": "fire"},
            {"name": "switch", "label": "Move on", "kind": "toggle", "node": "Move", "action": "enable",
             "config": {"value": True}},
            {"name": "amount", "label": "Amount", "kind": "slider", "node": "In",
             "config": {"start": 0, "stop": 10, "step": 2, "value": 4}},
            {"name": "pick", "label": "Pick", "kind": "select", "node": "Out",
             "config": {"options": ["a", "bb"], "value": "a"}},
            {"name": "say", "label": "Say", "kind": "text", "node": "In", "config": {"placeholder": "words"}},
            {"name": "idle", "label": "Idle", "kind": "button", "config": {"kind": "warn"}},
            {"name": "boom", "label": "Boom", "kind": "button", "node": "Move"},
            {"name": "stray", "label": "Stray", "kind": "button", "node": "Move"},
        ], "in declaration order; node, action and config only when there; no initial value for a button or a text"
        assert [place.get("style") for place in topology["places"]] == [None, {"fill": "token"}]

    @pytest.mark.asyncio
    async def test_a_net_that_declares_nothing_has_neither(self):
        owner = await loaded(plain, net="plain")
        (lines,) = await send_all(owner, [("get_state", None)])
        topology = lines[-1]["result"]["topology"]
        assert "controls" not in topology and all("style" not in place for place in topology["places"])

    def test_the_wire_models(self):
        control = wire.Control(name="go", label="Go", kind="button")
        assert control.wire() == {"name": "go", "label": "Go", "kind": "button"}
        with pytest.raises(ValueError):
            wire.Control(name="go", label="Go", kind="knob")
        with pytest.raises(ValueError):
            wire.Control(name="go", label="Go", kind="button", action="launch")
        assert wire.PlaceStyle(fill="token").wire() == {"fill": "token"}
        with pytest.raises(ValueError):
            wire.PlaceStyle(fill="stripes")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("controls, place_styles", [
        ({"bad": ControlSpec("In", "knob", "Bad", lambda v: None)}, None),
        ({"bad": ControlSpec("In", "button", "Bad", lambda v: None, action="launch")}, None),
        ({"bad": ControlSpec("Nowhere", "button", "Bad", lambda v: None)}, None),
        ({"bad": ControlSpec("In", "button", "Bad", lambda v: None, {"kind": object()})}, None),
        ({}, {"Nowhere": PlaceStyle(fill="token")}),
        ({}, {"In": PlaceStyle(fill="stripes")}),
        (lambda: ["not", "a", "map"], None),
    ], ids=["kind", "action", "node", "config", "style place", "fill", "not a map"])
    async def test_a_bad_declaration_fails_the_load(self, controls, place_styles):
        @petri_net(name="bad", controls=controls, place_styles=place_styles)
        def bad():
            return build_numbers()

        owner = Owner(Registry.from_factories([bad]))
        (lines,) = await send_all(owner, [("load", {"net": "bad"})])
        assert lines[-1]["error"]["code"] == "load_failed"

    @pytest.mark.asyncio
    async def test_a_callable_gives_each_generation_its_own_controls(self):
        made = []

        def controls():
            made.append({"add": ControlSpec.inject("In", lambda value: len(made), "Add")})
            return made[-1]

        @petri_net(name="fresh", controls=controls)
        def fresh():
            return build_numbers(seed=())

        owner = Owner(Registry.from_factories([fresh]))
        await send_all(owner, [("load", {"net": "fresh"}), ("press", {"control": "add"}),
                               ("reset", None), ("press", {"control": "add"})])
        assert len(made) == 2 and marking(owner)["In"] == [2]


# --- press ------------------------------------------------------------------------------------


class TestPress:

    @pytest.mark.asyncio
    async def test_an_inject_adds_its_token(self):
        owner = await loaded()
        lines = await press(owner, control="add")
        assert methods(lines) == ["added", "result"]
        assert lines[0]["params"]["place"] == "In" and lines[0]["params"]["label"] == "2"
        assert lines[-1]["result"] == {"status": "pressed", "marking_version": lines[0]["params"]["marking_version"]}
        assert marking(owner)["In"] == [2, 2]

    @pytest.mark.asyncio
    async def test_a_fire_fires_as_a_step_does(self):
        owner = await loaded()
        lines = await press(owner, control="move")
        assert methods(lines) == ["launched", "distributed", "fireable", "result"]
        assert lines[0]["params"]["transition"] == "Move" and lines[2]["params"]["transitions"] == []
        assert lines[-1]["result"] == {"status": "pressed", "marking_version": lines[1]["params"]["marking_version"]}
        (state,) = await send_all(owner, [("get_state", None)])
        assert state[-1]["result"]["progress"]["fired_counts"] == {"Move": 1}
        assert marking(owner) == {"In": [], "Out": [2]}

    @pytest.mark.asyncio
    async def test_a_held_fire_does_nothing(self):
        owner = await loaded(odd, net="odd")
        lines = await press(owner, control="move")
        assert lines == [{"id": "1", "result": {
            "status": "nothing", "reason": "Move is not fireable", "marking_version": 0,
        }}]
        assert marking(owner) == {"In": [3], "Out": []}

    @pytest.mark.asyncio
    async def test_a_disabled_transition_is_not_fireable_either(self):
        owner = await loaded()
        lines = await press(owner, control="switch", value=False)
        assert methods(lines) == ["toggled", "fireable", "result"] and lines[0]["params"]["enabled"] is False
        lines = await press(owner, control="move")
        assert lines[-1]["result"]["status"] == "nothing"
        lines = await press(owner, control="switch", value=True)
        assert methods(lines) == ["toggled", "fireable", "result"] and lines[0]["params"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_a_control_that_gives_nothing_answers_nothing(self):
        owner = await loaded()
        lines = await press(owner, control="idle")
        assert lines == [{"id": "1", "result": {
            "status": "nothing", "reason": "idle has nothing to do", "marking_version": 0,
        }}]
        assert (await press(owner, control="say", value=""))[-1]["result"]["status"] == "nothing"

    @pytest.mark.asyncio
    async def test_each_kind_passes_its_value(self):
        owner = await loaded()
        assert methods(await press(owner, control="amount", value=10)) == ["added", "result"]
        assert methods(await press(owner, control="amount", value=0.5)) == ["added", "result"]
        assert methods(await press(owner, control="pick", value="bb")) == ["added", "result"]
        assert methods(await press(owner, control="say", value="hello")) == ["added", "result"]
        assert marking(owner) == {"In": [2, 10, 0, 5], "Out": [2]}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("control, params", [
        ("add", {"value": 1}),
        ("move", {"value": True}),
        ("switch", {}),
        ("switch", {"value": 1}),
        ("switch", {"value": "true"}),
        ("amount", {"value": 11}),
        ("amount", {"value": -1}),
        ("amount", {"value": True}),
        ("amount", {"value": "4"}),
        ("pick", {"value": "c"}),
        ("pick", {"value": 1}),
        ("say", {}),
        ("say", {"value": 7}),
        ("say", {"value": ["hi"]}),
    ], ids=[
        "button given a value", "fire button given a value", "toggle without", "toggle given 1",
        "toggle given a string", "slider above", "slider below", "slider given true", "slider given a string",
        "select off the list", "select given a number", "text without", "text given a number",
        "text given a list",
    ])
    async def test_a_value_of_the_wrong_kind_is_invalid_params(self, control, params):
        owner = await loaded()
        lines = await press(owner, control=control, **params)
        assert methods(lines) == ["error"] and lines[0]["error"]["code"] == "invalid_params"
        assert marking(owner) == {"In": [2], "Out": []}

    @pytest.mark.asyncio
    async def test_a_null_value_is_no_value(self):
        owner = await loaded()
        assert (await press(owner, control="move", value=None))[-1]["result"]["status"] == "pressed"

    @pytest.mark.asyncio
    async def test_an_unknown_control(self):
        owner = await loaded()
        lines = await press(owner, control="nope")
        assert lines[0]["error"]["code"] == "unknown_control"
        lines = await press(owner)
        assert lines[0]["error"]["code"] == "invalid_params", "no control named"
        owner = await loaded(plain, net="plain")
        assert (await press(owner, control="move"))[0]["error"]["code"] == "unknown_control"

    @pytest.mark.asyncio
    async def test_press_acts_on_the_outer_net_only(self):
        owner = await loaded()
        lines = await press(owner, control="move", path="numbers")
        assert lines[0]["error"]["code"] == "invalid_params"

    @pytest.mark.asyncio
    async def test_a_control_that_raises_is_internal(self):
        owner = await loaded()
        assert (await press(owner, control="boom"))[0]["error"]["code"] == "internal"
        assert (await press(owner, control="stray"))[0]["error"]["code"] == "internal"
        assert marking(owner) == {"In": [2], "Out": []}

    @pytest.mark.asyncio
    async def test_press_needs_a_loaded_net(self):
        owner = Owner(Registry.from_factories([numbers]))
        (lines,) = await send_all(owner, [("press", {"control": "move"})])
        assert lines[0]["error"]["code"] == "not_loaded"

    @pytest.mark.asyncio
    async def test_a_press_waits_for_an_airborne_firing(self):
        owner = await loaded()
        out = await owner.handle({"id": "s", "method": "step", "params": {}}, NOW)
        assert out.launch is not None
        waiting = await owner.handle({"id": "p", "method": "press", "params": {"control": "add"}}, NOW)
        assert waiting.lines == [] and waiting.launch is None
        landed = await owner.completed(await out.launch.coroutine, NOW)
        answered = [line["id"] for line in landed.lines if "id" in line]
        assert answered == ["s", "p"], "the step's answer, then the press served after the landing"


# --- the notebook renderer ------------------------------------------------------------------------


def test_a_text_control_sends_what_is_typed_when_its_button_is_clicked():
    pytest.importorskip("marimo")
    from petritype.marimo_controls import build_controls, drain_controls

    class Widget:
        def __init__(self, value):
            self.value = value

    controls_map = {"say": CONTROLS["say"]}
    assert set(build_controls(controls_map)["say"].value) == {"text", "send"}

    async def run():
        ctx = RunContext(graph=build_numbers(), inbox=asyncio.Queue())
        drain_controls(controls_map, {"say": Widget({"text": "hello", "send": False})}, ctx, {})
        assert ctx.inbox.empty(), "typing alone sends nothing"
        drain_controls(controls_map, {"say": Widget({"text": "hello", "send": True})}, ctx, {})
        assert ctx.inbox.get_nowait() == Extend("In", [5])

    asyncio.run(run())
