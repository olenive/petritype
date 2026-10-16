"""Keyed input edges (``TOKEN_QUERYING_AND_FILTERING.md`` R5, Python track phase
3): ``TakeWhereEdge`` and ``MutateWhereEdge`` act on the oldest token a filter
keeps, leaving every other token where it is, with its id."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.token_view_filtering.wildlife import Species, build_wildlife, wildlife  # noqa: E402
from petritype.core.events import Fired, Mutated  # noqa: E402
from petritype.core.executable_graph_components import (  # noqa: E402
    ArgumentEdgeToTransition,
    ExecutableGraphCheck,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    NetParams,
    ReturnedEdgeFromTransition,
    TransitionFailedError,
)
from petritype.core.keys import Clause, Filter, MutateWhereEdge, TakeWhereEdge  # noqa: E402
from petritype.runner.owner import Owner  # noqa: E402
from petritype.runner.registry import Registry  # noqa: E402


def run(coroutine):
    return asyncio.run(coroutine)


def names(graph, place):
    return [s.name for s in graph.place_named(place).tokens]


def fired_events(graph):
    return [e for e in graph.drain_events() if isinstance(e, Fired)]


class TestTakeWhere:

    def test_migrate_takes_the_bird_alone_and_leaves_the_fox_with_its_id(self):
        graph = build_wildlife()  # autumn by default
        forest = graph.place_named("Forest")
        others = forest.token_ids[1:]
        graph.drain_events()
        run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
        assert graph.last_fired == "Migrate Forest Wetland"
        (fired,) = fired_events(graph)
        assert fired.consumed == (("Forest", 1),) and [p for p, _ in fired.produced] == ["Migrating"]
        assert names(graph, "Forest") == ["Fox", "Deer", "Woodpecker"]
        assert forest.token_ids == others, "the fox, the deer and the woodpecker never moved"
        assert names(graph, "Migrating") == ["Robin"]

    def test_fireable_only_with_a_matching_token(self):
        graph = build_wildlife()
        NetParams.set(graph, "season", "Autumn")
        migrate = graph.transition_named("Migrate Forest Wetland")
        maps = ExecutableGraphOperations.maps(graph)
        assert ExecutableGraphCheck.transition_is_fireable(migrate, graph, maps[1], maps[0], maps[3])
        forest = graph.place_named("Forest")
        forest.tokens[:] = [s for s in forest.tokens if s.class_ != "Bird"]
        assert not ExecutableGraphCheck.transition_is_fireable(migrate, graph, maps[1], maps[0], maps[3]), \
            "a fox and a deer: present, but matching nothing"
        assert ExecutableGraphCheck.sufficient_tokens_are_available(migrate, maps[1], maps[0], maps[3]), \
            "without the graph, presence alone is judged"

    def test_the_guard_still_decides(self):
        graph = build_wildlife()
        NetParams.set(graph, "season", "Summer")
        _, fired = run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
        assert graph.last_fired == "Spread Savanna Wetland" and names(graph, "Migrating") == [], "not autumn"

    def test_the_oldest_matching_token_is_taken_wherever_it_stands(self):
        graph = build_wildlife()
        forest = graph.place_named("Forest")
        forest.tokens.insert(0, Species(name="Boar", class_="Mammal", diet="Omnivore", population=9))
        forest.tokens.append(Species(name="Owl", class_="Bird", diet="Carnivore", population=2))
        run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
        assert names(graph, "Migrating") == ["Robin"]
        assert names(graph, "Forest") == ["Boar", "Fox", "Deer", "Woodpecker", "Owl"]
        run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
        assert names(graph, "Migrating") == ["Robin", "Woodpecker"], "the next bird, behind the fox and the deer"
        assert names(graph, "Forest") == ["Boar", "Fox", "Deer", "Owl"]

    def test_the_tundra_gives_up_its_owl_from_the_back(self):
        graph = build_wildlife()
        graph.disabled.add("Migrate Forest Wetland")
        run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
        assert graph.last_fired == "Migrate Tundra Wetland"
        assert names(graph, "Migrating") == ["Snowy Owl"] and names(graph, "Tundra") == ["Fox", "Arctic Hare", "Lemming"]

    def test_a_param_clause_on_the_edge(self):
        taken = []

        def take(item: str) -> None:
            taken.append(item)

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Pool", str, ["a", "b", "c"], keys={"letter": lambda s: s}),
            TakeWhereEdge("Pool", "Take", "item", where=Filter((Clause("letter", param="wanted"),))),
            FunctionTransitionNode("Take", take),
        ], params={"wanted": "b"})
        run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=None))
        assert taken == ["b"] and graph.place_named("Pool").tokens == ["a", "c"]

    def test_restore_on_failure_puts_the_token_back_at_the_front(self):
        def fail(item: str) -> None:
            raise RuntimeError("no")

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Pool", str, ["a", "b", "c"], keys={"letter": lambda s: s}),
            TakeWhereEdge("Pool", "Take", "item", where=Filter.where(letter="b")),
            FunctionTransitionNode("Take", fail),
        ], restore_tokens_on_failure=True)
        ids = graph.place_named("Pool").token_ids
        with pytest.raises(TransitionFailedError):
            run(ExecutableGraphOperations.execute_graph(graph))
        assert graph.place_named("Pool").tokens == ["b", "a", "c"]
        assert graph.place_named("Pool").token_ids == (ids[1], ids[0], ids[2])

    def test_where_must_be_a_filter(self):
        with pytest.raises(TypeError, match="must be a Filter"):
            TakeWhereEdge("Pool", "Take", "item", where="letter")


class TestMutateWhere:

    def test_mutates_the_oldest_matching_token_and_records_it(self):
        def shout(item: list) -> None:
            item[0] = item[0].upper()

        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Pool", list, [["a"], ["b"], ["c"]], keys={"letter": lambda s: s[0].lower()}),
            MutateWhereEdge("Pool", "Shout", "item", where=Filter.where(letter="b")),
            FunctionTransitionNode("Shout", shout, guard=lambda g: g.place_named("Pool").tokens[1] == ["b"]),
        ], record_events=True)
        ids = graph.place_named("Pool").token_ids
        graph.drain_events()
        _, fired = run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=None))
        assert fired == 1 and graph.place_named("Pool").tokens == [["a"], ["B"], ["c"]]
        assert graph.place_named("Pool").token_ids == ids
        events = graph.drain_events()
        assert events[0] == Mutated("Pool", ids[1])
        assert isinstance(events[1], Fired) and events[1].consumed == ()

    def test_not_fireable_without_a_match(self):
        graph = ExecutableGraphOperations.construct_graph([
            ListPlaceNode("Pool", str, ["a"], keys={"letter": lambda s: s}),
            MutateWhereEdge("Pool", "Shout", "item", where=Filter.where(letter="z")),
            FunctionTransitionNode("Shout", lambda item: None),
        ])
        _, fired = run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=None))
        assert fired == 0


class TestOverTheWire:

    @pytest.mark.asyncio
    async def test_topology_launched_and_the_fox_untouched(self):
        from datetime import datetime, timezone
        owner = Owner(Registry.from_factories([wildlife]))
        now = datetime(2026, 10, 9, tzinfo=timezone.utc)

        async def send(n, method, params=None):
            out = await owner.handle({"id": str(n), "method": method, "params": params or {}}, now)
            lines = list(out.lines)
            while out.launch is not None:
                out = await owner.completed(await out.launch.coroutine, now)
                lines.extend(out.lines)
            return lines

        await send(1, "load", {"net": "wildlife"})
        state = (await send(2, "get_state"))[-1]["result"]
        edge = next(e for e in state["topology"]["edges"] if e["transition"] == "Migrate Forest Wetland" and e["direction"] == "in")
        assert edge == {"place": "Forest", "transition": "Migrate Forest Wetland", "direction": "in",
                        "kind": "take_where", "filter": {"clauses": [{"key": "class", "values": ["Bird"]}]}}
        decline = next(e for e in state["topology"]["edges"] if e["transition"] == "Decline Forest" and e["direction"] == "in")
        assert decline == {"place": "Forest", "transition": "Decline Forest", "direction": "in",
                           "kind": "mutate_where", "filter": {"clauses": [{"key": "diet", "values": ["Herbivore"]}]}}
        robin, fox, deer, woodpecker = state["marking"]["places"]["Forest"]["tokens"]
        lines = await send(3, "step")  # autumn by default
        assert lines[0]["method"] == "launched" and lines[0]["params"]["consumed"] == [["Forest", robin["id"]]]
        after = (await send(4, "get_state"))[-1]["result"]
        assert [t["id"] for t in after["marking"]["places"]["Forest"]["tokens"]] == [fox["id"], deer["id"], woodpecker["id"]]
