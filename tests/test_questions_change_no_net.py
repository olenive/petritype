"""A question changes no net (since 2026-11-27).

The library calls a transition's guard and priority, a place's key functions, a
transition selector and an output distribution function to decide what fires and
where a result goes. While one of them runs, the library's own functions refuse
every change to every net with ``ReadOnlyError``: a put, a take, setting a
parameter, loading a marking. Whatever asked fails, also when the function caught
the error: a sequential step, a concurrent run, a query, or, for an output
distribution function, the firing. Reading is not restricted, and once the
function has returned the program and the bodies write as before."""

import asyncio
import io
from contextlib import redirect_stderr
from datetime import datetime, timezone
from typing import Callable, Optional

import pytest

from petritype import (
    ControlSpec,
    ExecutableGraph,
    ExecutableGraphOperations,
    ExecutionMode,
    Filter,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    NestedNets,
    NetJson,
    PetritypeError,
    Query,
    ReadOnlyError,
    ReturnedEdgeFromTransition,
    RunContext,
    Runner,
    TakeWhereEdge,
    TransitionFailedError,
    apply_control,
    petri_net,
)
from petritype.core.executable_graph_components import NetParams
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry


def unit(open: bool = False) -> ExecutableGraph:
    """A net with one port, one private place and one parameter."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Orders In", str, ["standing order"], port=True),
        ListPlaceNode("Log", str),
    ], params={"speed": 1}, open=open)


def same(x):
    return x


def field(
    guard: Optional[Callable] = None,
    priority: Optional[Callable] = None,
    selector: Optional[Callable] = None,
    distribution: Optional[Callable] = None,
    body: Callable = same,
    open: bool = False,
) -> ExecutableGraph:
    """A net with one unit in ``Field`` and a transition ``Move`` that carries it
    to ``Done``, with the questions given."""
    graph = ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Field", ExecutableGraph, [unit(open)], nested=True, port=True),
        ListPlaceNode("Done", ExecutableGraph, nested=True),
        FunctionTransitionNode(
            "Move", body, guard=guard, priority=priority, output_distribution_function=distribution,
        ),
        MutateEdge("Field", "Move", "x", reaches=("Orders In",)),
        ReturnedEdgeFromTransition("Move", "Done"),
    ], params={"speed": 1}, open=open)
    graph.transition_selector = selector
    return graph


def the_unit(graph: ExecutableGraph) -> ExecutableGraph:
    return (graph.place("Field").tokens or graph.place("Done").tokens)[0]


def orders(graph: ExecutableGraph) -> list[str]:
    return list(the_unit(graph).place("Orders In").tokens)


async def step(graph: ExecutableGraph) -> None:
    await ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1)


async def run_concurrently(graph: ExecutableGraph) -> None:
    await asyncio.wait_for(
        Runner.run_to_completion(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)), timeout=5.0,
    )


# Each takes the net its question was asked about and asks to change a net.
WRITES = {
    "a put at a port": lambda graph: NestedNets.put(the_unit(graph), "Orders In", ["new"]),
    "a take at a port": lambda graph: NestedNets.take(the_unit(graph), "Orders In"),
    "a put into its own net": lambda graph: NestedNets.put(graph, "Field", [unit()]),
    "a parameter of its own net": lambda graph: NetParams.set(graph, "speed", 9),
    "a parameter of a net inside": lambda graph: NestedNets.set_param(the_unit(graph), "speed", 9),
    "loading a marking": lambda graph: NetJson.import_marking(graph, NetJson.export_marking(graph)),
}


def unchanged(graph: ExecutableGraph) -> bool:
    return (
        orders(graph) == ["standing order"] and graph.params == {"speed": 1}
        and the_unit(graph).params == {"speed": 1} and len(graph.place("Field").tokens) == 1
    )


class TestAGuardChangesNoNet:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("write", WRITES)
    async def test_a_sequential_step_fails_and_nothing_is_changed(self, write):
        def guard(graph):
            WRITES[write](graph)
            return True

        graph = field(guard=guard)
        with pytest.raises(ReadOnlyError) as raised:
            await step(graph)
        assert raised.value.asked == "the guard of transition 'Move'"
        assert "guard of transition 'Move'" in str(raised.value) and "transition's body" in str(raised.value)
        assert isinstance(raised.value, PetritypeError) and isinstance(raised.value, RuntimeError)
        assert unchanged(graph)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write", WRITES)
    async def test_a_concurrent_run_fails_and_nothing_is_changed(self, write):
        def guard(graph):
            WRITES[write](graph)
            return True

        graph = field(guard=guard)
        with pytest.raises(ReadOnlyError):
            await run_concurrently(graph)
        assert unchanged(graph)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("run", [step, run_concurrently])
    async def test_a_guard_that_catches_the_refusal_fails_all_the_same(self, run):
        def guard(graph):
            try:
                NestedNets.put(the_unit(graph), "Orders In", ["new"])
            except ReadOnlyError:
                pass
            return True

        graph = field(guard=guard)
        with pytest.raises(ReadOnlyError):
            await run(graph)
        assert unchanged(graph)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("run", [step, run_concurrently])
    async def test_a_guard_presses_no_control(self, run):
        """A control is the net's own, so its command lands at any place; a guard
        that presses one is refused all the same (since 2026-11-28)."""
        add = ControlSpec.inject("Done", lambda _pressed: unit(), "Add")

        def guard(graph):
            ctx = RunContext(graph=graph, inbox=asyncio.Queue(), strict=True)
            try:
                apply_control(ctx, add, True)
                Runner.drain_inbox(ctx)
            except PetritypeError:
                pass
            return True

        graph = field(guard=guard)
        with pytest.raises(ReadOnlyError):
            await run(graph)
        assert unchanged(graph) and graph.place("Done").tokens == []

    @pytest.mark.asyncio
    async def test_a_net_declared_open_is_no_exception(self):
        def guard(graph):
            NestedNets.put(the_unit(graph), "Log", ["new"])
            return True

        graph = field(guard=guard, open=True)
        with pytest.raises(ReadOnlyError) as raised:
            await step(graph)
        assert raised.value.act == "a put at place 'Log'"
        assert the_unit(graph).place("Log").tokens == []

    @pytest.mark.asyncio
    async def test_a_net_the_guard_built_itself_is_no_exception(self):
        def guard(graph):
            NestedNets.put(unit(), "Orders In", ["new"])
            return True

        with pytest.raises(ReadOnlyError):
            await step(field(guard=guard))

    @pytest.mark.asyncio
    async def test_a_guard_reads_whatever_it_likes(self):
        def guard(graph):
            inner = the_unit(graph)
            return (
                inner.place("Orders In").tokens == ["standing order"] and inner.params["speed"] == 1
                and not NestedNets.can_fire(inner) and len(Query.matches(inner)) == 1
            )

        graph = field(guard=guard)
        await step(graph)
        assert len(graph.place("Done").tokens) == 1

    @pytest.mark.asyncio
    async def test_once_the_guard_has_returned_the_program_and_the_body_write_as_before(self):
        def guard(graph):
            try:
                NestedNets.put(the_unit(graph), "Orders In", ["from the guard"])
            except ReadOnlyError:
                return False
            return True

        def body(x: ExecutableGraph) -> ExecutableGraph:
            NestedNets.put(x, "Orders In", ["from the body"])
            return x

        # Declared open, so that the program reaches the unit's port: a closed field
        # would shield it (since 2026-11-27).
        refused = field(guard=guard, open=True)
        with pytest.raises(ReadOnlyError):
            await step(refused)
        NestedNets.put(the_unit(refused), "Orders In", ["from the program"])
        assert orders(refused) == ["standing order", "from the program"]

        graph = field(guard=lambda graph: True, body=body)
        await step(graph)
        assert orders(graph) == ["standing order", "from the body"]


class TestTheOtherQuestionsChangeNoNet:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("run", [step, run_concurrently])
    async def test_a_priority(self, run):
        def priority(graph):
            NestedNets.put(the_unit(graph), "Orders In", ["new"])
            return 1.0

        graph = field(priority=priority)
        with pytest.raises(ReadOnlyError) as raised:
            await run(graph)
        assert raised.value.asked == "the priority of transition 'Move'"
        assert unchanged(graph)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("run", [step, run_concurrently])
    async def test_a_selector_of_the_nets_own(self, run):
        def selector(graph, fireable):
            NestedNets.put(the_unit(graph), "Orders In", ["new"])
            return fireable[0]

        graph = field(selector=selector)
        with pytest.raises(ReadOnlyError) as raised:
            await run(graph)
        assert raised.value.asked == "the transition selector"
        assert unchanged(graph)

    @pytest.mark.asyncio
    async def test_a_selector_the_caller_passes(self):
        def selector(graph, fireable):
            NetParams.set(graph, "speed", 9)
            return fireable[0]

        graph = field()
        with pytest.raises(ReadOnlyError):
            await ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1, transition_selector=selector)
        assert unchanged(graph)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("catches", [False, True])
    async def test_an_output_distribution_function_fails_its_firing(self, catches):
        def distribution(result):
            try:
                NestedNets.put(result, "Orders In", ["new"])
            except ReadOnlyError:
                if not catches:
                    raise
            return {"Done": result}

        graph = field(distribution=distribution)
        with pytest.raises(TransitionFailedError) as raised:
            await step(graph)
        assert isinstance(raised.value.__cause__, ReadOnlyError)
        assert raised.value.__cause__.asked == "the output distribution function of transition 'Move'"
        assert graph.place("Done").tokens == []

    @staticmethod
    def shelf(target: ExecutableGraph, catches: bool = False) -> ExecutableGraph:
        """A net whose place derives a key by a function that puts a token into
        ``target``, and a transition that takes by that key."""
        def kind(token: str) -> str:
            try:
                NestedNets.put(target, "Orders In", [token])
            except ReadOnlyError:
                if not catches:
                    raise
            return token[0]

        return ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Shelf", str, ["apple", "bread"], keys={"kind": kind}),
            ListPlaceNode("Out", str),
            FunctionTransitionNode("Pick", same),
            TakeWhereEdge("Shelf", "Pick", "x", where=Filter.where(kind="b")),
            ReturnedEdgeFromTransition("Pick", "Out"),
        ])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("run", [step, run_concurrently])
    @pytest.mark.parametrize("catches", [False, True])
    async def test_a_key_function_asked_for_a_keyed_edge(self, run, catches):
        target = unit()
        graph = self.shelf(target, catches)
        with pytest.raises(ReadOnlyError) as raised:
            await run(graph)
        assert raised.value.asked == "a key function of place 'Shelf'"
        assert target.place("Orders In").tokens == ["standing order"]
        assert graph.place("Shelf").tokens == ["apple", "bread"]

    @pytest.mark.parametrize("catches", [False, True])
    def test_a_key_function_asked_for_a_query(self, catches):
        target = unit()
        graph = self.shelf(target, catches)
        with pytest.raises(ReadOnlyError):
            Query.matches(graph, Filter.where(kind="a"))
        with pytest.raises(ReadOnlyError):
            Query.distinct(graph, "kind")
        assert target.place("Orders In").tokens == ["standing order"]

    @pytest.mark.asyncio
    async def test_a_guard_that_catches_what_a_key_function_it_asked_was_refused_fails_too(self):
        target = unit()
        shelf = self.shelf(target)

        def guard(graph):
            try:
                Query.matches(shelf)
            except ReadOnlyError:
                pass
            return True

        with pytest.raises(ReadOnlyError) as raised:
            await step(field(guard=guard))
        assert raised.value.asked == "a key function of place 'Shelf'"

    @pytest.mark.asyncio
    async def test_a_body_that_catches_what_a_key_function_it_asked_was_refused_fails_its_firing(self):
        target = unit()
        shelf = self.shelf(target)

        def body(x: ExecutableGraph) -> ExecutableGraph:
            try:
                Query.matches(shelf)
            except ReadOnlyError:
                pass
            return x

        graph = field(body=body)
        with pytest.raises(TransitionFailedError) as raised:
            await step(graph)
        assert isinstance(raised.value.__cause__, ReadOnlyError)
        assert target.place("Orders In").tokens == ["standing order"]


def guarded_by_a_writer(graph: ExecutableGraph) -> bool:
    NestedNets.put(the_unit(graph), "Orders In", ["new"])
    return True


@petri_net(name="writing-guard", description="A net whose guard asks to change a net")
def writing_guard() -> ExecutableGraph:
    return field(guard=guarded_by_a_writer)


class TestTheProtocolRunner:
    @pytest.mark.asyncio
    async def test_the_transition_cannot_fire_and_the_refusal_is_said_on_stderr_once(self):
        owner = Owner(Registry.from_factories([writing_guard]))
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        said = io.StringIO()
        with redirect_stderr(said):
            loaded = await owner.handle({"id": "1", "method": "load", "params": {"net": "writing-guard"}}, now)
            state = await owner.handle({"id": "2", "method": "get_state", "params": {}}, now)
            await owner.handle({"id": "3", "method": "get_state", "params": {}}, now)
        assert "result" in loaded.lines[-1]
        assert state.lines[-1]["result"]["transitions"]["Move"]["fireable"] is False
        [line] = said.getvalue().splitlines()
        assert line.startswith("petritype runner: a put at place 'Orders In'")
        assert "the guard of transition 'Move'" in line
