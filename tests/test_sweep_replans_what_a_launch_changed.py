"""The concurrent launch sweep plans a transition again only when a launch may have
changed what its plan reads (``_launch_sweep`` in ``petritype.runtime``).

These tests hold it to the sweep it replaced, kept here as ``_reference_sweep``: that
one planned every transition again each time round and gathered the claims held in
flight afresh for every check. On random nets built from a fixed seed the two must
launch the same transitions in the same order, hand the selector the same lists, ask
the guards and priorities in the same order, apply the same ``Fire`` commands and
leave the same marking. Then the cases a kept plan could get wrong, one by one."""

import asyncio
import random
import re
from dataclasses import dataclass
from typing import Optional

import pytest

from petritype import runtime
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    DrainEdge,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    MutateWhereEdge,
    NestedNets,
    ReadAllEdge,
    ReadEdge,
    ReadWhereEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    SurveyEdge,
    TakeWhereEdge,
    default_transition_selector,
)
from petritype.core.keys import Clause, Filter
from petritype.runtime import ExecutionMode, Fire, RunContext, Runner

construct = ExecutableGraphOperations.construct_graph


# --- the sweep as it was: everything planned again each time round ----------------------


def _reference_conflicts(claims, flight_state) -> bool:
    if not flight_state:
        return False
    held_ex_tok: set = set()
    held_sh_tok: set = set()
    held_ex_place: set = set()
    held_sh_place: set = set()
    for _transition, _args, _firing_ids, active in flight_state.values():
        held_ex_tok.update(active.hold_exclusive_tokens)
        held_sh_tok.update(active.hold_shared_tokens)
        held_ex_place.update(active.hold_exclusive_places)
        held_sh_place.update(active.hold_shared_places)
    if not (held_ex_tok or held_sh_tok or held_ex_place or held_sh_place):
        return False
    held_ex_tok_places = {place for place, _ in held_ex_tok}
    held_sh_tok_places = {place for place, _ in held_sh_tok}
    for place in claims.need_exclusive_places:
        if (
            place in held_ex_place
            or place in held_sh_place
            or place in held_ex_tok_places
            or place in held_sh_tok_places
        ):
            return True
    for place in claims.need_shared_places:
        if place in held_ex_place or place in held_ex_tok_places:
            return True
    for pair in claims.need_exclusive_tokens:
        place = pair[0]
        if pair in held_ex_tok or pair in held_sh_tok or place in held_ex_place or place in held_sh_place:
            return True
    for pair in claims.need_shared_tokens:
        place = pair[0]
        if pair in held_ex_tok or place in held_ex_place:
            return True
    return False


def _reference_launchable(ctx, transition, place_nodes, incoming, read, flight_state):
    graph = ctx.graph
    if transition.name in ctx.disabled or transition.name in graph.disabled:
        return None
    plan = ExecutableGraphOperations.plan_stage_1(
        transition=transition,
        transition_names_to_incoming_edges=incoming,
        place_names_to_nodes=place_nodes,
        transition_names_to_read_edges=read,
        net_params=graph.params,
        check_filters=True,
    )
    if plan is None:
        return None
    claims = runtime._claims_from_plan(plan)
    if _reference_conflicts(claims, flight_state):
        return None
    if transition.guard is not None and not bool(transition.guard(graph)):
        return None
    return plan, claims


def _reference_sweep(ctx, place_nodes, incoming, outgoing, read, in_flight, flight_state, remaining_cap=None):
    graph = ctx.graph
    airborne = set(in_flight.values())
    selector = graph.transition_selector or default_transition_selector
    launched = 0
    while remaining_cap is None or launched < remaining_cap:
        candidates = []
        plans_and_claims = {}
        for transition in graph.transitions:
            if transition.name in airborne:
                continue
            launchable = _reference_launchable(ctx, transition, place_nodes, incoming, read, flight_state)
            if launchable is None:
                continue
            candidates.append(transition)
            plans_and_claims[transition.name] = launchable
        if not candidates:
            break
        chosen = selector(graph, candidates)
        if chosen is None:
            break
        if chosen not in candidates:
            raise ValueError(
                f"transition_selector returned {chosen!r}, which is not among the launchable transitions "
                f"{[t.name for t in candidates]}"
            )
        if runtime._fuse_spent(ctx):
            runtime._blow_fuse(ctx, chosen.name)
        plan, claims = plans_and_claims[chosen.name]
        task, firing_ids, args = runtime._launch(ctx, chosen, place_nodes, incoming, outgoing, read, plan=plan)
        in_flight[task] = chosen.name
        flight_state[task] = (chosen, args, firing_ids, claims)
        airborne.add(chosen.name)
        launched += 1
        ctx.n_fired += 1
    if launched:
        NestedNets.took_own_turn(graph)
    return launched


# --- random nets -------------------------------------------------------------------------


@dataclass
class Tok:
    kind: str
    n: int


KINDS = ("a", "b", "c")
KEYS = {"kind": lambda token: token.kind, "n": lambda token: token.n % 3}
ONE_TOKEN = ("take", "take_where", "snapshot", "mutate", "mutate_where", "read", "read_where")
MANY_TOKENS = ("take_list", "drain", "snapshot_list", "survey", "mutate_list", "read_all")
READS = ("read", "read_where", "read_all", "bound_read")
BOUND = ("bound_take", "bound_mutate", "bound_read")
SELECTORS = ("none", "none", "default", "default", "middle", "last", "decline", "reverse", "copy", "not_offered")


def _keyed(rng: random.Random) -> Filter:
    return Filter.where(kind=rng.choice(KINDS)) if rng.random() < 0.6 else Filter.where(n=rng.randint(0, 2))


def _edge(kind: str, place: str, transition: str, argument: str, where: Optional[Filter]):
    if kind in ("take", "take_list"):
        return ArgumentEdgeToTransition(place, transition, argument)
    if kind == "drain":
        return DrainEdge(place, transition, argument)
    if kind in ("take_where", "bound_take"):
        return TakeWhereEdge(place, transition, argument, where=where)
    if kind in ("snapshot", "snapshot_list"):
        return SnapshotEdge(place, transition, argument)
    if kind == "survey":
        return SurveyEdge(place, transition, argument)
    if kind in ("mutate", "mutate_list"):
        return MutateEdge(place, transition, argument)
    if kind in ("mutate_where", "bound_mutate"):
        return MutateWhereEdge(place, transition, argument, where=where)
    if kind == "read":
        return ReadEdge(place, transition, argument)
    if kind in ("read_where", "bound_read"):
        return ReadWhereEdge(place, transition, argument, where=where)
    assert kind == "read_all"
    return ReadAllEdge(place, transition, argument)


def _inputs(rng: random.Random, places: list[str]) -> list[tuple[str, str, Optional[Filter]]]:
    """One to three input edges, as (kind, place, filter), that a built net accepts: a
    read edge's place, and a bound edge's place and its source's, have no other edge
    of the transition."""
    chosen: list[tuple[str, str, Optional[Filter]]] = []
    alone: set[str] = set()  # places no further edge of the transition may name
    used: dict[str, int] = {}
    for _ in range(rng.randint(1, 3)):
        roll = rng.random()
        if not chosen and roll < 0.85:  # mostly a transition that consumes
            kind = rng.choice(("take", "take", "take_where", "take_list", "drain"))
        elif chosen and roll < 0.2:
            kind = rng.choice(BOUND)
        else:
            kind = rng.choice(ONE_TOKEN + MANY_TOKENS)
        if kind in BOUND:
            sources = [p for k, p, _where in chosen if k in ONE_TOKEN and used[p] == 1 and p not in alone]
            free = [p for p in places if p not in used]
            if not sources or not free:
                continue
            source = rng.choice(sources)
            place = rng.choice(free)
            key = rng.choice(("kind", "n"))
            clauses = (Clause(key, input=source, input_key=key),)
            if rng.random() < 0.3:
                clauses += (Clause("kind", (rng.choice(KINDS),)),)
            chosen.append((kind, place, Filter(clauses)))
            alone.update((source, place))
            used[place] = 1
            continue
        options = [p for p in places if p not in alone and (kind not in READS or p not in used)]
        if not options:
            continue
        place = rng.choice(options)
        where = _keyed(rng) if kind in ("take_where", "mutate_where", "read_where") else None
        chosen.append((kind, place, where))
        used[place] = used.get(place, 0) + 1
        if kind in READS:
            alone.add(place)
    return chosen


def _body(name: str, kinds: list[str], log: list, asynchronous: bool, output: bool):
    """A body of arguments a0, a1, ... annotated as the edges want, that logs its run,
    adds one to every token a mutate edge hands it, and returns a token made from its
    inputs if it has an output place, else None."""
    arguments = [f"a{i}" for i in range(len(kinds))]
    lists = {f"a{i}" for i, kind in enumerate(kinds) if kind in MANY_TOKENS}
    mutated = {f"a{i}" for i, kind in enumerate(kinds) if kind.startswith("mutate") or kind == "bound_mutate"}

    def result(given: dict) -> tuple[Optional[Tok], int]:
        total = 0
        for argument, value in given.items():
            tokens = value if argument in lists else [value]
            total += len(tokens) + sum(token.n for token in tokens)
            if argument in mutated:
                for token in tokens:
                    token.n += 1
        log.append(("body", name, total))
        return (Tok(KINDS[total % 3], total % 5) if output else None), total % 3

    async def run_async(**given):
        made, pause = result(given)
        for _ in range(pause):
            await asyncio.sleep(0)
        return made

    def run_sync(**given):
        return result(given)[0]

    signature = ", ".join(arguments)
    passed = ", ".join(f"{argument}={argument}" for argument in arguments)
    source = (
        f"async def body({signature}):\n    return await run({passed})\n"
        if asynchronous
        else f"def body({signature}):\n    return run({passed})\n"
    )
    scope: dict = {"run": run_async if asynchronous else run_sync}
    exec(source, scope)
    body = scope["body"]
    body.__annotations__ = {
        **{argument: list[Tok] if argument in lists else Tok for argument in arguments},
        "return": Tok if output else None,
    }
    return body


def _selector(kind: str, log: list):
    def recorded(choose):
        def selector(graph, fireable):
            log.append(("offered", [t.name for t in fireable]))
            chosen = choose(graph, fireable)
            log.append(("chose", getattr(chosen, "name", chosen)))
            return chosen
        return selector

    return recorded({
        "default": default_transition_selector,
        "middle": lambda graph, fs: fs[len(fs) // 2],
        "last": lambda graph, fs: fs[-1],
        "decline": lambda graph, fs: None if len(fs) >= 3 else fs[0],
        "reverse": lambda graph, fs: (fs.reverse(), fs[0])[1],
        "copy": lambda graph, fs: fs[0].model_copy(),
        "not_offered": lambda graph, fs: graph.transitions[0],
    }[kind])


def _net(seed: int, asynchronous: bool, log: list) -> tuple[ExecutableGraph, dict, list[Fire], int]:
    """A random net, the RunContext arguments to run it with, the Fire commands in its
    inbox at the start, and the cap on its firings."""
    rng = random.Random(seed)
    places = [f"P{i}" for i in range(rng.randint(2, 5))]
    parts: list = [
        ListPlaceNode(
            name, Tok, [Tok(rng.choice(KINDS), rng.randint(0, 5)) for _ in range(rng.randint(0, 6))], keys=KEYS,
        )
        for name in places
    ]
    names = [f"T{i}" for i in range(rng.randint(2, 8))]
    for name in names:
        inputs = _inputs(rng, places)
        kinds = [kind for kind, _place, _where in inputs]
        guard = priority = None
        if rng.random() < 0.4:
            watched, most = rng.choice(places), rng.randint(0, 3)

            def guard(graph, name=name, watched=watched, most=most):
                log.append(("guard", name))
                return len(graph.place_named(watched).tokens) <= most
        if rng.random() < 0.4:
            watched, weight = rng.choice(places), rng.choice((-1.0, 1.0, 2.0))

            def priority(graph, name=name, watched=watched, weight=weight):
                log.append(("priority", name))
                return weight * len(graph.place_named(watched).tokens)
        output = rng.random() < 0.8
        body = _body(name, kinds, log, asynchronous, output)
        parts.append(FunctionTransitionNode(name, body, guard=guard, priority=priority))
        parts.extend(_edge(kind, place, name, f"a{i}", where) for i, (kind, place, where) in enumerate(inputs))
        if output:
            parts.append(ReturnedEdgeFromTransition(name, rng.choice(places)))
    graph = construct(parts, tie_break=rng.choice(("first_declared", "last_declared", "round_robin")))
    selector = rng.choice(SELECTORS)
    if selector != "none":
        graph.transition_selector = _selector(selector, log)
    context = {
        "disabled": {rng.choice(names)} if rng.random() < 0.2 else set(),
        "error_after_n_firings": rng.randint(3, 30) if rng.random() < 0.15 else None,
    }
    fires = [Fire(rng.choice(names)) for _ in range(rng.choice((0, 0, 1, 2)))]
    return graph, context, fires, rng.randint(1, 40)


class _InLaunchOrder:
    """``asyncio`` for the runtime module, but for ``wait``, which hands back the tasks
    that have finished in the order they were launched. The concurrent loop lands them
    in the order it is handed them, and a set of tasks has no fixed order, so without
    this two runs of one net could land them differently and their token ids differ."""

    def __init__(self, launched: dict):
        self._launched = launched

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait(self, tasks, **kwargs):
        done, pending = await asyncio.wait(tasks, **kwargs)
        return sorted(done, key=self._launched.__getitem__), pending


def _run(seed: int, asynchronous: bool, reference: bool, monkeypatch) -> dict:
    """Run net ``seed`` concurrently, under the sweep as it is or the reference sweep;
    what happened, in order, and the marking it left."""
    log: list = []
    graph, context, fires, cap = _net(seed, asynchronous, log)
    launch, fire_now = runtime._launch, runtime._fire_now
    launched: dict = {}

    def recorded_launch(ctx, transition, *args, **kwargs):
        log.append(("launch", transition.name))
        task, firing_ids, given = launch(ctx, transition, *args, **kwargs)
        launched[task] = len(launched)
        return task, firing_ids, given

    def recorded_fire_now(ctx, cmd):
        fired = fire_now(ctx, cmd)
        log.append(("fire", cmd.transition, fired))
        return fired

    with monkeypatch.context() as patch:
        if reference:
            patch.setattr(runtime, "_launch_sweep", _reference_sweep)
            patch.setattr(runtime, "_launchable", _reference_launchable)
            patch.setattr(runtime, "_holds_of", lambda flight_state: flight_state)
        patch.setattr(runtime, "_launch", recorded_launch)
        patch.setattr(runtime, "_fire_now", recorded_fire_now)
        patch.setattr(runtime, "asyncio", _InLaunchOrder(launched))

        async def go():
            inbox: asyncio.Queue = asyncio.Queue()
            for command in fires:
                inbox.put_nowait(command)
            calls = 0

            def observer(_graph):
                nonlocal calls
                calls += 1
                if calls in (3, 6):  # a Fire drained while bodies may be in flight
                    inbox.put_nowait(Fire(graph.transitions[calls % len(graph.transitions)].name))

            ctx = RunContext(
                graph=graph, mode=ExecutionMode.CONCURRENT, inbox=inbox, observers=(observer,), **context,
            )
            try:
                summary = await Runner.run(ctx, stop_after_n_firings=cap)
                return ("returned", summary.quiesced, summary.n_fired)
            except Exception as error:  # noqa: BLE001 - compared, not handled
                # A repr names a body by its address, which differs between the two nets.
                return ("raised", type(error).__name__, re.sub(r" at 0x[0-9a-f]+", "", str(error)))

        ending = asyncio.run(go())
    marking = {
        place.name: ([(token.kind, token.n) for token in place.tokens], list(place.token_ids))
        for place in graph.places
    }
    return {"ending": ending, "log": log, "marking": marking, "fired": dict(graph.fired_counts)}


@pytest.mark.parametrize("asynchronous", [False, True], ids=["synchronous", "asynchronous"])
def test_the_sweep_does_what_planning_everything_again_did(asynchronous, monkeypatch):
    """Sixty random nets: shared places, takes, list-mode takes, drains, snapshots,
    surveys, mutate and read edges, keyed and bound edges, priorities, guards that read
    other places' token counts, every tie-break, selectors of the program's own (one
    that reorders the list it is handed, one that returns a copy, one that returns a
    transition it was not offered), a transition switched off, the fuse, and ``Fire``
    commands, some drained while bodies are in flight. Synchronous bodies land before
    the next sweep; asynchronous ones wait a few turns of the loop, so later sweeps run
    beside the claims of firings still in flight."""
    launches = 0
    for seed in range(60):
        expected = _run(seed, asynchronous, True, monkeypatch)
        actual = _run(seed, asynchronous, False, monkeypatch)
        assert actual == expected, f"net {seed}"
        launches += sum(1 for entry in actual["log"] if entry[0] == "launch")
    assert launches > 400  # the nets do launch


# --- the cases a kept plan could get wrong -----------------------------------------------


def _same(x: int) -> int:
    return x


@pytest.mark.asyncio
async def test_of_two_transitions_after_one_token_only_the_first_launches():
    """``First`` launches and takes the token; ``Second``'s plan, made on the same
    marking, names the token that is gone, and is made again: it does not launch."""
    graph = construct([
        ListPlaceNode("In", int, [1]), ListPlaceNode("Out", int),
        FunctionTransitionNode("First", _same), FunctionTransitionNode("Second", _same),
        ArgumentEdgeToTransition("In", "First", "x"), ArgumentEdgeToTransition("In", "Second", "x"),
        ReturnedEdgeFromTransition("First", "Out"), ReturnedEdgeFromTransition("Second", "Out"),
    ])
    summary = await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
    assert summary.quiesced is True
    assert graph.fired_counts == {"First": 1}
    assert graph.place_named("Out").tokens == [1]


@pytest.mark.asyncio
async def test_a_launch_that_empties_the_place_a_guard_reads_stops_the_guarded_transition():
    """``Gated`` takes from a place of its own, and its guard asks for a token in
    ``Signal``, which ``Clear``, launched first, takes. Neither plan reads the other's
    places, so only asking the guard again keeps ``Gated`` from launching."""
    asked: list[int] = []

    def signal_up(graph) -> bool:
        asked.append(len(graph.place_named("Signal").tokens))
        return bool(graph.place_named("Signal").tokens)

    graph = construct([
        ListPlaceNode("Signal", int, [1]), ListPlaceNode("Work", int, [7]), ListPlaceNode("Out", int),
        FunctionTransitionNode("Clear", _same, priority=lambda g: 1.0),
        FunctionTransitionNode("Gated", _same, guard=signal_up),
        ArgumentEdgeToTransition("Signal", "Clear", "x"), ArgumentEdgeToTransition("Work", "Gated", "x"),
        ReturnedEdgeFromTransition("Clear", "Out"), ReturnedEdgeFromTransition("Gated", "Out"),
    ])
    summary = await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
    assert summary.quiesced is True
    assert graph.fired_counts == {"Clear": 1}
    assert graph.place_named("Work").tokens == [7]
    assert asked[:2] == [1, 0]  # asked before the launch, then again after it


@pytest.mark.asyncio
async def test_a_launch_that_reads_a_token_holds_back_a_consumer_of_it_in_the_same_sweep(monkeypatch):
    """``Reader`` takes only its gate, and reads the front token of ``Shared``; the
    launch changes nothing ``Taker``'s plan reads, but its read holds the token
    ``Taker`` would take, so ``Taker``'s claims are checked again and it waits."""
    order: list[str] = []

    async def reader(box: int, _gate: int) -> int:
        order.append("read starts")
        await asyncio.sleep(0.01)
        order.append("read ends")
        return box

    def taker(box: int) -> int:
        order.append(f"take {box}")
        return box

    graph = construct([
        ListPlaceNode("Shared", int, [5]), ListPlaceNode("Gate", int, [0]), ListPlaceNode("Out", int),
        FunctionTransitionNode("Reader", reader), FunctionTransitionNode("Taker", taker),
        ReadEdge("Shared", "Reader", "box"), ArgumentEdgeToTransition("Gate", "Reader", "_gate"),
        ArgumentEdgeToTransition("Shared", "Taker", "box"),
        ReturnedEdgeFromTransition("Reader", "Out"), ReturnedEdgeFromTransition("Taker", "Out"),
    ])
    launched: list[list[str]] = []
    sweep = runtime._launch_sweep

    def watched(ctx, *args, **kwargs):
        count = sweep(ctx, *args, **kwargs)
        launched.append(sorted(set(args[4].values())))  # in_flight after the sweep
        return count

    monkeypatch.setattr(runtime, "_launch_sweep", watched)
    summary = await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT))
    assert summary.quiesced is True
    assert launched[0] == ["Reader"]
    assert order == ["read starts", "read ends", "take 5"]


@dataclass
class Order:
    courier: str


@dataclass
class Courier:
    name: str


@pytest.mark.asyncio
@pytest.mark.parametrize("source_edge", [ArgumentEdgeToTransition, ReadEdge], ids=["taken", "read"])
async def test_a_bound_edge_is_planned_again_when_a_launch_takes_from_its_source(source_edge):
    """``Load`` pairs an order with the courier it names: the oldest order with a
    courier, ``y`` at the middle of ``Orders``. ``Cancel``, launched first, takes the
    front order, so the order ``y`` moves to the front; a plan kept from before would
    hand ``Load`` the order behind it, ``z``, beside courier ``y``."""
    paired: list[tuple[str, str]] = []

    def load(order: Order, courier: Courier) -> str:
        paired.append((order.courier, courier.name))
        return courier.name

    graph = construct([
        ListPlaceNode("Orders", Order, [Order("x"), Order("y"), Order("z")], keys={"courier": lambda o: o.courier}),
        ListPlaceNode("Couriers", Courier, [Courier("y")], keys={"name": lambda c: c.name}),
        ListPlaceNode("Cancelled", Order), ListPlaceNode("Loaded", str),
        FunctionTransitionNode("Cancel", lambda order: order, priority=lambda g: 1.0),
        FunctionTransitionNode("Load", load),
        ArgumentEdgeToTransition("Orders", "Cancel", "order"),
        source_edge("Orders", "Load", "order"),
        TakeWhereEdge("Couriers", "Load", "courier", where=Filter((Clause("name", input="Orders", input_key="courier"),))),
        ReturnedEdgeFromTransition("Cancel", "Cancelled"), ReturnedEdgeFromTransition("Load", "Loaded"),
    ])
    await Runner.run(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT), stop_after_n_firings=2)
    assert [order.courier for order in graph.place_named("Cancelled").tokens] == ["x"]
    assert paired == [("y", "y")]
