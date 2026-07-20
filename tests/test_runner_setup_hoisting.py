"""Per-entry-point setup in the Runner loops (SUGGESTIONS.md item 4).

Every Runner entry point validates the full marking and builds the adjacency
maps once at entry, then fires on those maps: an N-firing sequential run pays
the setup cost once, not N times, and concurrent mode — which previously never
validated at all — now rejects a bad marking at entry just like sequential.
"""

import asyncio

import pytest

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MapPlaceNames,
    ReturnedEdgeFromTransition,
)
from petritype.runtime import ExecutionMode, RunContext, Runner, SetTokens


def _chain_graph(n_transitions: int, tokens: list[int]):
    """P0 -T1-> P1 -T2-> ... -Tn-> Pn, each transition passing its token through."""

    def passthrough():
        async def body(x: int) -> int:
            return x

        return body

    components = [ListPlaceNode('P0', int, list(tokens))]
    for i in range(1, n_transitions + 1):
        components += [
            ArgumentEdgeToTransition(f'P{i - 1}', f'T{i}', 'x'),
            FunctionTransitionNode(f'T{i}', passthrough()),
            ReturnedEdgeFromTransition(f'T{i}', f'P{i}'),
            ListPlaceNode(f'P{i}', int),
        ]
    return ExecutableGraphOperations.construct_graph(components)


def _count_map_builds(monkeypatch) -> dict:
    """Wrap MapPlaceNames.to_list_place_nodes with a call counter — the proxy for
    how many times the per-run setup (maps + validation) was done."""
    calls = {'count': 0}
    original = MapPlaceNames.to_list_place_nodes

    def counting(graph):
        calls['count'] += 1
        return original(graph)

    monkeypatch.setattr(MapPlaceNames, 'to_list_place_nodes', counting)
    return calls


class TestSetupHoistedOncePerRun:
    @pytest.mark.asyncio
    async def test_sequential_run_builds_maps_once(self, monkeypatch):
        ctx = RunContext(graph=_chain_graph(10, [1]))
        calls = _count_map_builds(monkeypatch)
        summary = await Runner.run(ctx)
        assert summary.n_fired == 10
        assert calls['count'] == 1

    @pytest.mark.asyncio
    async def test_concurrent_run_builds_maps_once(self, monkeypatch):
        ctx = RunContext(graph=_chain_graph(10, [1]), mode=ExecutionMode.CONCURRENT)
        calls = _count_map_builds(monkeypatch)
        summary = await Runner.run(ctx)
        assert summary.n_fired == 10
        assert calls['count'] == 1

    @pytest.mark.asyncio
    async def test_set_tokens_mid_run_lands_on_the_hoisted_maps(self):
        # SetTokens rebinds place.tokens on the node object the hoisted maps
        # reference, so a replacement enqueued mid-run is consumed without any
        # map rebuild.
        graph = _chain_graph(1, [1])
        inbox = asyncio.Queue()
        injected = {'done': False}

        def refill(g):
            if not injected['done'] and g.fired_counts.get('T1') == 1:
                injected['done'] = True
                inbox.put_nowait(SetTokens('P0', [5]))

        ctx = RunContext(graph=graph, observers=(refill,), inbox=inbox)
        summary = await Runner.run(ctx)
        assert summary.fired == {'T1': 2}
        assert graph.place_named('P1').tokens == [1, 5]


class TestMarkingValidatedAtEntry:
    """Construction already rejects bad initial tokens, so only post-construction
    mutation (a caller or observer poking the live graph) can corrupt a marking —
    each entry point sweeps for that once, before anything fires."""

    def _corrupted(self):
        graph = _chain_graph(1, [1])
        graph.place_named('P0').tokens.append('rogue')  # int place
        return graph

    @pytest.mark.asyncio
    async def test_sequential_run_validates_at_entry(self):
        with pytest.raises(TypeError, match='P0'):
            await Runner.run(RunContext(graph=self._corrupted()))

    @pytest.mark.asyncio
    async def test_concurrent_run_validates_at_entry(self):
        # The consistency fix: concurrent mode used to skip validation entirely.
        ctx = RunContext(graph=self._corrupted(), mode=ExecutionMode.CONCURRENT)
        with pytest.raises(TypeError, match='P0'):
            await Runner.run(ctx)

    @pytest.mark.asyncio
    async def test_run_indefinitely_validates_at_entry(self):
        ctx = RunContext(graph=self._corrupted(), mode=ExecutionMode.CONCURRENT)
        with pytest.raises(TypeError, match='P0'):
            await Runner.run_indefinitely(ctx, tick=0.001)

    @pytest.mark.asyncio
    async def test_step_validates_before_firing(self):
        graph = self._corrupted()
        with pytest.raises(TypeError, match='P0'):
            await Runner.step(RunContext(graph=graph))
        assert graph.fired_counts == {}
