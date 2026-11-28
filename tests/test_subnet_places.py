"""Subnet places and port places (PROTOCOL.md §12.7, ``SubnetPlaceNode``, ``Port``).

A subnet place holds a net of its own, its subnet, for the outer net's whole life,
and the outer net reaches it only through the places the subnet's definition marks
as ports, each named from outside by the pair ``Port(subnet place, port)``. A port
has one list of tokens and one set of ids, the subnet's, whichever net looks, and a
change is told once, by the net through which it was made.

These tests are, in turn: every input edge kind and every way of choosing an output
on a port; ids and keys across the border both ways; failure; the in-flight rule;
stepping under each rule, two subnets with different rules and a subnet in a
subnet; tokens that carry nets in a port; outside input; the events; naming and the
program's reading and changing; saving, restoring and copying at one and two depths;
the build errors; the concurrent runtime; the Graphviz symbol; and the example. They
follow the Rust core's ``tests/subnet.rs`` where the two cover the same ground, so the
two runtimes behave alike. The protocol runner's service of subnet places is
``tests/test_runner_subnets.py``.
"""

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Optional, Union

import pytest
from pydantic import BaseModel

from petritype.core.events import Added, Failed, Fired, InnerFiring, Mutated, Nested, PathStep, Port, Removed
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ClosedNetError,
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MapPlaceNames,
    MutateEdge,
    MutateWhereEdge,
    NestedNets,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    SubnetHeldError,
    SubnetPlaceNode,
    SurveyEdge,
    TakeWhereEdge,
    TokenIds,
    TransitionFailedError,
)
from petritype.core.keys import Filter, Query
from petritype.core.serialisation import NetJson, Templates
from petritype.runtime import Extend, ExecutionMode, RunContext, Runner, SetTokens

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def run(coroutine):
    return asyncio.run(coroutine)


# --- heroes, levels and worlds -----------------------------------------------------------------


class Hero(BaseModel):
    name: str
    role: str = "Knight"
    hp: int = 10


class Note(BaseModel):
    text: str


def hero(name: str, role: str = "Knight") -> Hero:
    return Hero(name=name, role=role)


def same(hero: Hero) -> Hero:
    return hero


KEYS = ("name", "role", "hp")
LOBBY = Port("Level 3", "Lobby")
HALL = Port("Level 3", "Exit Hall")


def level(walk: bool = False, lobby=(), template: Optional[str] = None, hall=()) -> ExecutableGraph:
    """A level as its definition builds it: ``Lobby`` and ``Exit Hall`` are ports,
    keyed by name, role and hit points, and ``Arena`` is the level's own. With
    ``walk``, ``Walk`` takes the first hero in the lobby into the arena and ``Out``
    the first in the arena to the exit hall."""
    nodes = [
        ListPlaceNode("Lobby", Hero, list(lobby), port=True, keys=KEYS),
        ListPlaceNode("Arena", Hero),
        ListPlaceNode("Exit Hall", Hero, list(hall), port=True, keys=KEYS),
    ]
    if walk:
        nodes += [
            FunctionTransitionNode("Walk", same),
            ArgumentEdgeToTransition("Lobby", "Walk", "hero"),
            ReturnedEdgeFromTransition("Walk", "Arena"),
            FunctionTransitionNode("Out", same),
            ArgumentEdgeToTransition("Arena", "Out", "hero"),
            ReturnedEdgeFromTransition("Out", "Exit Hall"),
        ]
    return ExecutableGraphOperations.construct_graph(nodes, template=template)


def world(*nodes, walk: bool = False, lobby=(), door=(), restore: bool = False, stepping: str = "inner_first",
          hall=(), open: bool = False):
    """A world holding one level as ``Level 3``, with a ``Door`` and a ``Gone`` of
    its own, and the given transitions and edges. Closed unless ``open``: from
    outside, only an open world takes a write at a port of its level, named by
    the pair."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", Hero, list(door)),
        SubnetPlaceNode("Level 3", level(walk, lobby, hall=hall), stepping=stepping),
        ListPlaceNode("Gone", Hero),
        *nodes,
    ], record_events=True, restore_tokens_on_failure=restore, open=open)


def names(graph: ExecutableGraph, place) -> list[str]:
    return [token.name for token in graph.place_named(place).tokens]


def ids(graph: ExecutableGraph, place) -> list[int]:
    return list(graph.place_named(place).token_ids)


def same_from_both(graph: ExecutableGraph, port: Port) -> None:
    """A port reads the same from both nets: one place, so the same tokens and the
    same ids, in the same order."""
    inside = graph.subnet(port.place).place_named(port.port)
    assert graph.place_named(port) is inside, "one place, one list"
    assert ids(graph, port) == list(inside.token_ids)


def fire(graph: ExecutableGraph, name: str):
    """Fire the transition ``name`` of ``graph`` alone, if it is fireable."""
    def pinned(_graph, fireable):
        return next((t for t in fireable if t.name == name), None)
    return run(ExecutableGraphOperations.fire_one_transition(
        graph, *ExecutableGraphOperations.maps(graph), transition_selector=pinned, descend=False,
    ))


def deep_step(graph: ExecutableGraph):
    return run(ExecutableGraphOperations.fire_one_transition(graph, *ExecutableGraphOperations.maps(graph)))


def until_rest(graph: ExecutableGraph, budget: int = 200) -> list:
    """Deep steps until nothing fires; what each fired."""
    fired = []
    for _ in range(budget):
        step = deep_step(graph)
        if step is None:
            return fired
        fired.append(step)
    raise AssertionError("never came to rest")


def told(event) -> tuple:
    """A firing's record without its duration."""
    return event.transition, event.consumed, event.produced


# --- every input edge kind on a port -----------------------------------------------------------


def test_place_finds_a_port_by_its_pair_and_refuses_a_subnet_place_named_alone():
    w = world(lobby=(hero("Ada"),))
    assert [token.name for token in w.place(LOBBY).tokens] == ["Ada"]
    assert w.place(LOBBY) is w.place_named(LOBBY)
    with pytest.raises(KeyError, match="subnet place, which holds no tokens of its own"):
        w.place("Level 3")
    with pytest.raises(KeyError, match="the net has no place"):
        w.place(Port("Level 3", "Cellar"))


def test_a_take_edge_takes_the_front_of_a_port():
    w = world(
        FunctionTransitionNode("Leave", same),
        ArgumentEdgeToTransition(LOBBY, "Leave", "hero"),
        ReturnedEdgeFromTransition("Leave", "Gone"),
        lobby=[hero("Ada"), hero("Bo")],
    )
    ada = ids(w, LOBBY)[0]
    assert fire(w, "Leave") == "Leave"
    assert names(w, "Gone") == ["Ada"]
    assert names(w, LOBBY) == ["Bo"]
    same_from_both(w, LOBBY)
    gone = ids(w, "Gone")[0]
    assert [told(event) for event in w.drain_events()] == [("Leave", ((LOBBY, ada),), (("Gone", gone),))]


def everyone(heroes: list[Hero]) -> list[Hero]:
    return heroes


def test_take_all_and_drain_take_every_token_of_a_port():
    w = world(
        FunctionTransitionNode("All Out", everyone),
        ArgumentEdgeToTransition(LOBBY, "All Out", "heroes"),
        ReturnedEdgeFromTransition("All Out", "Gone"),
        FunctionTransitionNode("Sweep", everyone),
        DrainEdge(HALL, "Sweep", "heroes"),
        ReturnedEdgeFromTransition("Sweep", "Gone"),
        lobby=[hero("Ada"), hero("Bo")],
    )
    assert fire(w, "All Out") == "All Out"
    assert names(w, "Gone") == ["Ada", "Bo"] and names(w, LOBBY) == []
    # A drain fires on an empty port too, and takes whatever is there.
    assert fire(w, "Sweep") == "Sweep"
    w.subnet("Level 3").place_named("Exit Hall").tokens.append(hero("Cy"))
    assert fire(w, "Sweep") == "Sweep"
    assert names(w, "Gone") == ["Ada", "Bo", "Cy"] and names(w, HALL) == []
    same_from_both(w, LOBBY)
    same_from_both(w, HALL)


def test_a_keyed_take_finds_its_token_in_a_port():
    w = world(
        FunctionTransitionNode("Pick a Mage", same),
        TakeWhereEdge(LOBBY, "Pick a Mage", "hero", where=Filter.where(role="Mage")),
        ReturnedEdgeFromTransition("Pick a Mage", "Gone"),
        lobby=[hero("Ada"), hero("Bo", "Mage"), hero("Cy")],
    )
    ada, bo, cy = ids(w, LOBBY)
    assert fire(w, "Pick a Mage") == "Pick a Mage"
    assert names(w, "Gone") == ["Bo"]
    assert ids(w, LOBBY) == [ada, cy], "every other token where it was, with its id"
    assert fire(w, "Pick a Mage") is None, "no mage left"


def look(tick: int, first: Hero, hall: list[Hero]) -> int:
    return len(hall)


def test_snapshot_and_survey_read_a_port_and_leave_it_alone():
    w = world(
        ListPlaceNode("Ticks", int, [1]),
        ListPlaceNode("Counts", int),
        FunctionTransitionNode("Look", look),
        ArgumentEdgeToTransition("Ticks", "Look", "tick"),
        SnapshotEdge(LOBBY, "Look", "first"),
        SurveyEdge(HALL, "Look", "hall"),
        ReturnedEdgeFromTransition("Look", "Counts"),
        lobby=[hero("Ada")],
    )
    before = ids(w, LOBBY)
    assert fire(w, "Look") == "Look"
    assert w.place_named("Counts").tokens == [0], "the survey read an empty port"
    assert ids(w, LOBBY) == before
    events = w.drain_events()
    assert [told(event)[1] for event in events] == [(("Ticks", 1),)], "nothing taken from a port"


def heal(tick: int, hero: Hero, mage: Hero) -> None:
    hero.hp = 99
    mage.hp = 98


def test_mutating_edges_change_a_token_where_it_lies_in_the_port():
    w = world(
        ListPlaceNode("Ticks", int, [1]),
        FunctionTransitionNode("Heal", heal),
        ArgumentEdgeToTransition("Ticks", "Heal", "tick"),
        MutateEdge(LOBBY, "Heal", "hero"),
        MutateWhereEdge(HALL, "Heal", "mage", where=Filter.where(role="Mage")),
        lobby=[hero("Ada")],
    )
    w.subnet("Level 3").place_named("Exit Hall").tokens.extend([hero("Bo"), hero("Cy", "Mage")])
    TokenIds.resync(w.subnet("Level 3"))
    w.drain_events()
    ada, (bo, cy) = ids(w, LOBBY)[0], ids(w, HALL)
    assert fire(w, "Heal") == "Heal"
    assert [h.hp for h in w.place_named(LOBBY).tokens] == [99]
    assert [h.hp for h in w.place_named(HALL).tokens] == [10, 98]
    assert ids(w, LOBBY) == [ada] and ids(w, HALL) == [bo, cy], "the same tokens, the same ids"
    events = w.drain_events()
    assert events[:2] == [Mutated(LOBBY, ada), Mutated(HALL, cy)]
    assert isinstance(events[2], Fired)


# --- every way of choosing an output onto a port -----------------------------------------------


def split(a: Hero, b: Hero) -> tuple[Hero, Hero]:
    return a, b


def route(hero: Hero) -> Union[Hero, Note]:
    return hero if hero.role != "Bard" else Note(text=hero.name)


def send_to_the_hall(hero: Hero) -> dict:
    return {HALL: hero}


def test_every_way_of_choosing_an_output_can_choose_a_port():
    w = world(
        ListPlaceNode("Notes", Note),
        ListPlaceNode("Pairs", Hero, [hero("Ada"), hero("Bo")]),
        FunctionTransitionNode("Split", split),
        ArgumentEdgeToTransition("Pairs", "Split", "a"),
        ArgumentEdgeToTransition("Pairs", "Split", "b"),
        ReturnedEdgeFromTransition("Split", LOBBY, 0),
        ReturnedEdgeFromTransition("Split", HALL, 1),
        FunctionTransitionNode("Route", route),
        ArgumentEdgeToTransition("Door", "Route", "hero"),
        ReturnedEdgeFromTransition("Route", LOBBY),
        ReturnedEdgeFromTransition("Route", "Notes"),
        FunctionTransitionNode("Send", same, output_distribution_function=send_to_the_hall),
        ArgumentEdgeToTransition("Gone", "Send", "hero"),
        ReturnedEdgeFromTransition("Send", LOBBY),
        ReturnedEdgeFromTransition("Send", HALL),
        door=[hero("Cy"), hero("Di", "Bard")],
    )
    w.place_named("Gone").tokens.append(hero("Ed"))
    assert fire(w, "Split") == "Split"  # by position
    assert names(w, LOBBY) == ["Ada"] and names(w, HALL) == ["Bo"]
    assert fire(w, "Route") == "Route"  # by type: a hero to the lobby
    assert fire(w, "Route") == "Route"  # by type: a bard's note to Notes
    assert names(w, LOBBY) == ["Ada", "Cy"] and w.place_named("Notes").tokens == [Note(text="Di")]
    assert fire(w, "Send") == "Send"  # by an output distribution function that names a port
    assert names(w, HALL) == ["Bo", "Ed"]
    same_from_both(w, LOBBY)
    same_from_both(w, HALL)


def test_two_ports_with_one_name_are_two_places():
    w = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Pairs", Hero, [hero("Ada"), hero("Bo")]),
        SubnetPlaceNode("Level 1", level()),
        SubnetPlaceNode("Level 2", level()),
        FunctionTransitionNode("Split", split),
        ArgumentEdgeToTransition("Pairs", "Split", "a"),
        ArgumentEdgeToTransition("Pairs", "Split", "b"),
        ReturnedEdgeFromTransition("Split", Port("Level 1", "Lobby"), 0),
        ReturnedEdgeFromTransition("Split", Port("Level 2", "Lobby"), 1),
    ])
    assert fire(w, "Split") == "Split"
    assert names(w, Port("Level 1", "Lobby")) == ["Ada"]
    assert names(w, Port("Level 2", "Lobby")) == ["Bo"]


# --- ids and keys across the border ------------------------------------------------------------


def test_a_token_keeps_one_id_across_the_border_both_ways():
    w = world(
        FunctionTransitionNode("Enter", same),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", LOBBY),
        FunctionTransitionNode("Leave", same),
        ArgumentEdgeToTransition(HALL, "Leave", "hero"),
        ReturnedEdgeFromTransition("Leave", "Gone"),
        walk=True, door=[hero("Ada")],
    )
    level_id = w.subnet("Level 3").net_id
    inside = (PathStep("Level 3"),)
    assert until_rest(w) == [
        "Enter", InnerFiring(inside, "Walk"), InnerFiring(inside, "Out"), "Leave",
    ]
    assert names(w, "Gone") == ["Ada"]
    enter, walk, out, leave = w.drain_events()
    assert isinstance(walk, Nested) and isinstance(out, Nested)
    assert (walk.path, walk.nested_id, out.path, out.nested_id) == (inside, level_id, inside, level_id)
    # Into the port from outside, out of it inside: one id.
    assert enter.produced == ((LOBBY, walk.event.consumed[0][1]),)
    assert walk.event.consumed == (("Lobby", enter.produced[0][1]),)
    # Into the port inside, out of it from outside: one id.
    assert out.event.produced == (("Exit Hall", leave.consumed[0][1]),)
    assert leave.consumed == ((HALL, out.event.produced[0][1]),)


def test_ids_in_a_port_are_the_subnets_and_stay_in_increasing_order():
    w = world(
        FunctionTransitionNode("Enter", same),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", LOBBY),
        lobby=[hero("Ada")], door=[hero("Bo"), hero("Cy")], open=True,
    )
    outer_next = w.next_token_id
    # The subnet mints, wherever the token comes from: the outer net's firing, the
    # program through the outer net (open, so that it takes a write at the pair),
    # the program inside the subnet.
    fire(w, "Enter")
    ctx = RunContext(graph=w, inbox=asyncio.Queue(), strict=True)
    ctx.inbox.put_nowait(Extend(LOBBY, [hero("Di")]))
    assert Runner.drain_inbox(ctx) == 1
    with NestedNets.subnet_scope(w, "Level 3") as inside:
        NestedNets.put(inside, "Lobby", [hero("Ed")])
    fire(w, "Enter")
    assert names(w, LOBBY) == ["Ada", "Bo", "Di", "Ed", "Cy"]
    assert ids(w, LOBBY) == [1, 2, 3, 4, 5], "one set of ids, in increasing order"
    assert w.next_token_id == outer_next, "the outer net minted none of them"
    assert w.subnet("Level 3").next_token_id == 6
    same_from_both(w, LOBBY)


def train(hero: Hero) -> Hero:
    return hero.model_copy(update={"hp": 99})


def test_keyed_edges_and_queries_see_across_the_border_both_ways():
    # The level picks mages out of its lobby, whoever put them there.
    trainer = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Lobby", Hero, port=True, keys=KEYS),
        ListPlaceNode("Exit Hall", Hero, port=True, keys=KEYS),
        FunctionTransitionNode("Train a Mage", train),
        TakeWhereEdge("Lobby", "Train a Mage", "hero", where=Filter.where(role="Mage")),
        ReturnedEdgeFromTransition("Train a Mage", "Exit Hall"),
    ])
    # The world picks trained heroes out of the hall.
    w = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", Hero, [hero("Ada"), hero("Bo", "Mage")]),
        SubnetPlaceNode("Level 3", trainer),
        ListPlaceNode("Gone", Hero),
        FunctionTransitionNode("Enter", everyone),
        ArgumentEdgeToTransition("Door", "Enter", "heroes"),
        ReturnedEdgeFromTransition("Enter", LOBBY),
        FunctionTransitionNode("Take the Trained", same),
        TakeWhereEdge(HALL, "Take the Trained", "hero", where=Filter.where(hp=99)),
        ReturnedEdgeFromTransition("Take the Trained", "Gone"),
    ])
    until_rest(w)
    assert names(w, "Gone") == ["Bo"], "trained inside, taken outside"
    assert names(w, LOBBY) == ["Ada"]
    # Queries from out here read the port, named by its pair.
    ada = ids(w, LOBBY)[0]
    found = Query.matches(w, Filter.where(role="Knight"))
    assert [(match.place, match.token_id) for match in found] == [(LOBBY, ada)]
    assert found[0].keys["name"] == "Ada"
    assert Query.distinct(w, "role") == ["Knight"], "the lobby's Ada; Bo has gone, and Gone declares no keys"
    assert [m.place for m in Query.matches(w, Filter().in_places(LOBBY))] == [LOBBY]
    assert [m.place for m in Query.matches(w, Filter().with_prefix("Lob"))] == [LOBBY], "a port by its own name"
    assert Query.matches(w, Filter().in_places("Level 3")) == [], "a subnet place holds no tokens"
    assert Query.distinct(w, "name", places=[HALL]) == []


# --- failure -----------------------------------------------------------------------------------


def ambush(hero: Hero, heroes: list[Hero]) -> Hero:
    raise RuntimeError("an ambush")


def test_a_failed_outer_firing_puts_back_what_it_took_from_two_ports():
    w = world(
        FunctionTransitionNode("Ambush", ambush),
        ArgumentEdgeToTransition(LOBBY, "Ambush", "hero"),
        ArgumentEdgeToTransition(HALL, "Ambush", "heroes"),
        ReturnedEdgeFromTransition("Ambush", "Gone"),
        lobby=[hero("Ada"), hero("Bo")], hall=[hero("Cy"), hero("Di")], restore=True,
    )
    lobby, hall = ids(w, LOBBY), ids(w, HALL)
    w.drain_events()
    with pytest.raises(TransitionFailedError) as failed:
        fire(w, "Ambush")
    assert failed.value.restored
    assert ids(w, LOBBY) == lobby and ids(w, HALL) == hall, "in the places and order they came from"
    same_from_both(w, LOBBY)
    same_from_both(w, HALL)
    [event] = w.drain_events()
    assert isinstance(event, Failed) and event.restored
    assert event.consumed == ((LOBBY, lobby[0]), (HALL, hall[0]), (HALL, hall[1]))
    # The subnet finds the restored tokens as its own.
    assert len(Query.matches(w.subnet("Level 3"), Filter.where(name="Ada"))) == 1


def stumble(hero: Hero) -> Hero:
    raise RuntimeError("a stumble")


def test_a_failed_outer_firing_puts_nothing_into_a_port():
    w = world(
        FunctionTransitionNode("Stumble In", stumble),
        ArgumentEdgeToTransition("Door", "Stumble In", "hero"),
        ReturnedEdgeFromTransition("Stumble In", LOBBY),
        lobby=[hero("Ada")], door=[hero("Bo")],
    )
    before = ids(w, LOBBY)
    with pytest.raises(TransitionFailedError):
        fire(w, "Stumble In")
    assert ids(w, LOBBY) == before, "nothing delivered"
    assert names(w, "Door") == [], "and Bo is lost, as by default"
    events = w.drain_events()
    assert [type(event) for event in events] == [Failed], "the subnet was not touched"


def test_a_failed_firing_inside_a_subnet_is_told_as_an_inner_failure():
    broken = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Lobby", Hero, [hero("Ada")], port=True),
        ListPlaceNode("Arena", Hero),
        FunctionTransitionNode("Trip", stumble),
        ArgumentEdgeToTransition("Lobby", "Trip", "hero"),
        ReturnedEdgeFromTransition("Trip", "Arena"),
    ])
    # As for any inner net, the setting of the net a deep step is taken on governs it.
    w = ExecutableGraphOperations.construct_graph(
        [SubnetPlaceNode("Level 3", broken)], record_events=True, restore_tokens_on_failure=True,
    )
    with pytest.raises(TransitionFailedError) as failed:
        deep_step(w)
    assert failed.value.transition_name == "Trip"
    [event] = w.drain_events()
    assert isinstance(event, Nested) and event.path == (PathStep("Level 3"),)
    assert isinstance(event.event, Failed) and event.event.consumed == (("Lobby", 1),)
    assert names(w, LOBBY) == ["Ada"]


# --- the in-flight rule ------------------------------------------------------------------------


def walking_world(door=(), lobby=None):
    if lobby is None:
        lobby = (hero("Ada"),)
    return world(
        FunctionTransitionNode("Enter", same),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", LOBBY),
        walk=True, lobby=lobby, door=door,
    )


def test_a_firing_in_flight_on_a_port_holds_its_subnet_still():
    w = walking_world()
    # As while Enter is launched and not landed: its hero out of the door, its body running.
    w.in_flight = {"Enter"}
    assert NestedNets.held_subnets(w) == {"Level 3": "Enter"}
    assert deep_step(w) is None, "the level has work, but Enter is in flight with a token for its lobby"
    with pytest.raises(SubnetHeldError) as held:
        with NestedNets.subnet_scope(w, "Level 3"):
            pass
    assert (held.value.place, held.value.transition_name) == ("Level 3", "Enter")
    w.in_flight = set()
    assert deep_step(w) == InnerFiring((PathStep("Level 3"),), "Walk")
    with NestedNets.subnet_scope(w, "Level 3"):
        pass


def test_a_sequential_firing_holds_its_subnet_still_while_its_body_runs():
    w = walking_world(door=[hero("Bo")])
    seen = []

    def observe(graph):
        if graph.in_flight:
            try:
                with NestedNets.subnet_scope(graph, "Level 3"):
                    seen.append("free")
            except SubnetHeldError as error:
                seen.append(error.transition_name)

    run(Runner.run(RunContext(graph=w, observers=(observe,))))
    assert seen == ["Enter"], "at Enter's launch the level was held"
    assert names(w, HALL) == ["Ada", "Bo"]


def test_a_firing_in_flight_holds_only_the_subnet_it_reaches():
    w = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", Hero),
        SubnetPlaceNode("Level 1", level(walk=True, lobby=[hero("Ada")])),
        SubnetPlaceNode("Level 2", level(walk=True, lobby=[hero("Bo")])),
        FunctionTransitionNode("Enter", same),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", Port("Level 1", "Lobby")),
    ])
    w.in_flight = {"Enter"}
    assert deep_step(w) == InnerFiring((PathStep("Level 2"),), "Walk")


def test_one_firing_on_ports_of_two_subnets_holds_both_still():
    w = ExecutableGraphOperations.construct_graph([
        SubnetPlaceNode("Level 1", level(walk=True, lobby=[hero("Ada")])),
        SubnetPlaceNode("Level 2", level(walk=True, lobby=[hero("Bo")])),
        FunctionTransitionNode("Transfer", same),
        ArgumentEdgeToTransition(Port("Level 1", "Exit Hall"), "Transfer", "hero"),
        ReturnedEdgeFromTransition("Transfer", Port("Level 2", "Lobby")),
    ])
    w.in_flight = {"Transfer"}
    assert NestedNets.held_subnets(w) == {"Level 1": "Transfer", "Level 2": "Transfer"}
    assert deep_step(w) is None, "both levels held"
    w.in_flight = set()
    until_rest(w)
    assert names(w, Port("Level 2", "Exit Hall")) == ["Bo", "Ada"], "Ada walked out of level 1, and over"


# --- stepping ----------------------------------------------------------------------------------


def forward(n: int) -> int:
    return n


def where_fired(step, names: dict) -> str:
    """Where a deep step fired: ``outer`` for the net stepped, else the name of the
    innermost subnet it fired in, ``-`` for nothing."""
    if step is None:
        return "-"
    if isinstance(step, InnerFiring):
        return names[step.path[-1].place]
    return "outer"


def story(graph: ExecutableGraph, n: int, names: dict) -> list[str]:
    return [where_fired(deep_step(graph), names) for _ in range(n)]


def guild(stepping: Optional[str]) -> ExecutableGraph:
    """An outer net with two jobs of its own (``Inbox -Forward-> Outbox``) and, at
    ``Level``, a level whose lobby holds one hero: two inner firings."""
    subnet = SubnetPlaceNode("Level", level(walk=True, lobby=[hero("Ada")]))
    if stepping is not None:
        subnet = SubnetPlaceNode("Level", subnet.graph, stepping=stepping)
    return ExecutableGraphOperations.construct_graph([
        subnet,
        ListPlaceNode("Inbox", int, [1, 2]),
        ListPlaceNode("Outbox", int),
        FunctionTransitionNode("Forward", forward),
        ArgumentEdgeToTransition("Inbox", "Forward", "n"),
        ReturnedEdgeFromTransition("Forward", "Outbox"),
    ], record_events=True)


@pytest.mark.parametrize(("stepping", "expected"), [
    (None, ["level", "level", "outer", "outer", "-"]),
    ("inner_first", ["level", "level", "outer", "outer", "-"]),
    ("take_turns", ["outer", "level", "outer", "level", "-"]),
    ("outer_first", ["outer", "outer", "level", "level", "-"]),
    ("passive", ["outer", "outer", "-", "-", "-"]),
])
def test_a_subnet_steps_under_each_rule(stepping, expected):
    g = guild(stepping)
    assert g.place_named("Level").stepping == (stepping or "inner_first")
    assert story(g, 5, {"Level": "level"}) == expected


def test_a_passive_subnet_is_the_programs_to_step():
    g = guild("passive")
    until_rest(g)
    g.drain_events()
    with NestedNets.subnet_scope(g, "Level") as inside:
        fired = deep_step(inside)
    assert fired == "Walk"
    [event] = g.drain_events()
    assert event.path == (PathStep("Level"),) and event.event.transition == "Walk", "told by the subnet, lifted"


def test_assigning_stepping_changes_a_subnet_places_rule():
    g = guild(None)
    g.place_named("Level").stepping = "outer_first"
    assert story(g, 5, {"Level": "level"}) == ["outer", "outer", "level", "level", "-"]


def test_two_subnets_with_different_rules_take_their_bands():
    g = ExecutableGraphOperations.construct_graph([
        # Declared against the bands' order, as the stepping tests do.
        SubnetPlaceNode("Late", level(walk=True, lobby=[hero("Bo")]), stepping="outer_first"),
        SubnetPlaceNode("Ring", level(walk=True, lobby=[hero("Cy")]), stepping="take_turns"),
        SubnetPlaceNode("Early", level(walk=True, lobby=[hero("Ada")])),
        ListPlaceNode("Inbox", int, [1, 2, 3]),
        ListPlaceNode("Outbox", int),
        FunctionTransitionNode("Forward", forward),
        ArgumentEdgeToTransition("Inbox", "Forward", "n"),
        ReturnedEdgeFromTransition("Forward", "Outbox"),
    ])
    names = {"Late": "late", "Ring": "ring", "Early": "early"}
    assert story(g, 10, names) == [
        "early", "early", "outer", "ring", "outer", "ring", "outer", "late", "late", "-",
    ]


def sit(hero: Hero) -> Hero:
    return hero


def room_level_world():
    room = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Den", Hero, port=True),
        ListPlaceNode("Seat", Hero),
        FunctionTransitionNode("Sit", sit),
        ArgumentEdgeToTransition("Den", "Sit", "hero"),
        ReturnedEdgeFromTransition("Sit", "Seat"),
    ], template="Test Room")
    a_level = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Lobby", Hero, port=True),
        SubnetPlaceNode("Room", room),
        FunctionTransitionNode("Go to the Room", same),
        ArgumentEdgeToTransition("Lobby", "Go to the Room", "hero"),
        ReturnedEdgeFromTransition("Go to the Room", Port("Room", "Den")),
    ], template="Test Floor")
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", Hero, [hero("Ada")]),
        SubnetPlaceNode("Level 3", a_level),
        FunctionTransitionNode("Enter", same),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", LOBBY),
    ], record_events=True, template="Test Tower")


def test_a_subnet_inside_a_subnet():
    w = room_level_world()
    room = w.subnet("Level 3").subnet("Room")
    down = (PathStep("Level 3"), PathStep("Room"))
    assert until_rest(w) == [
        "Enter",
        InnerFiring((PathStep("Level 3"),), "Go to the Room"),
        InnerFiring(down, "Sit"),
    ]
    enter, go, sit_event = w.drain_events()
    assert go.path == (PathStep("Level 3"),)
    assert go.event.produced == ((Port("Room", "Den"), 1),), "the level tells its own firing, naming the pair"
    assert (sit_event.path, sit_event.nested_id) == (down, room.net_id)
    assert sit_event.event.consumed == (("Den", 1),)
    assert names(room, "Seat") == ["Ada"]
    assert NestedNets.resolve(w, down) is room
    assert NestedNets.find(w, room.net_id) == (down, room)
    assert w.fired_counts[InnerFiring(down, "Sit")] == 1 and w.step_count == 3


# --- tokens that carry nets in a port ----------------------------------------------------------


def work(n: int) -> int:
    return n


def hire(worker: ExecutableGraph) -> ExecutableGraph:
    return worker


def test_a_port_may_hold_tokens_that_carry_nets():
    worker = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Todo", int, [1]),
        ListPlaceNode("Done", int),
        FunctionTransitionNode("Work", work),
        ArgumentEdgeToTransition("Todo", "Work", "n"),
        ReturnedEdgeFromTransition("Work", "Done"),
    ])
    workshop = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Bench", ExecutableGraph, nested=True, port=True),
    ])
    w = ExecutableGraphOperations.construct_graph([
        # The yard's worker waits for the hire: the yard is passive.
        ListPlaceNode("Yard", ExecutableGraph, [worker], nested=True, stepping="passive"),
        SubnetPlaceNode("Workshop", workshop),
        FunctionTransitionNode("Hire", hire),
        ArgumentEdgeToTransition("Yard", "Hire", "worker"),
        ReturnedEdgeFromTransition("Hire", Port("Workshop", "Bench")),
    ], record_events=True)
    fired = until_rest(w)
    [token] = ids(w, Port("Workshop", "Bench"))
    path = (PathStep("Workshop"), PathStep("Bench", token, ""))
    assert fired == ["Hire", InnerFiring(path, "Work")], "the workshop stepped it, under its own rule for Bench"
    assert worker.fired_counts["Work"] == 1
    events = w.drain_events()
    assert any(isinstance(e, Nested) and e.path == path and e.event.transition == "Work" for e in events)
    assert NestedNets.resolve(w, path) is worker


# --- outside input -----------------------------------------------------------------------------


def test_outside_input_reaches_a_port_through_the_outer_net():
    """Through an open outer net: a closed one refuses a write at a pair, since a port
    of its subnet place is none of its own (``tests/test_closed_nets.py``)."""
    closed = world(lobby=[hero("Ada")])
    closed_ctx = RunContext(graph=closed, inbox=asyncio.Queue(), strict=True)
    closed_ctx.inbox.put_nowait(Extend(LOBBY, [hero("Bo")]))
    with pytest.raises(ClosedNetError):
        Runner.drain_inbox(closed_ctx)
    assert names(closed, LOBBY) == ["Ada"]
    w = world(lobby=[hero("Ada")], open=True)
    w.drain_events()
    ctx = RunContext(graph=w, inbox=asyncio.Queue(), strict=True)
    ctx.inbox.put_nowait(Extend(LOBBY, [hero("Bo"), hero("Cy")]))
    ctx.inbox.put_nowait(SetTokens(HALL, [hero("Di")]))
    ctx.inbox.put_nowait(SetTokens(HALL, [hero("Ed")]))
    assert Runner.drain_inbox(ctx) == 3
    assert names(w, LOBBY) == ["Ada", "Bo", "Cy"] and names(w, HALL) == ["Ed"]
    same_from_both(w, LOBBY)
    same_from_both(w, HALL)
    assert w.drain_events() == [
        Added(LOBBY, 2), Added(LOBBY, 3), Added(HALL, 4), Removed(HALL, 4), Added(HALL, 5),
    ], "each change once, in the outer log, naming the pair, with the subnet's ids"
    ctx.inbox.put_nowait(Extend(LOBBY, [7]))
    with pytest.raises(ValueError, match="does not match"):
        Runner.drain_inbox(ctx)
    ctx.inbox.put_nowait(Extend("Level 3", [hero("Fy")]))
    with pytest.raises(ValueError, match="holds no tokens of its own"):
        Runner.drain_inbox(ctx)
    ctx.inbox.put_nowait(Extend(Port("Level 3", "Arena"), [hero("Fy")]))
    with pytest.raises(ValueError, match="unknown place"):
        Runner.drain_inbox(ctx)


def test_nested_nets_put_and_take_reach_a_port_through_an_open_outer_net():
    """The program's ``NestedNets.put`` and ``take`` by the pair, through an open
    outer net, which takes a write there; a closed one refuses it
    (``test_outside_input_reaches_a_port_through_the_outer_net``)."""
    w = world(open=True)
    NestedNets.put(w, LOBBY, [hero("Ada")])
    assert NestedNets.take(w, LOBBY) == [hero("Ada")]
    assert w.drain_events() == [Added(LOBBY, 1), Removed(LOBBY, 1)]
    with pytest.raises(KeyError, match="holds no tokens of its own"):
        NestedNets.put(w, "Level 3", [hero("Bo")])


# --- the events --------------------------------------------------------------------------------


def test_each_change_is_told_once_by_the_net_that_made_it():
    w = walking_world(door=[hero("Bo")], lobby=())
    assert deep_step(w) == "Enter"
    assert w.subnet("Level 3").drain_events() == [], "the subnet told nothing of Enter"
    assert deep_step(w) == InnerFiring((PathStep("Level 3"),), "Walk")
    bo = ids(w.subnet("Level 3"), "Arena")[0]
    enter, walk = w.drain_events()
    assert told(enter) == ("Enter", (("Door", 1),), ((LOBBY, 1),))
    assert walk.path == (PathStep("Level 3"),) and walk.nested_id == w.subnet("Level 3").net_id
    assert told(walk.event) == ("Walk", (("Lobby", 1),), (("Arena", bo),))
    assert w.drain_events() == []


# --- naming, reading and changing --------------------------------------------------------------


def test_a_port_is_named_by_its_subnet_place_and_its_name():
    w = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Lobby", Hero),
        SubnetPlaceNode("Level 1", level()),
        SubnetPlaceNode("Level 2", level(lobby=[hero("Ada")])),
    ])
    own = w.place_named("Lobby")
    assert own is w.places[0], "a name alone finds only the net's own place"
    assert w.place_named(Port("Level 2", "Lobby")) is w.subnet("Level 2").place_named("Lobby")
    assert w.place_named(Port("Level 2", "Arena")) is None, "only ports are reached from outside"
    assert w.place_named(Port("Lobby", "Lobby")) is None
    assert list(MapPlaceNames.to_list_place_nodes(w)) == [
        "Lobby", Port("Level 1", "Lobby"), Port("Level 1", "Exit Hall"),
        Port("Level 2", "Lobby"), Port("Level 2", "Exit Hall"),
    ]
    assert isinstance(w.place_named("Level 1"), SubnetPlaceNode)
    assert w.place_named("Level 1").tokens == ()
    assert w.subnet("Lobby") is None and w.subnet("Nowhere") is None
    with pytest.raises(LookupError):
        with NestedNets.subnet_scope(w, "Lobby"):
            pass
    assert TokenIds.locate(w, 1) is None, "a port's ids are its subnet's, located through it"
    assert TokenIds.locate(w.subnet("Level 2"), 1) == ("Lobby", hero("Ada"))
    assert NestedNets.token_count(w) == 0 and NestedNets.token_count(w.subnet("Level 2")) == 1


# --- saving, restoring and copying -------------------------------------------------------------


@Templates.template("Test Level")
def build_test_level(lobby=()) -> ExecutableGraph:
    return level(walk=True, lobby=lobby, template="Test Level")


@Templates.template("Test World")
def build_test_world(door=(), lobby=()) -> ExecutableGraph:
    """The heroes at its door and in its level's lobby start it."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", Hero, list(door)),
        SubnetPlaceNode("Level 3", build_test_level(lobby)),
        ListPlaceNode("Gone", Hero),
        FunctionTransitionNode("Enter", same),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", LOBBY),
        FunctionTransitionNode("Leave", same),
        ArgumentEdgeToTransition(HALL, "Leave", "hero"),
        ReturnedEdgeFromTransition("Leave", "Gone"),
    ], record_events=True, template="Test World")


Templates.register("Test Room", lambda: room_level_world().subnet("Level 3").subnet("Room"))
Templates.register("Test Floor", lambda: room_level_world().subnet("Level 3"))
Templates.register("Test Tower", room_level_world)


def test_saving_and_restoring_a_net_with_a_subnet():
    w = build_test_world(door=[hero("Ada"), hero("Bo")])
    deep_step(w)  # Ada enters
    deep_step(w)  # and walks into the arena
    saved = NetJson.save(w)
    assert saved["marking"]["Level 3"] == [{
        "template": "Test Level",
        "marking": {"Lobby": [], "Arena": [hero("Ada").model_dump()], "Exit Hall": []},
        "params": {},
        "enabled": {"Walk": True, "Out": True},
    }], "a list of one, the subnet in its saved form, and nothing of the ports outside it"
    assert set(saved["marking"]) == {"Door", "Level 3", "Gone"}
    again = NetJson.restore(json.loads(json.dumps(saved)))
    assert NetJson.save(again) == saved
    assert again.subnet("Level 3").net_id != w.subnet("Level 3").net_id
    assert again.place_named("Level 3").stepping == "inner_first", "the rule is the definition's"
    until_rest(again)
    assert names(again, "Gone") == ["Ada", "Bo"]
    assert w.model_dump(mode="json") == saved, "a graph dumps as its saved form"


def test_saving_and_restoring_at_two_depths():
    w = room_level_world()
    deep_step(w)
    deep_step(w)  # Ada at the room's door
    saved = NetJson.save(w)
    floor = saved["marking"]["Level 3"][0]
    assert floor["marking"]["Room"][0]["marking"] == {"Den": [hero("Ada").model_dump()], "Seat": []}
    again = NetJson.restore(json.loads(json.dumps(saved)))
    assert NetJson.save(again) == saved
    until_rest(again)
    assert names(again.subnet("Level 3").subnet("Room"), "Seat") == ["Ada"]


def test_copying_a_net_copies_its_subnets():
    w = room_level_world()
    deep_step(w)
    twin = copy.deepcopy(w)
    assert twin.subnet("Level 3").net_id != w.subnet("Level 3").net_id
    assert twin.subnet("Level 3").subnet("Room").net_id != w.subnet("Level 3").subnet("Room").net_id
    until_rest(twin)
    assert names(twin.subnet("Level 3").subnet("Room"), "Seat") == ["Ada"]
    assert names(w, LOBBY) == ["Ada"], "the original stays where it was"
    assert NetJson.save(NestedNets.fork(w)) == NetJson.save(w)


def test_a_subnet_without_a_template_cannot_be_saved():
    w = world(lobby=[hero("Ada")])
    with pytest.raises(TypeError, match="subnet place 'Level 3' holds a net built without a template"):
        NetJson.save(w)
    with pytest.raises(TypeError, match="subnet place 'Level 3'"):
        w.import_marking({})
    assert names(w, LOBBY) == ["Ada"]


def test_loading_a_marking_checks_every_depth_before_changing_anything():
    w = build_test_world(door=[hero("Ada")], lobby=[hero("Bo")])
    for bad, kind in [
        ({"Level 3": {"marking": {}}}, ValueError),
        ({"Level 3": [{"marking": {}}, {"marking": {}}]}, ValueError),
        ({"Level 3": [{"marking": {"Kitchen": []}}]}, KeyError),
        ({"Level 3": [{"marking": {"Lobby": [7]}}]}, ValueError),
        ({"Level 3": [{"template": "Cabin", "marking": {}}]}, ValueError),
    ]:
        with pytest.raises(kind, match="in subnet place 'Level 3'"):
            w.import_marking({"Door": [], **bad})
        assert names(w, "Door") == ["Ada"] and names(w, LOBBY) == ["Bo"], bad
    w.import_marking({"Door": [hero("Cy").model_dump()]})
    assert names(w, "Door") == ["Cy"] and names(w, LOBBY) == [], "a subnet place left out loads empty"


# --- the build errors --------------------------------------------------------------------------


def edge_to(place, argument_type=Hero):
    def body(hero: argument_type) -> Hero:  # type: ignore[valid-type]
        return hero
    return [
        FunctionTransitionNode("Leave", body),
        ArgumentEdgeToTransition(place, "Leave", "hero"),
        ReturnedEdgeFromTransition("Leave", "Gone"),
    ]


@pytest.mark.parametrize(("place", "message"), [
    (Port("Level 3", "Kitchen"), "subnet place 'Level 3' has no port 'Kitchen'"),
    (Port("Level 3", "Arena"), "subnet place 'Level 3' has no port 'Arena'"),
    (Port("Door", "Lobby"), "names Port\\('Door', 'Lobby'\\), but 'Door' is not a subnet place"),
    (Port("Nowhere", "Lobby"), "unknown place: Nowhere"),
    ("Level 3", "transition 'Leave' has an edge on subnet place 'Level 3', which holds no tokens of its own"),
])
def test_an_edge_must_name_a_place_or_a_port_the_subnet_has(place, message):
    with pytest.raises(ValueError, match=message):
        world(*edge_to(place))


def test_an_edge_must_fit_its_ports_type():
    with pytest.raises(TypeError, match="from port 'Lobby' of subnet place 'Level 3'"):
        world(*edge_to(LOBBY, int))


def test_a_subnet_place_cannot_be_a_port():
    with pytest.raises(ValueError, match="subnet place 'Room' cannot be a port"):
        SubnetPlaceNode("Room", level(), port=True)


def test_one_net_cannot_stand_in_two_subnet_places():
    shared = level()
    with pytest.raises(ValueError, match="the same net"):
        ExecutableGraphOperations.construct_graph([SubnetPlaceNode("A", shared), SubnetPlaceNode("B", shared)])


def test_a_subnet_place_has_a_stepping_rule_of_the_four():
    with pytest.raises(ValueError):
        SubnetPlaceNode("Level 3", level(), stepping="sometimes")


# --- the concurrent runtime --------------------------------------------------------------------


def test_the_concurrent_runtime_runs_a_world_with_a_subnet():
    w = world(
        FunctionTransitionNode("Enter", same),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", LOBBY),
        FunctionTransitionNode("Leave", same),
        ArgumentEdgeToTransition(HALL, "Leave", "hero"),
        ReturnedEdgeFromTransition("Leave", "Gone"),
        walk=True, door=[hero(f"Hero {n}") for n in range(5)],
    )
    summary = run(Runner.run(RunContext(graph=w, mode=ExecutionMode.CONCURRENT)))
    assert summary.quiesced
    assert len(names(w, "Gone")) == 5 and names(w, LOBBY) == [] and names(w, HALL) == []
    assert summary.fired[InnerFiring((PathStep("Level 3"),), "Walk")] == 5


def test_the_concurrent_runtime_holds_a_subnet_still_while_a_body_on_its_port_runs():
    inside = level(walk=True)
    watched = []

    async def slow_enter(hero: Hero) -> Hero:
        before = inside.step_count
        await asyncio.sleep(0.02)
        watched.append(inside.step_count - before)
        return hero

    w = world(
        ListPlaceNode("Bench", Hero, [hero("Cy")]),
        FunctionTransitionNode("Enter", slow_enter),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", LOBBY),
        FunctionTransitionNode("Quick", same),
        ArgumentEdgeToTransition("Bench", "Quick", "hero"),
        ReturnedEdgeFromTransition("Quick", LOBBY),
        door=[hero("Ada")],
    )
    # The world's level is the one the bodies reach.
    w = ExecutableGraphOperations.construct_graph(
        [place if place.name != "Level 3" else SubnetPlaceNode("Level 3", inside) for place in w.places]
        + list(w.transitions) + list(w.argument_edges) + list(w.return_edges),
        record_events=True,
    )
    run(Runner.run(RunContext(graph=w, mode=ExecutionMode.CONCURRENT)))
    # Quick lands while Enter is still in flight, so the level has work, Cy in its
    # lobby, and is held still until Enter lands.
    assert watched == [0]
    assert names(w, HALL) == ["Cy", "Ada"]


# --- the Graphviz symbol -----------------------------------------------------------------------


def dot_of(graph: ExecutableGraph) -> str:
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz
    node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(graph)
    return RustworkxGraph.from_executable_graph(graph).to_dot(node_attr_fn, edge_attr_fn)


def test_a_closed_subnet_place_is_one_rounded_box_with_a_cell_per_port():
    w = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Door", Hero),
        SubnetPlaceNode("Level 3", level(lobby=[hero("Ada"), hero("Bo")], template="Test Level")),
        ListPlaceNode("Gone", Hero),
        FunctionTransitionNode("Enter", same),
        ArgumentEdgeToTransition("Door", "Enter", "hero"),
        ReturnedEdgeFromTransition("Enter", LOBBY),
        FunctionTransitionNode("Leave", same),
        ArgumentEdgeToTransition(HALL, "Leave", "hero"),
        ReturnedEdgeFromTransition("Leave", "Gone"),
    ])
    dot = dot_of(w)
    assert dot.count("shape=Mrecord") == 1, "one node for the subnet place, none for its ports"
    assert 'label="{Level 3\\n(Test Level)|{<port0> Lobby\\n2|<port1> Exit Hall\\n0}}"' in dot
    assert "Arena" not in dot, "the subnet's own places are not drawn outside"
    lines = dot.splitlines()
    nodes = {line.split(" ")[0]: line for line in lines if " [" in line and "->" not in line}
    index = {name: key for key, line in nodes.items() for name in ("Door", "Level 3", "Gone", "Enter", "Leave")
             if f'label="{name}' in line or f'label="{{{name}' in line}
    assert any(line.startswith(f"{index['Enter']} -> {index['Level 3']} ") and "headport=port0" in line
               for line in lines), "Enter's arrow ends at the lobby's cell"
    assert any(line.startswith(f"{index['Level 3']} -> {index['Leave']} ") and "tailport=port1" in line
               for line in lines), "Leave's arrow starts at the exit hall's cell"


def test_a_port_seen_from_inside_has_one_dotted_ring():
    """Since 2026-11-28, where it had a second ring, which looked like a place
    inside a place."""
    dot = dot_of(level())
    assert "peripheries" not in dot
    # A label may hold line breaks, so a node's statement runs to its "];".
    nodes = [statement for statement in dot.split("];") if "label=" in statement and "->" not in statement]
    assert [('style="filled,dotted"' in node) for node in nodes] == [True, False, True], "Lobby, Arena, Exit Hall"
    assert "style=filled" in nodes[1] and "dotted" not in nodes[1], "Arena, plainly filled"


def test_a_cell_draws_any_name_as_it_is():
    pytest.importorskip("rustworkx")
    from petritype.plotting.rustworkx_to_graphviz import NodeLabel
    odd = ExecutableGraphOperations.construct_graph([ListPlaceNode('A|b {c} <d> "e"\\', int, [1], port=True)])
    label = NodeLabel.subnet_place(SubnetPlaceNode("x|y", odd))
    assert label == '{x\\|y|{<port0> A\\|b \\{c\\} \\<d\\> \\"e\\"\\\\\\n1}}'


# --- the example -------------------------------------------------------------------------------


def test_the_levels_example_runs_its_own_journey():
    from examples.subnet_places.levels import build_world, describe_log, journey
    w = build_world()
    events = run(journey(w))
    assert len(describe_log(events)) == 21
    assert [(h.name, h.xp) for h in w.place_named("Outside").tokens] == [("Ada", 3), ("Bram", 3), ("Cato", 3)]
    assert names(w, "Door") == []
    for level_name in ("Level 1", "Level 2"):
        assert NestedNets.token_count(w.subnet(level_name)) == 0
    # The world is built by a plain function, its levels by the template: it loads
    # into a world built the same way.
    again = NetJson.load_into(build_world(), json.loads(json.dumps(NetJson.save(w))))
    assert again.subnet("Level 2").params == {"reward": 2}
    assert [h.name for h in again.place_named("Outside").tokens] == ["Ada", "Bram", "Cato"]
