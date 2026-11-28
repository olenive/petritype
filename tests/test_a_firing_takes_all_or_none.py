"""A firing takes all its tokens or none (since 2026-11-28). Stage 1 makes every
copy it hands over, a snapshot's, a survey's and the token history's, before it
removes any token, so a copy that raises leaves the marking as it was: the firing
fails as a raising body does, with ``TransitionFailedError`` and the copy's error
as its cause, and a ``Failed`` record that tells the tokens it would have taken
as consumed and restored, with their ids, whatever ``restore_tokens_on_failure``
says. Until then the consumed token was lost, the error was the copy's own and
nothing was recorded. The nets of the tokens it hands a body live are asked for
before any token is removed too, so a ``__petritype_nets__`` that raises
anything but ``TypeError`` there fails the firing likewise (since 2026-11-28)."""

import asyncio
from datetime import datetime, timezone

import pytest

from petritype import petri_net
from petritype.core.events import Failed, Fired
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReadEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    SurveyEdge,
    TransitionFailedError,
)
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry
from petritype.runtime import ExecutionMode, RunContext, Runner


class Fragile:
    """A token that cannot be copied."""

    def __deepcopy__(self, memo):
        raise RuntimeError("this token cannot be copied")


def look(order: str, view: Fragile) -> str:
    return order


def look_at_all(order: str, views: list[Fragile]) -> str:
    return order


def take(order: Fragile) -> str:
    return "taken"


def looking(edge: str, restore: bool) -> ExecutableGraph:
    """``Look`` takes an order and copies a fragile view through a snapshot or a survey."""
    body, edge_node = {
        "snapshot": (look, SnapshotEdge("Views", "Look", "view")),
        "survey": (look_at_all, SurveyEdge("Views", "Look", "views")),
    }[edge]
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders", str, ["Order"]),
        ListPlaceNode("Views", Fragile, [Fragile()]),
        ListPlaceNode("Done", str),
        FunctionTransitionNode("Look", body),
        ArgumentEdgeToTransition("Orders", "Look", "order"),
        edge_node,
        ReturnedEdgeFromTransition("Look", "Done"),
    ], restore_tokens_on_failure=restore, record_events=True, open=True)


async def fire(graph: ExecutableGraph, how: str, **settings) -> None:
    if how == "execute_graph":
        await ExecutableGraphOperations.execute_graph(graph, **settings)
    else:
        await Runner.run(RunContext(graph=graph, mode=ExecutionMode(how)))


HOW = ["execute_graph", "sequential", "concurrent"]


def assert_untouched(graph: ExecutableGraph, failure: TransitionFailedError, place: str, ids: tuple) -> None:
    assert isinstance(failure.__cause__, RuntimeError) and "cannot be copied" in str(failure.__cause__)
    assert failure.restored, "nothing left its place"
    assert graph.place(place).token_ids == ids, "the token is where it was, with its id"
    events = graph.drain_events()
    assert not any(isinstance(event, Fired) for event in events)
    [failed] = [event for event in events if isinstance(event, Failed)]
    assert failed.consumed == tuple((place, token_id) for token_id in ids) and failed.restored
    assert failed.error == "RuntimeError: this token cannot be copied"


@pytest.mark.parametrize("how", HOW)
@pytest.mark.parametrize("edge", ["snapshot", "survey"])
@pytest.mark.parametrize("restore", [False, True], ids=["no restore", "restore"])
def test_a_copy_that_raises_fails_the_firing_and_takes_nothing(how, edge, restore):
    graph = looking(edge, restore)
    ids = graph.place("Orders").token_ids
    with pytest.raises(TransitionFailedError) as failed:
        asyncio.run(fire(graph, how))
    assert graph.place("Orders").tokens == ["Order"] and graph.place("Done").tokens == []
    assert failed.value.consumed == {"order": "Order"}
    assert_untouched(graph, failed.value, "Orders", ids)


def test_a_history_copy_that_raises_fails_the_firing_and_takes_nothing():
    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders", Fragile, [Fragile()]),
        ListPlaceNode("Done", str),
        FunctionTransitionNode("Take", take),
        ArgumentEdgeToTransition("Orders", "Take", "order"),
        ReturnedEdgeFromTransition("Take", "Done"),
    ], allow_token_copying=True, record_events=True, open=True)
    ids = graph.place("Orders").token_ids
    with pytest.raises(TransitionFailedError) as failed:
        asyncio.run(fire(graph, "execute_graph", token_history_length=1))
    assert len(graph.place("Orders").tokens) == 1 and graph.place("Done").tokens == []
    assert_untouched(graph, failed.value, "Orders", ids)


class Moody:
    """A token that carries a net, and whose ``__petritype_nets__`` raises once
    when ``fail_next`` is set: what stage 1 meets when it asks which nets the
    token lends the body."""

    def __init__(self) -> None:
        self.net = ExecutableGraphOperations.construct_graph([ListPlaceNode("In", int, port=True)])
        self.fail_next = False

    def __petritype_nets__(self) -> dict[str, ExecutableGraph]:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("cannot say which nets it carries")
        return {"net": self.net}


def arming(unit: Moody):
    """A guard that passes and arms the unit, so its next answer, stage 1's, raises."""
    def guard(_graph: ExecutableGraph) -> bool:
        unit.fail_next = True
        return True
    return guard


LENDING = {
    "take": lambda: ArgumentEdgeToTransition("Units", "Use", "unit"),
    "mutate": lambda: MutateEdge("Units", "Use", "unit"),
    "read": lambda: ReadEdge("Units", "Use", "unit"),
}


@pytest.mark.parametrize("how", HOW)
@pytest.mark.parametrize("edge", LENDING)
@pytest.mark.parametrize("restore", [False, True], ids=["no restore", "restore"])
def test_a_token_that_cannot_say_which_nets_it_lends_fails_the_firing_and_takes_nothing(how, edge, restore):
    """Until 2026-11-28 the nets were asked for after the tokens were removed: the
    error was the token's own, and the order and a unit taken were lost."""
    unit = Moody()
    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Units", Moody, [unit], nested=True),
        ListPlaceNode("Orders", str, ["Order"]),
        ListPlaceNode("Done", str),
        FunctionTransitionNode("Use", lambda unit, order: "done", guard=arming(unit)),
        LENDING[edge](),
        ArgumentEdgeToTransition("Orders", "Use", "order"),
        ReturnedEdgeFromTransition("Use", "Done"),
    ], restore_tokens_on_failure=restore, record_events=True, open=True)
    orders, units = graph.place("Orders").token_ids, graph.place("Units").token_ids
    with pytest.raises(TransitionFailedError) as failed:
        asyncio.run(fire(graph, how))
    assert isinstance(failed.value.__cause__, RuntimeError) and failed.value.restored
    assert graph.place("Orders").tokens == ["Order"] and graph.place("Orders").token_ids == orders
    assert graph.place("Units").tokens == [unit] and graph.place("Units").token_ids == units
    assert graph.place("Done").tokens == [] and not graph.in_flight
    assert not unit.net._holds, "nothing holds the unit's net"
    [failed_event] = [event for event in graph.drain_events() if isinstance(event, Failed)]
    taken = (("Units", units[0]),) if edge == "take" else ()
    assert sorted(failed_event.consumed) == sorted((("Orders", orders[0]), *taken)) and failed_event.restored
    assert failed_event.error == "RuntimeError: cannot say which nets it carries"


@petri_net(name="fragile-view", description="A snapshot of a token that cannot be copied")
def fragile_view() -> ExecutableGraph:
    return looking("snapshot", restore=False)


def test_the_protocol_runner_tells_it_as_a_failed_firing_whose_tokens_are_back():
    """``launched`` named the order as consumed; the landing says it failed and was
    restored, the order is still there with its id, and the log says the same."""
    async def go():
        owner = Owner(Registry.from_factories([fragile_view]))
        now = datetime(2026, 11, 28, tzinfo=timezone.utc)
        sent = [0]

        async def send(method, params=None):
            sent[0] += 1
            out = await owner.handle({"id": str(sent[0]), "method": method, "params": params or {}}, now)
            lines = list(out.lines)
            while out.launch is not None:
                out = await owner.completed(await out.launch.coroutine, now)
                lines.extend(out.lines)
            return lines

        await send("load", {"net": "fragile-view"})
        lines = await send("step")
        assert lines[-1]["result"]["failed"] is True
        [failed] = [line["params"] for line in lines if line.get("method") == "failed"]
        assert failed["consumed"] == [["Orders", "1"]] and failed["restored"] is True
        assert not any(line.get("method") == "reloaded" for line in lines)
        state = (await send("get_state"))[-1]["result"]
        assert [token["id"] for token in state["marking"]["places"]["Orders"]["tokens"]] == ["1"]
        [entry] = (await send("get_log"))[-1]["result"]["entries"]
        assert entry["outcome"] == "failed" and entry["restored"] is True
    asyncio.run(go())
