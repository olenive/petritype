"""The protocol runner over subnet places and port places (PROTOCOL.md §12.7),
driven through ``Owner`` directly: what the shared transcript
``09_subnet_places.json`` cannot show, chiefly the rule on versions in each of its
cases, keys and filters on a port, two depths, a subnet inside a token's net and a
token's net inside a port, and the reads that wait or are served from a snapshot
while a firing touches a port.
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from petritype import petri_net  # noqa: E402
from petritype.core.events import Port  # noqa: E402
from petritype.core.executable_graph_components import (  # noqa: E402
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReturnedEdgeFromTransition,
    SubnetPlaceNode,
)
from petritype.core.serialisation import Templates  # noqa: E402
from petritype.runner.owner import Owner  # noqa: E402
from petritype.runner.process import Gauges  # noqa: E402
from petritype.runner.registry import Registry  # noqa: E402
from petritype.runtime import ControlSpec, Extend, SetTokens  # noqa: E402
from tests.protocol_nets.levels import Hero, level, levels  # noqa: E402

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 21, 9, 0, 0, tzinfo=timezone.utc)
L1, L2 = [{"place": "Level 1"}], [{"place": "Level 2"}]
construct = ExecutableGraphOperations.construct_graph


class Harness:
    """Assigns ids "1", "2", ... and lands every launch inline unless told not to."""

    def __init__(self, *factories):
        self.owner = Owner(Registry.from_factories(factories))
        self.next_id = 0
        self.pending = None

    async def send(self, method, params=None, *, land=True):
        self.next_id += 1
        request = {"id": str(self.next_id), "method": method, "params": params or {}}
        return await self._follow(await self.owner.handle(request, NOW), land)

    async def land(self):
        launch, self.pending = self.pending, None
        assert launch is not None, "nothing airborne"
        landing = await launch.coroutine
        return await self._follow(await self.owner.completed(landing, NOW), True)

    async def _follow(self, out, land):
        lines = list(out.lines)
        while out.launch is not None:
            if not land:
                self.pending = out.launch
                break
            landing = await out.launch.coroutine
            out = await self.owner.completed(landing, NOW)
            lines.extend(out.lines)
        return lines


def result(lines):
    assert "result" in lines[-1], lines
    return lines[-1]["result"]


def error(lines):
    assert "error" in lines[-1], lines
    return lines[-1]["error"]["code"]


def outer(lines, method):
    """The params of the loaded net's own notifications called ``method``."""
    return [line["params"] for line in lines if line.get("method") == method]


def inner(lines, method=None):
    """``(wrapper params, event params)`` of each ``nested`` line, of one method if given."""
    return [
        (line["params"], line["params"]["event"]["params"]) for line in lines
        if line.get("method") == "nested" and (method is None or line["params"]["event"]["method"] == method)
    ]


def sequence(lines):
    """Each line as ``method`` or ``nested:method``, and ``result`` or ``error``."""
    return [
        f"nested:{line['params']['event']['method']}" if line.get("method") == "nested"
        else line.get("method") or ("result" if "result" in line else "error")
        for line in lines
    ]


async def loaded(*factories, net, params=None):
    harness = Harness(*factories)
    await harness.send("load", {"net": net, "params": params or {}})
    return harness


async def nested_id(harness, path):
    return result(await harness.send("get_state", {"path": path}))["nested_id"]


def heartbeat(harness):
    """The params of the ``heartbeat`` the process would write now (PROTOCOL.md §7.2)."""
    return Gauges.of(harness.owner).heartbeat()["params"]


async def held_step(harness):
    """One ``step`` held while its body runs: the heartbeat meanwhile, and the
    ``step`` result once the body lands."""
    await harness.send("step", land=False)
    beat = heartbeat(harness)
    return beat, result(await harness.land())


def names_no_body(beat):
    """Both ``null``, and no ``path`` or ``nested_id``."""
    return set(beat) == {"step_started_at", "executing_transition", "marking_version", "running"} and (
        beat["step_started_at"] is None and beat["executing_transition"] is None
    )


# --- a world of knights with keys, a mutate edge on a port and a control that clears it ----------


class Knight(BaseModel):
    name: str
    role: str


KEYS = ("name", "role")


def keyed_level() -> ExecutableGraph:
    return construct([
        ListPlaceNode("Lobby", Knight, port=True, keys=KEYS),
        ListPlaceNode("Exit Hall", Knight, port=True, keys=KEYS),
    ], template="Keyed Level")


def walks(knight: Knight) -> Knight:
    return knight


def promote(tick: int, knight: Knight) -> None:
    knight.role = "Lord"


def clear_lobby(_clicked):
    return SetTokens(Port("Level", "Lobby"), [])


KNIGHTS = {knight.name: knight for knight in (
    Knight(name="Ada", role="Knight"), Knight(name="Bo", role="Mage"), Knight(name="Cy", role="Bard"),
)}


def send_to(port: str):
    """A control's command: a copy of the knight the press names, put at the level's
    ``port``. The world is closed, so a knight from outside it comes in by one of its
    own controls, or enters at its door."""
    def to_command(name: str):
        return Extend(Port("Level", port), [KNIGHTS[name].model_copy()])
    return to_command


@petri_net(
    name="keyed-world", description="Knights enter a level whose ports are keyed",
    controls=lambda: {
        "clear": ControlSpec("Level", "button", "Clear the lobby", clear_lobby),
        "admit": ControlSpec("Level", "select", "A knight into the lobby", send_to("Lobby"),
                             {"options": list(KNIGHTS)}),
        "release": ControlSpec("Level", "select", "A knight into the exit hall", send_to("Exit Hall"),
                               {"options": list(KNIGHTS)}),
    },
)
def keyed_world() -> ExecutableGraph:
    return construct([
        ListPlaceNode("Door", Knight, [Knight(name="Ada", role="Knight"), Knight(name="Bo", role="Mage")], keys=KEYS),
        ListPlaceNode("Ticks", int, [1]),
        SubnetPlaceNode("Level", keyed_level()),
        FunctionTransitionNode("Enter", walks),
        ArgumentEdgeToTransition("Door", "Enter", "knight"),
        ReturnedEdgeFromTransition("Enter", Port("Level", "Lobby")),
        FunctionTransitionNode("Promote", promote),
        ArgumentEdgeToTransition("Ticks", "Promote", "tick"),
        MutateEdge(Port("Level", "Lobby"), "Promote", "knight"),
    ])


# --- a tower: a floor, the subnet of the world, holding a room, the subnet of the floor ---------


def moves(hero: Hero) -> Hero:
    return hero


def gather(hero: Hero, guest: Hero) -> Hero:
    return hero


def room() -> ExecutableGraph:
    return construct([
        ListPlaceNode("Den", Hero, port=True),
        ListPlaceNode("Seat", Hero),
        FunctionTransitionNode("Sit", moves),
        ArgumentEdgeToTransition("Den", "Sit", "hero"),
        ReturnedEdgeFromTransition("Sit", "Seat"),
    ], template="Room")


def floor(room_stepping: str) -> ExecutableGraph:
    """``Gather`` takes from the floor's own port and from a port of its room, so its
    ``launched`` tells a change on both sides at once; it is declared first, and
    fires only once a hero waits in the room's den and another in the lobby."""
    return construct([
        ListPlaceNode("Lobby", Hero, port=True),
        SubnetPlaceNode("Room", room(), stepping=room_stepping),
        ListPlaceNode("Hall", Hero),
        FunctionTransitionNode("Gather", gather),
        ArgumentEdgeToTransition("Lobby", "Gather", "hero"),
        ArgumentEdgeToTransition(Port("Room", "Den"), "Gather", "guest"),
        ReturnedEdgeFromTransition("Gather", "Hall"),
        FunctionTransitionNode("Go In", moves),
        ArgumentEdgeToTransition("Lobby", "Go In", "hero"),
        ReturnedEdgeFromTransition("Go In", Port("Room", "Den")),
    ], template="Floor")


@petri_net(name="tower", description="Heroes enter a floor and its room")
def tower(room: str = "passive") -> ExecutableGraph:
    return construct([
        ListPlaceNode("Door", Hero, [Hero(name="Ada", fought=False), Hero(name="Bo", fought=False)]),
        SubnetPlaceNode("Floor", floor(room)),
        FunctionTransitionNode("Enter", moves),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", Port("Floor", "Lobby")),
    ])


FLOOR = [{"place": "Floor"}]
ROOM = [{"place": "Floor"}, {"place": "Room"}]


# --- a subnet inside a token's net: ships in a harbour, each with a hold ------------------------


def stow(cargo: str) -> str:
    return cargo


def scrap(ship: ExecutableGraph) -> str:
    return "scrap"


def _hold() -> ExecutableGraph:
    return construct([
        ListPlaceNode("Bay", str, port=True),
        ListPlaceNode("Deep", str),
        FunctionTransitionNode("Stow", stow),
        ArgumentEdgeToTransition("Bay", "Stow", "cargo"),
        ReturnedEdgeFromTransition("Stow", "Deep"),
    ], template="Runner Hold")


def _ship() -> ExecutableGraph:
    return construct([
        ListPlaceNode("Deck", str, ["crate"]),
        SubnetPlaceNode("Hold", hold()),
        FunctionTransitionNode("Load", stow),
        ArgumentEdgeToTransition("Deck", "Load", "cargo"),
        ReturnedEdgeFromTransition("Load", Port("Hold", "Bay")),
    ], template="Runner Ship")


hold = Templates.register("Runner Hold", _hold)
ship = Templates.register("Runner Ship", _ship)


@petri_net(name="harbour", description="A ship loads its hold, then goes for scrap")
def harbour() -> ExecutableGraph:
    return construct([
        ListPlaceNode("Harbour", ExecutableGraph, [ship()], nested=True, port=True),  # where the runner injects
        ListPlaceNode("Scrapyard", str),
        FunctionTransitionNode("Scrap", scrap),
        ArgumentEdgeToTransition("Harbour", "Scrap", "ship"),
        ReturnedEdgeFromTransition("Scrap", "Scrapyard"),
    ])


# --- a token's net inside a port: a worker hired onto a yard's bench -----------------------------


def work(job: int) -> int:
    return job


def hire(worker: ExecutableGraph) -> ExecutableGraph:
    return worker


def worker() -> ExecutableGraph:
    return construct([
        ListPlaceNode("Todo", int, [7]),
        ListPlaceNode("Done", int),
        FunctionTransitionNode("Work", work),
        ArgumentEdgeToTransition("Todo", "Work", "job"),
        ReturnedEdgeFromTransition("Work", "Done"),
    ], template="Runner Worker")


@petri_net(name="yard", description="A worker is hired onto the yard's bench and works there")
def yard_world() -> ExecutableGraph:
    yard = construct([ListPlaceNode("Bench", ExecutableGraph, nested=True, port=True)], template="Runner Yard")
    return construct([
        ListPlaceNode("Pool", ExecutableGraph, [worker()], nested=True, stepping="passive"),
        SubnetPlaceNode("Yard", yard),
        FunctionTransitionNode("Hire", hire),
        ArgumentEdgeToTransition("Pool", "Hire", "worker"),
        ReturnedEdgeFromTransition("Hire", Port("Yard", "Bench")),
    ])


@petri_net(name="levels-eve", description="Two levels, the second with heroes in it from its definition")
def levels_eve() -> ExecutableGraph:
    """The two levels, with Eve in level 2's arena and three heroes in its lobby
    from the level's definition: the world and its levels are closed, so a token
    from outside reaches only their ports, and the world, which marks none, takes
    none (since 2026-11-27)."""
    second = construct([
        ListPlaceNode("Lobby", Hero, [Hero(name=name, fought=False) for name in ("Bram", "Cato", "Dara")], port=True),
        ListPlaceNode("Arena", Hero, [Hero(name="Eve", fought=False)]),
        ListPlaceNode("Exit Hall", Hero, port=True),
    ], template="Level")
    return construct([
        ListPlaceNode("Door", Hero),
        SubnetPlaceNode("Level 1", level()),
        SubnetPlaceNode("Level 2", second),
        ListPlaceNode("Outside", Hero),
    ])


@petri_net(name="open-levels", description="The world of the two levels, declared open")
def open_levels() -> ExecutableGraph:
    """The world of ``levels``, the same places, transitions and seed, declared
    open: a closed world shields its levels (since 2026-11-27), so an inject
    addressed to a level, by path, lands at the level's port only when the world
    around it is open."""
    return construct([
        ListPlaceNode("Door", Hero, [Hero(name="Ada", fought=False)]),
        SubnetPlaceNode("Level 1", level()),
        SubnetPlaceNode("Level 2", level()),
        ListPlaceNode("Outside", Hero),
        FunctionTransitionNode("Enter", moves),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", Port("Level 1", "Lobby")),
        FunctionTransitionNode("Climb", moves),
        ArgumentEdgeToTransition(Port("Level 1", "Exit Hall"), "Climb", "hero"),
        ReturnedEdgeFromTransition("Climb", Port("Level 2", "Lobby")),
        FunctionTransitionNode("Leave", moves),
        ArgumentEdgeToTransition(Port("Level 2", "Exit Hall"), "Leave", "hero"),
        ReturnedEdgeFromTransition("Leave", "Outside"),
    ], open=True)


# --- slow bodies on a port, outside and inside ----------------------------------------------------


GATE = {"open": None}  # an asyncio.Event the slow bodies wait on, set per test


async def slow(hero: Hero) -> Hero:
    await GATE["open"].wait()
    return hero


@petri_net(name="slow-door", description="A hero enters a level slowly")
def slow_door() -> ExecutableGraph:
    return construct([
        ListPlaceNode("Door", Hero, [Hero(name="Ada", fought=False)]),
        SubnetPlaceNode("Level", level()),
        FunctionTransitionNode("Enter", slow),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", Port("Level", "Lobby")),
    ])


@petri_net(name="slow-lobby", description="A hero walks slowly out of a level's lobby")
def slow_lobby() -> ExecutableGraph:
    inside = construct([
        ListPlaceNode("Lobby", Hero, [Hero(name="Ada", fought=False), Hero(name="Bo", fought=False)], port=True),
        ListPlaceNode("Arena", Hero),
        FunctionTransitionNode("Walk In", slow),
        ArgumentEdgeToTransition("Lobby", "Walk In", "hero"),
        ReturnedEdgeFromTransition("Walk In", "Arena"),
    ], template="Slow Level")
    return construct([ListPlaceNode("Door", Hero), SubnetPlaceNode("Level", inside)])


@petri_net(name="lone-level", description="A level loaded on its own")
def lone_level() -> ExecutableGraph:
    return level()


@pytest.fixture
def gate():
    GATE["open"] = asyncio.Event()
    yield GATE["open"]
    GATE["open"].set()


# --- the tests ------------------------------------------------------------------------------------


class TestLoadingAndRecords:
    async def test_a_net_with_subnet_places_loads_and_each_subnet_is_known_from_the_load(self):
        h = await loaded(levels, net="levels")
        state = result(await h.send("get_state"))
        entry = state["marking"]["places"]["Level 1"]
        assert set(entry) == {"subnet", "ports"}
        assert entry["subnet"] == {"nested_id": entry["subnet"]["nested_id"], "path": L1, "template": "Level",
                                   "count": 0}
        by_id = result(await h.send("get_state", {"nested_id": entry["subnet"]["nested_id"]}))
        by_path = result(await h.send("get_state", {"path": L1}))
        assert by_id == by_path
        assert (by_path["net"], by_path["generation"], by_path["template"], by_path["path"]) == ("Level 1", 1, "Level", L1)
        assert by_path["nested_id"] != state["marking"]["places"]["Level 2"]["subnet"]["nested_id"]

    async def test_a_subnet_has_its_own_progress_and_log_view(self):
        h = await loaded(levels, net="levels")
        for _ in range(4):
            await h.send("step")
        inside = result(await h.send("get_state", {"path": L1}))
        assert inside["progress"]["fired_counts"] == {"Walk In": 1, "Fight": 1, "Win": 1}
        assert inside["progress"]["last_fired"] == "Win"
        world = result(await h.send("get_state"))
        assert world["progress"]["fired_counts"] == {"Enter": 1}, "the world counts its own firings alone"
        entries = result(await h.send("get_log", {"path": L1}))["entries"]
        assert [entry["transition"] for entry in entries] == ["Walk In", "Fight", "Win"]
        assert all(entry["path"] == L1 for entry in entries)

    async def test_a_reset_builds_new_subnets_and_takes_their_baselines_afresh(self):
        h = await loaded(levels, net="levels")
        before = await nested_id(h, L1)
        await h.send("step")
        await h.send("reset")
        after = await nested_id(h, L1)
        assert after != before
        assert error(await h.send("get_state", {"nested_id": before})) == "unknown_net"
        lines = await h.send("step")
        [(wrapper, fireable)] = inner(lines, "fireable")
        assert wrapper["nested_id"] == after and fireable["transitions"] == ["Walk In"]

    async def test_the_levels_example_is_served_and_runs_to_rest(self):
        from examples.subnet_places.levels import world
        h = await loaded(world, net="levels-world")
        lines = await h.send("start")
        assert outer(lines, "execution_stopped")[-1]["reason"] == "no_fireable_transitions"
        state = result(await h.send("get_state"))
        assert state["marking"]["places"]["Outside"]["count"] == 3
        assert all(entry["subnet"]["count"] == 0 for name, entry in state["marking"]["places"].items()
                   if name.startswith("Level"))


class TestTopology:
    async def test_the_outer_topology_lists_a_subnet_place_with_its_ports_and_edges_name_a_port(self):
        h = await loaded(levels, net="levels")
        topology = result(await h.send("get_state"))["topology"]
        assert topology["places"][1] == {
            "name": "Level 1", "type_name": "Level", "subnet": True, "stepping": "inner_first",
            "ports": [{"name": "Lobby", "type_name": "Hero", "serializable": True},
                      {"name": "Exit Hall", "type_name": "Hero", "serializable": True}],
        }
        assert {"place": "Level 1", "port": "Exit Hall", "transition": "Climb", "direction": "in", "kind": "take"} \
            in topology["edges"]
        assert {"place": "Level 2", "port": "Lobby", "transition": "Climb", "direction": "out"} in topology["edges"]
        assert all(place["name"] != "Arena" for place in topology["places"]), "nothing else of the subnet"

    async def test_a_subnets_own_topology_marks_its_ports_and_so_does_a_net_loaded_alone(self):
        h = await loaded(levels, lone_level, net="levels")
        places = result(await h.send("get_state", {"path": L1}))["topology"]["places"]
        assert [place.get("port") for place in places] == [True, None, True]
        await h.send("load", {"net": "lone-level"})
        places = result(await h.send("get_state"))["topology"]["places"]
        assert [place.get("port") for place in places] == [True, None, True], "every net shows its ports"

    async def test_a_subnet_place_without_a_template_has_an_empty_type_name(self):
        h = await loaded(slow_door, slow_lobby, net="slow-lobby")
        place = result(await h.send("get_state"))["topology"]["places"][1]
        assert place["type_name"] == "Slow Level"
        harness = await loaded(yard_world, net="yard")
        assert result(await harness.send("get_state"))["topology"]["places"][1]["type_name"] == "Runner Yard"

        @petri_net(name="unstamped")
        def unstamped() -> ExecutableGraph:
            return construct([SubnetPlaceNode("Inside", construct([ListPlaceNode("In", int, port=True)]))])

        h = await loaded(unstamped, net="unstamped")
        state = result(await h.send("get_state"))
        assert state["topology"]["places"][0]["type_name"] == ""
        assert "template" not in state["marking"]["places"]["Inside"]["subnet"]


class TestAddresses:
    async def test_a_subnet_step_names_its_subnet_and_the_wrong_steps_name_none(self):
        h = await loaded(levels, net="levels")
        assert result(await h.send("get_state", {"path": L1}))["net"] == "Level 1"
        for path, words in [
            ([{"place": "Door"}], "step 0 of the path: place 'Door' is not a subnet place"),
            ([{"place": "Nowhere"}], "step 0 of the path: no place named 'Nowhere'"),
            ([{"place": "Level 1", "token": "1", "net": ""}], "step 0 of the path: place 'Level 1' is a subnet place"),
            ([{"place": "Level 1"}, {"place": "Lobby"}], "step 1 of the path: place 'Lobby' is not a subnet place"),
        ]:
            lines = await h.send("get_state", {"path": path})
            assert error(lines) == "unknown_net"
            assert lines[-1]["error"]["message"].startswith(words), lines

    async def test_two_depths_of_subnets(self):
        h = await loaded(tower, net="tower", params={"room": "inner_first"})
        room_id = await nested_id(h, ROOM)
        assert result(await h.send("get_state", {"nested_id": room_id}))["path"] == ROOM
        floor_state = result(await h.send("get_state", {"path": FLOOR}))
        assert floor_state["marking"]["places"]["Room"]["subnet"]["path"] == ROOM
        assert floor_state["marking"]["places"]["Room"]["subnet"]["nested_id"] == room_id
        await h.send("step")  # Enter
        await h.send("step")  # the floor's Go In
        lines = await h.send("step")  # the room's Sit, two levels down
        assert result(lines)["path"] == ROOM and result(lines)["nested_id"] == room_id
        [(wrapper, fired)] = inner(lines, "launched")
        assert wrapper["path"] == ROOM and fired["transition"] == "Sit"

    async def test_a_subnet_inside_a_tokens_net(self):
        h = await loaded(harbour, net="harbour")
        token = result(await h.send("get_state"))["marking"]["places"]["Harbour"]["tokens"][0]
        [net] = token["nets"]
        hold_path = [{"place": "Harbour", "token": token["id"], "net": ""}, {"place": "Hold"}]
        hold = result(await h.send("get_state", {"path": hold_path}))
        assert (hold["net"], hold["template"], hold["path"]) == ("Hold", "Runner Hold", hold_path)
        ship_state = result(await h.send("get_state", {"nested_id": net["nested_id"]}))
        assert ship_state["marking"]["places"]["Hold"]["subnet"]["path"] == hold_path
        lines = await h.send("step")  # the ship's Load, into its hold's bay
        assert result(lines)["path"] == hold_path[:1]
        [(wrapper, launched)] = inner(lines, "launched")
        [(_, distributed)] = inner(lines, "distributed")
        assert distributed["produced"][0][0] == {"place": "Hold", "port": "Bay"}
        assert set(distributed["subnet_marking_versions"]) == {"Hold"}
        assert wrapper["marking_version"] == result(lines)["marking_version"], "the world sees no port of the hold"
        lines = await h.send("step")  # the hold's Stow, two levels down
        assert result(lines)["path"] == hold_path
        [(wrapper, launched)] = inner(lines, "launched")
        assert wrapper["path"] == hold_path and "outer_marking_version" in launched

    async def test_a_subnet_dies_with_the_token_whose_net_holds_it(self):
        h = await loaded(harbour, net="harbour")
        token = result(await h.send("get_state"))["marking"]["places"]["Harbour"]["tokens"][0]
        hold_path = [{"place": "Harbour", "token": token["id"], "net": ""}, {"place": "Hold"}]
        hold_id = await nested_id(h, hold_path)
        steps = [result(await h.send("step"))["transition"] for _ in range(3)]
        assert steps == ["Load", "Stow", "Scrap"]
        assert error(await h.send("get_state", {"nested_id": hold_id})) == "unknown_net"
        assert error(await h.send("get_state", {"path": hold_path})) == "unknown_net"

    async def test_a_tokens_net_inside_a_port(self):
        h = await loaded(yard_world, net="yard")
        lines = await h.send("step")  # Hire: the worker onto the yard's bench
        assert result(lines)["transition"] == "Hire"
        [produced] = outer(lines, "distributed")[0]["produced"]
        assert produced[0] == {"place": "Yard", "port": "Bench"}
        bench = result(await h.send("get_place_tokens", {"place": "Yard", "port": "Bench"}))
        [token] = bench["tokens"]
        worker_path = [{"place": "Yard"}, {"place": "Bench", "token": token["id"], "net": ""}]
        assert token["nets"][0]["path"] == worker_path, "a port's tokens' nets are reached through the subnet"
        lines = await h.send("step")  # the worker's Work, in the yard's bench
        assert result(lines)["path"] == worker_path
        assert inner(lines, "launched")[0][0]["path"] == worker_path
        worker_state = result(await h.send("get_state", {"path": worker_path}))
        assert worker_state["progress"]["fired_counts"] == {"Work": 1}
        through = result(await h.send("get_token", {"id": token["id"], "path": [{"place": "Yard"}]}))
        assert through["nets"][0]["path"] == worker_path


class TestHeartbeat:
    """While a body runs, the heartbeat names it, and names the inner net it runs
    in by the ``path`` and ``nested_id`` that its ``step`` result carries."""

    async def test_a_body_in_a_subnet_and_one_in_the_loaded_net(self):
        h = await loaded(levels, net="levels")
        assert names_no_body(heartbeat(h))
        beat, answer = await held_step(h)
        assert answer["transition"] == beat["executing_transition"] == "Enter"
        assert beat["step_started_at"] == "2026-11-21T09:00:00.000Z"
        assert "path" not in beat and "nested_id" not in beat, "a body of the loaded net"
        assert names_no_body(heartbeat(h))
        level_1 = await nested_id(h, L1)
        beat, answer = await held_step(h)
        assert (beat["executing_transition"], beat["path"], beat["nested_id"]) == ("Walk In", L1, level_1)
        assert (answer["transition"], answer["path"], answer["nested_id"]) == ("Walk In", L1, level_1)
        assert names_no_body(heartbeat(h))

    async def test_a_body_in_a_tokens_net_and_in_a_subnet_inside_it(self):
        h = await loaded(harbour, net="harbour")
        token = result(await h.send("get_state"))["marking"]["places"]["Harbour"]["tokens"][0]
        ship_path = [{"place": "Harbour", "token": token["id"], "net": ""}]
        hold_path = ship_path + [{"place": "Hold"}]
        ship_id, hold_id = token["nets"][0]["nested_id"], await nested_id(h, hold_path)
        for transition, path, net_id in [("Load", ship_path, ship_id), ("Stow", hold_path, hold_id)]:
            beat, answer = await held_step(h)
            assert (beat["executing_transition"], beat["path"], beat["nested_id"]) == (transition, path, net_id)
            assert (answer["transition"], answer["path"], answer["nested_id"]) == (transition, path, net_id)
            assert names_no_body(heartbeat(h))
        beat, answer = await held_step(h)
        assert answer["transition"] == beat["executing_transition"] == "Scrap"
        assert "path" not in beat and "nested_id" not in beat, "a body of the loaded net"
        assert names_no_body(heartbeat(h))

    async def test_a_body_in_a_tokens_net_inside_a_port(self):
        h = await loaded(yard_world, net="yard")
        await h.send("step")  # Hire: the worker onto the yard's bench
        [token] = result(await h.send("get_place_tokens", {"place": "Yard", "port": "Bench"}))["tokens"]
        worker_path = [{"place": "Yard"}, {"place": "Bench", "token": token["id"], "net": ""}]
        beat, answer = await held_step(h)
        assert (beat["executing_transition"], beat["path"], beat["nested_id"]) == (
            "Work", worker_path, token["nets"][0]["nested_id"],
        )
        assert (answer["path"], answer["nested_id"]) == (beat["path"], beat["nested_id"])


class TestReferences:
    async def test_a_firing_names_a_port_by_the_object_and_so_does_the_log(self):
        h = await loaded(levels, net="levels")
        enter = await h.send("step")
        [launched], [distributed] = outer(enter, "launched"), outer(enter, "distributed")
        assert launched["consumed"] == [["Door", "1"]]
        assert distributed["produced"][0][:2] == [{"place": "Level 1", "port": "Lobby"}, "1"]
        for _ in range(3):
            await h.send("step")
        climb = await h.send("step")
        assert outer(climb, "launched")[0]["consumed"][0][0] == {"place": "Level 1", "port": "Exit Hall"}
        log = result(await h.send("get_log"))["entries"]
        assert log[0]["produced"][0][0] == {"place": "Level 1", "port": "Lobby"}
        assert log[-1]["consumed"][0][0] == {"place": "Level 1", "port": "Exit Hall"}
        assert "path" not in log[-1], "the world's firing is the world's entry"

    async def test_added_removed_and_mutated_name_a_port_with_port_beside_place(self):
        h = await loaded(keyed_world, net="keyed-world")
        await h.send("disable", {"transition": "Promote"})
        await h.send("step")
        await h.send("step")
        await h.send("disable", {"transition": "Enter"})
        await h.send("enable", {"transition": "Promote"})
        lines = await h.send("step")  # Promote: Ada, at the front of the lobby, becomes a lord
        [mutated] = outer(lines, "mutated")
        assert (mutated["place"], mutated["port"], mutated["keys"]) == ("Level", "Lobby", {"name": "Ada", "role": "Lord"})
        assert set(mutated["subnet_marking_versions"]) == {"Level"}
        lines = await h.send("press", {"control": "clear"})
        removed = outer(lines, "removed")
        assert [(r["place"], r["port"]) for r in removed] == [("Level", "Lobby")] * 2
        lines = await h.send("press", {"control": "release", "value": "Cy"})
        [added] = outer(lines, "added")
        assert (added["place"], added["port"], added["keys"]) == ("Level", "Exit Hall", {"name": "Cy", "role": "Bard"})

    async def test_hidden_names_a_port_under_its_subnets_filter(self):
        h = await loaded(keyed_world, net="keyed-world")
        mages = {"clauses": [{"key": "role", "values": ["Mage"]}]}
        await h.send("set_filter", {"filter": mages, "path": [{"place": "Level"}]})
        lines = await h.send("press", {"control": "admit", "value": "Ada"})
        [hidden] = outer(lines, "hidden")
        assert (hidden["kind"], hidden["place"], hidden["port"]) == ("added", "Level", "Lobby")
        assert set(hidden["subnet_marking_versions"]) == {"Level"}
        lines = await h.send("press", {"control": "admit", "value": "Bo"})
        assert outer(lines, "added")[0]["port"] == "Lobby"


class TestState:
    async def test_a_subnet_places_entry_holds_its_subnet_and_its_ports_under_the_token_limit(self):
        h = await loaded(levels_eve, net="levels-eve")
        entry = result(await h.send("get_state", {"token_limit": 2}))["marking"]["places"]["Level 2"]
        assert entry["subnet"]["count"] == 4, "its ports included, and its own places"
        assert entry["ports"]["Lobby"]["count"] == 3 and len(entry["ports"]["Lobby"]["tokens"]) == 2
        assert entry["ports"]["Exit Hall"] == {"count": 0, "tokens": []}

    async def test_a_port_is_projected_by_its_subnets_filter_in_both_views(self):
        h = await loaded(keyed_world, net="keyed-world")
        await h.send("step")
        await h.send("step")
        mages = {"clauses": [{"key": "role", "values": ["Mage"]}]}
        await h.send("set_filter", {"filter": mages, "path": [{"place": "Level"}]})
        lobby = result(await h.send("get_state"))["marking"]["places"]["Level"]["ports"]["Lobby"]
        assert (lobby["count"], lobby["matching"], [t["keys"]["name"] for t in lobby["tokens"]]) == (2, 1, ["Bo"])
        assert "matching" not in result(await h.send("get_state"))["marking"]["places"]["Door"]
        inside = result(await h.send("get_state", {"path": [{"place": "Level"}]}))["marking"]["places"]["Lobby"]
        assert (inside["count"], inside["matching"]) == (2, 1)
        found = result(await h.send("query"))["matches"]
        assert [(m["place"], m.get("port"), m["token"]["id"]) for m in found] == [
            ("Ticks", None, "3"), ("Level", "Lobby", lobby["tokens"][0]["id"]),
        ], "the world's own places by its own filter, none; the port by its subnet's"
        everyone = result(await h.send("query", {"filter": {}}))["matches"]
        assert [(m["place"], m.get("port")) for m in everyone] == [
            ("Ticks", None), ("Level", "Lobby"), ("Level", "Lobby"),
        ], "a filter of its own, ports included"


class TestRequests:
    async def test_a_ports_tokens_are_read_and_put_both_ways_with_the_subnets_ids(self):
        """The world's own ``Enter`` puts a hero into level 1's lobby by the pair, and an
        inject by path, at the subnet, puts another, the world being declared open. The
        closed world refuses both injects (``closed_net``): by the pair since it marks
        no port (``tests/test_runner_closed_nets.py``), and by path since it shields
        its levels (since 2026-11-27)."""
        closed = await loaded(levels, net="levels")
        hero = {"name": "Cato", "fought": False}
        assert error(await closed.send("inject", {"place": "Level 1", "port": "Lobby", "token": hero})) == "closed_net"
        assert error(await closed.send("inject", {"place": "Lobby", "path": L1, "token": hero})) == "closed_net"
        h = await loaded(open_levels, net="open-levels")
        await h.send("step")  # Enter: Ada, through the pair
        by_place = result(await h.send("inject", {"place": "Lobby", "path": L1, "token": hero}))
        assert by_place["id"] == "2" and by_place["path"] == L1, "one counter, the subnet's: Ada is its 1"
        outside = result(await h.send("get_place_tokens", {"place": "Level 1", "port": "Lobby"}))
        inside = result(await h.send("get_place_tokens", {"place": "Lobby", "path": L1}))
        assert outside["tokens"] == inside["tokens"] and [t["id"] for t in inside["tokens"]] == ["1", "2"]
        assert (outside["place"], outside["port"]) == ("Level 1", "Lobby") and "port" not in inside
        # Each answered with the version of the net it addressed.
        assert outside["marking_version"] == result(await h.send("get_state"))["marking_version"]
        assert inside["marking_version"] == result(await h.send("get_state", {"path": L1}))["marking_version"]

    async def test_a_ports_token_is_read_through_its_subnet(self):
        h = await loaded(levels, net="levels")
        await h.send("step")  # Enter: Ada, the world's token 1, is the level's token 1 now
        assert error(await h.send("get_token", {"id": "1"})) == "unknown_token"
        value = result(await h.send("get_token", {"id": "1", "path": L1}))
        assert (value["place"], value["kind"], value["value"]) == ("Lobby", "json", {"name": "Ada", "fought": False})

    async def test_the_errors_of_naming_a_port(self):
        h = await loaded(levels, net="levels")
        hero = {"name": "Bram", "fought": False}
        for method, params, code in [
            ("get_place_tokens", {"place": "Level 1"}, "invalid_params"),
            ("inject", {"place": "Level 1", "token": hero}, "invalid_params"),
            ("get_place_tokens", {"place": "Level 1", "port": "Arena"}, "unknown_place"),
            ("get_place_tokens", {"place": "Level 1", "port": "Kitchen"}, "unknown_place"),
            ("inject", {"place": "Door", "port": "Lobby", "token": hero}, "unknown_place"),
            ("get_place_tokens", {"place": "Nowhere", "port": "Lobby"}, "unknown_place"),
            ("get_place_tokens", {"place": "Level 1", "port": 3}, "invalid_params"),
            ("get_place_tokens", {"place": "Level 1", "port": "Lobby", "path": L1}, "unknown_place"),
            ("inject", {"place": "Level 1", "port": "Lobby", "token": {"name": "Bram"}}, "invalid_token"),
        ]:
            assert error(await h.send(method, params)) == code, (method, params)

    async def test_filters_and_distinct_name_a_port_by_the_object(self):
        h = await loaded(keyed_world, net="keyed-world")
        await h.send("step")
        lobby = {"place": "Level", "port": "Lobby"}
        found = result(await h.send("query", {"filter": {"places": [lobby]}}))["matches"]
        assert [(m["place"], m["port"]) for m in found] == [("Level", "Lobby")]
        assert result(await h.send("query", {"filter": {"places": ["Level"]}}))["count"] == 0, "it holds none"
        found = result(await h.send("query", {"filter": {"prefix": "Lob"}}))["matches"]
        assert [m.get("port") for m in found] == ["Lobby"], "a port by its own name"
        assert result(await h.send("distinct", {"key": "name", "places": [lobby]}))["values"] == ["Ada"]
        assert result(await h.send("distinct", {"key": "name"}))["values"] == ["Bo", "Ada"]
        await h.send("set_filter", {"filter": {"places": [lobby, "Door"]}})
        for bad, code in [
            ({"places": [{"place": "Level", "port": "Kitchen"}]}, "unknown_place"),
            ({"places": [{"place": "Door", "port": "Lobby"}]}, "unknown_place"),
            ({"places": [{"place": "Level"}]}, "invalid_params"),
            ({"places": [{"place": "Level", "port": 7}]}, "invalid_params"),
            ({"places": "Level"}, "invalid_params"),
        ]:
            assert error(await h.send("query", {"filter": bad})) == code, bad
        other_keys = {"places": [{"place": "Level", "port": "Lobby", "note": "ignored"}]}
        assert result(await h.send("query", {"filter": other_keys}))["count"] == 1, "other keys ignored (§2)"
        assert error(await h.send("distinct", {"key": "name", "places": [{"place": 1, "port": "x"}]})) == "invalid_params"


class TestTwoViews:
    async def test_a_change_is_told_once_by_the_net_through_which_it_was_made(self):
        h = await loaded(levels, net="levels")
        lines = await h.send("step")  # Enter, told by the world
        assert sequence(lines) == ["launched", "distributed", "fireable", "nested:fireable", "result"]
        lines = await h.send("step")  # Walk In, told by the level
        assert sequence(lines) == ["nested:launched", "nested:distributed", "nested:fireable", "result"]

    async def test_after_a_change_of_a_port_both_nets_fireable_sets_are_compared_the_outer_first(self):
        h = await loaded(open_levels, net="open-levels")  # open, so that an inject by path reaches a level
        lines = await h.send("inject", {"place": "Exit Hall", "path": L2, "token": {"name": "Cy", "fought": True}})
        assert sequence(lines) == ["nested:added", "fireable", "result"], "the level's set did not change"
        assert outer(lines, "fireable")[0]["transitions"] == ["Enter", "Leave"]
        lines = await h.send("step")  # Enter, the world's own firing, at level 1's lobby by the pair
        assert sequence(lines) == ["launched", "distributed", "fireable", "nested:fireable", "result"], "both changed"
        lines = await h.send("inject", {"place": "Lobby", "path": L2, "token": {"name": "Di", "fought": False}})
        assert sequence(lines) == ["nested:added", "nested:fireable", "result"], "the world's set did not change"

    async def test_a_nets_fireable_precedes_the_nets_inside_it_at_two_depths(self):
        h = await loaded(tower, net="tower", params={"room": "inner_first"})
        await h.send("step")  # Enter
        lines = await h.send("step")  # the floor's Go In, into the room's den
        fireables = inner(lines, "fireable")
        assert [wrapper["path"] for wrapper, _ in fireables] == [FLOOR, ROOM]


class TestVersions:
    async def test_told_by_the_outer_net_the_subnets_version_rides_along_and_it_goes_on_from_there(self):
        h = await loaded(levels, net="levels")
        level_before = result(await h.send("get_state", {"path": L1}))["marking_version"]
        lines = await h.send("step")  # Enter, into level 1's lobby
        [distributed] = outer(lines, "distributed")
        assert distributed["subnet_marking_versions"] == {"Level 1": level_before + 1}
        [(_, fireable)] = inner(lines, "fireable")
        assert fireable["marking_version"] == level_before + 1, "the level's own lines carry on from it"
        lines = await h.send("step")  # Walk In, told by the level
        [(_, launched)] = inner(lines, "launched")
        assert launched["marking_version"] == level_before + 2, "no version of the level skipped"

    async def test_told_by_the_subnet_the_outer_version_rides_along_and_on_the_wrapper(self):
        h = await loaded(levels, net="levels")
        await h.send("step")  # Enter
        world_before = result(await h.send("get_state"))["marking_version"]
        lines = await h.send("step")  # Walk In, out of the lobby
        [(wrapper, launched)] = inner(lines, "launched")
        assert launched["outer_marking_version"] == world_before + 1 == wrapper["marking_version"]
        [(wrapper, distributed)] = inner(lines, "distributed")
        assert "outer_marking_version" not in distributed and wrapper["marking_version"] == world_before + 1
        assert result(await h.send("get_state"))["marking_version"] == world_before + 1

    async def test_one_notification_may_carry_both_fields(self):
        h = await loaded(tower, net="tower")  # the room passive: its den keeps what it is given
        await h.send("step")  # Enter Ada
        lines = await h.send("step")  # the floor's Go In: out of its lobby, into the room's den
        [(_, launched)] = inner(lines, "launched")
        [(_, distributed)] = inner(lines, "distributed")
        assert set(launched) >= {"outer_marking_version"} and "subnet_marking_versions" not in launched
        assert set(distributed["subnet_marking_versions"]) == {"Room"} and "outer_marking_version" not in distributed
        await h.send("step")  # Enter Bo
        lines = await h.send("step")  # the floor's Gather: from its lobby and from the room's den at once
        [(wrapper, launched)] = inner(lines, "launched")
        assert launched["transition"] == "Gather"
        assert set(launched["subnet_marking_versions"]) == {"Room"} and "outer_marking_version" in launched
        assert wrapper["marking_version"] == launched["outer_marking_version"], "the world is the floor's outer net"
        room_state = result(await h.send("get_state", {"path": ROOM}))
        assert room_state["marking_version"] == launched["subnet_marking_versions"]["Room"]

    async def test_deeper_down_the_wrapper_carries_the_loaded_nets_version_unchanged(self):
        h = await loaded(tower, net="tower", params={"room": "inner_first"})
        await h.send("step")  # Enter
        await h.send("step")  # Go In
        world = result(await h.send("get_state"))["marking_version"]
        floor_version = result(await h.send("get_state", {"path": FLOOR}))["marking_version"]
        lines = await h.send("step")  # the room's Sit, out of its den, which the floor sees
        [(wrapper, launched)] = inner(lines, "launched")
        assert launched["outer_marking_version"] == floor_version + 1
        assert wrapper["marking_version"] == world, "the world does not see the den"
        assert result(await h.send("get_state", {"path": FLOOR}))["marking_version"] == floor_version + 1

    async def test_a_reader_that_follows_one_net_sees_a_change_told_by_the_other_as_a_gap(self):
        h = await loaded(open_levels, net="open-levels")  # open, so that an inject by path reaches a level
        world_lines, level_lines = [], []
        level_id = await nested_id(h, L1)
        sends = [("step", {})] * 5  # Enter, Walk In, Fight, Win, Climb; then one told by the level
        sends.append(("inject", {"place": "Lobby", "path": L1, "token": {"name": "Bram", "fought": False}}))
        for method, params in sends:
            for line in await h.send(method, params):
                if line.get("method") == "nested" and line["params"]["nested_id"] == level_id:
                    level_lines.append(line["params"]["event"]["params"])
                elif "method" in line and line["method"] != "nested":
                    world_lines.append(line["params"])

        def steps(stamps):
            versions = [params["marking_version"] for params in stamps if "transitions" not in params]
            return [after - before for before, after in zip(versions, versions[1:])]

        # Walk In and Win moved the world, told by the level; Enter and Climb the
        # level, told by the world: each reader meets the next value more than one on.
        assert steps(world_lines) == [1, 3, 1]
        assert max(steps(level_lines)) > 1

    async def test_a_reader_that_follows_both_nets_sees_no_version_skipped_in_either(self):
        held = {"world": 0, "level": 0}
        h2 = await loaded(levels, net="levels")
        level_id = await nested_id(h2, L1)
        for _ in range(5):
            for line in await h2.send("step"):
                if line.get("method") == "nested" and line["params"]["nested_id"] == level_id:
                    event = line["params"]["event"]
                    if event["method"] != "fireable":
                        assert event["params"]["marking_version"] == held["level"] + 1
                        held["level"] = event["params"]["marking_version"]
                    if "outer_marking_version" in event["params"]:
                        assert event["params"]["outer_marking_version"] == held["world"] + 1
                        held["world"] = event["params"]["outer_marking_version"]
                elif line.get("method") in ("launched", "distributed", "added", "removed", "mutated", "hidden"):
                    assert line["params"]["marking_version"] == held["world"] + 1
                    held["world"] = line["params"]["marking_version"]
                    if "Level 1" in line["params"].get("subnet_marking_versions", {}):
                        assert line["params"]["subnet_marking_versions"]["Level 1"] == held["level"] + 1
                        held["level"] = line["params"]["subnet_marking_versions"]["Level 1"]
        assert held["world"] == result(await h2.send("get_state"))["marking_version"]
        assert held["level"] == result(await h2.send("get_state", {"path": L1}))["marking_version"]


class TestReadsDuringAFiring:
    async def test_a_subnet_is_read_after_the_landing_of_an_outer_firing_on_its_port(self, gate):
        h = await loaded(slow_door, net="slow-door")
        lines = await h.send("step", land=False)
        assert outer(lines, "launched")[0]["consumed"] == [["Door", "1"]]
        assert await h.send("get_state", {"path": [{"place": "Level"}]}) == [], "it waits"
        state = result(await h.send("get_state"))
        assert state["transitions"]["Enter"]["in_flight"] is True
        assert state["marking"]["places"]["Door"]["count"] == 0
        assert state["marking"]["places"]["Level"]["ports"]["Lobby"]["count"] == 0
        gate.set()
        lines = await h.land()
        waited = [line for line in lines if line.get("id") == "3"]
        assert waited and waited[0]["result"]["marking"]["places"]["Lobby"]["count"] == 1

    async def test_the_outer_net_reads_a_port_as_a_firing_of_the_subnet_leaves_it(self, gate):
        h = await loaded(slow_lobby, net="slow-lobby")
        lines = await h.send("step", land=False)  # the level's Walk In, out of its own lobby
        [(wrapper, launched)] = inner(lines, "launched")
        state = result(await h.send("get_state"))
        assert state["marking_version"] == launched["outer_marking_version"] == wrapper["marking_version"]
        lobby = state["marking"]["places"]["Level"]["ports"]["Lobby"]
        assert lobby["count"] == 1 and [t["id"] for t in lobby["tokens"]] == ["2"], "Ada is out, Bo waits"
        assert state["marking"]["places"]["Level"]["subnet"]["count"] == 1
        page = result(await h.send("get_place_tokens", {"place": "Level", "port": "Lobby"}))
        assert page["count"] == 1 and page["marking_version"] == state["marking_version"]
        gate.set()
        await h.land()
        after = result(await h.send("get_state"))["marking"]["places"]["Level"]
        assert after["ports"]["Lobby"]["count"] == 1 and after["subnet"]["count"] == 2


class TestSavedForms:
    async def test_a_token_whose_net_holds_a_subnet_place_has_its_subnet_as_a_list_of_one(self):
        h = await loaded(harbour, net="harbour")
        token = result(await h.send("get_state"))["marking"]["places"]["Harbour"]["tokens"][0]
        value = result(await h.send("get_token", {"id": token["id"]}))
        assert value["kind"] == "json"
        assert value["value"]["template"] == "Runner Ship"
        [hold_form] = value["value"]["marking"]["Hold"]
        assert hold_form == {"template": "Runner Hold", "marking": {"Bay": [], "Deep": []}, "params": {},
                             "enabled": {"Stow": True}}
        hold_form["marking"]["Bay"] = ["barrel"]
        lines = await h.send("inject", {"place": "Harbour", "token": value["value"]})
        new = result(lines)["id"]
        hold_path = [{"place": "Harbour", "token": new, "net": ""}, {"place": "Hold"}]
        bay = result(await h.send("get_place_tokens", {"place": "Bay", "path": hold_path}))
        assert [t["label"] for t in bay["tokens"]] == ["'barrel'"]
        ship_path = hold_path[:1]
        outside = result(await h.send("get_place_tokens", {"place": "Hold", "port": "Bay", "path": ship_path}))
        assert outside["tokens"] == bay["tokens"]
