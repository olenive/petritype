"""Tests for the two firing limits (SUGGESTIONS.md item 2) and the
`max_transitions` deprecation.

Pacing: `Runner.run(ctx, stop_after_n_firings=N)` always returns a `RunSummary`;
`quiesced` distinguishes "nothing left to fire" from "the limit stopped this
call". Safety: `RunContext.error_after_n_firings` is a run-wide fuse checked
*before* firing/launching, so `TooManyFiringsError` means the net attempted
firing n+1 — firing exactly n and quiescing is not an error.
"""

import pytest
from pydantic import ValidationError

from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.runtime import (
    ExecutionMode,
    RunContext,
    Runner,
    RunSummary,
    TooManyFiringsError,
)


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


class TestStopAfterNFirings:
    @pytest.mark.asyncio
    async def test_sequential_stop_reports_not_quiesced_and_resumes(self):
        graph = _chain_graph(3, [1])
        ctx = RunContext(graph=graph)
        first = await Runner.run(ctx, stop_after_n_firings=2)
        assert first.quiesced is False
        assert first.n_fired == 2
        assert first.fired == {'T1': 1, 'T2': 1}
        second = await Runner.run(ctx, stop_after_n_firings=2)
        assert second.quiesced is True
        assert second.n_fired == 1
        assert second.fired == {'T3': 1}
        assert graph.place_named('P3').tokens == [1]

    @pytest.mark.asyncio
    async def test_unbounded_run_quiesces(self):
        summary = await Runner.run(RunContext(graph=_chain_graph(3, [1])))
        assert summary.quiesced is True
        assert summary.n_fired == 3

    @pytest.mark.asyncio
    async def test_stop_exactly_at_quiescence_is_honestly_unknown(self):
        # fired == stop_after_n_firings cannot distinguish cut-off from completion,
        # so the summary says False; the next call proves quiescence with 0 firings.
        graph = _chain_graph(3, [1])
        ctx = RunContext(graph=graph)
        first = await Runner.run(ctx, stop_after_n_firings=3)
        assert first.n_fired == 3
        assert first.quiesced is False
        second = await Runner.run(ctx)
        assert second.quiesced is True
        assert second.n_fired == 0
        assert second.fired == {}

    @pytest.mark.asyncio
    async def test_concurrent_stop_caps_launches_and_resumes(self):
        graph = _chain_graph(3, [1])
        ctx = RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)
        first = await Runner.run(ctx, stop_after_n_firings=2)
        assert first.quiesced is False
        assert first.n_fired == 2
        assert graph.in_flight == set()  # capped launches still finish and deposit
        second = await Runner.run(ctx)
        assert second.quiesced is True
        assert graph.place_named('P3').tokens == [1]

    @pytest.mark.asyncio
    async def test_paced_totals_match_unbounded_run(self):
        ctx = RunContext(graph=_chain_graph(3, [1, 2]))
        totals: dict[str, int] = {}
        while True:
            summary = await Runner.run(ctx, stop_after_n_firings=1)
            for name, count in summary.fired.items():
                totals[name] = totals.get(name, 0) + count
            if summary.quiesced:
                break
        reference = await Runner.run(RunContext(graph=_chain_graph(3, [1, 2])))
        assert totals == reference.fired == {'T1': 2, 'T2': 2, 'T3': 2}

    @pytest.mark.asyncio
    async def test_stop_after_zero_fires_nothing(self):
        summary = await Runner.run(
            RunContext(graph=_chain_graph(1, [1])), stop_after_n_firings=0
        )
        assert summary.n_fired == 0
        assert summary.quiesced is False


class TestErrorAfterNFirings:
    @pytest.mark.asyncio
    async def test_sequential_fuse_raises_before_firing_n_plus_1(self):
        graph = _chain_graph(3, [1])
        ctx = RunContext(graph=graph, error_after_n_firings=2)
        with pytest.raises(TooManyFiringsError) as excinfo:
            await Runner.run(ctx)
        assert ctx.n_fired == 2
        assert graph.fired_counts == {'T1': 1, 'T2': 1}
        assert graph.place_named('P2').tokens == [1]  # T3 never consumed anything
        summary = excinfo.value.summary
        assert summary is not None
        assert summary.quiesced is False
        assert summary.n_fired == 2
        assert summary.fired == {'T1': 1, 'T2': 1}

    @pytest.mark.asyncio
    async def test_fuse_equal_to_quiescence_does_not_raise(self):
        ctx = RunContext(graph=_chain_graph(3, [1]), error_after_n_firings=3)
        summary = await Runner.run(ctx)
        assert summary.quiesced is True
        assert summary.n_fired == 3

    @pytest.mark.asyncio
    async def test_fuse_tally_spans_calls(self):
        ctx = RunContext(graph=_chain_graph(3, [1]), error_after_n_firings=2)
        await Runner.run(ctx, stop_after_n_firings=1)
        await Runner.run(ctx, stop_after_n_firings=1)  # spends the fuse, no attempt yet
        with pytest.raises(TooManyFiringsError):
            await Runner.run(ctx)

    @pytest.mark.asyncio
    async def test_concurrent_fuse_checks_before_launch(self):
        graph = _chain_graph(3, [1])
        ctx = RunContext(
            graph=graph, mode=ExecutionMode.CONCURRENT, error_after_n_firings=2
        )
        with pytest.raises(TooManyFiringsError) as excinfo:
            await Runner.run(ctx)
        assert graph.fired_counts == {'T1': 1, 'T2': 1}
        assert graph.in_flight == set()
        assert excinfo.value.summary.n_fired == 2

    @pytest.mark.asyncio
    async def test_fuse_raises_out_of_run_indefinitely(self):
        # Without the fuse this net would keep firing (and the call never returns).
        ctx = RunContext(graph=_chain_graph(1, [1, 2, 3]), error_after_n_firings=2)
        with pytest.raises(TooManyFiringsError) as excinfo:
            await Runner.run_indefinitely(ctx, tick=0.001)
        assert ctx.n_fired == 2
        assert excinfo.value.summary.fired == {'T1': 2}

    @pytest.mark.asyncio
    async def test_step_past_spent_fuse_raises_before_firing(self):
        graph = _chain_graph(1, [1, 2])
        ctx = RunContext(graph=graph, error_after_n_firings=1)
        assert await Runner.step(ctx) == 1
        with pytest.raises(TooManyFiringsError) as excinfo:
            await Runner.step(ctx)
        assert graph.fired_counts == {'T1': 1}
        assert excinfo.value.summary.n_fired == 0

    @pytest.mark.asyncio
    async def test_idle_net_past_spent_fuse_does_not_raise(self):
        # The fuse blows on an *attempt*, not on being spent: a quiet net stays quiet.
        ctx = RunContext(graph=_chain_graph(1, [1]), error_after_n_firings=1)
        first = await Runner.run(ctx)  # fires exactly 1, quiesces
        assert first.quiesced is True
        second = await Runner.run(ctx)  # nothing enabled — spent fuse must not raise
        assert second.n_fired == 0
        assert second.quiesced is True


class TestRunSummary:
    def test_frozen(self):
        summary = RunSummary(quiesced=True, n_fired=0, fired={})
        with pytest.raises(ValidationError):
            summary.quiesced = False


class TestDeprecatedMaxTransitions:
    @pytest.mark.asyncio
    async def test_max_transitions_warns_and_still_works(self):
        graph = _chain_graph(2, [1])
        with pytest.warns(DeprecationWarning, match="stop_after_n_firings"):
            graph, fired = await ExecutableGraphOperations.execute_graph(
                graph, max_transitions=1
            )
        assert fired == 1

    @pytest.mark.asyncio
    async def test_passing_both_raises(self):
        graph = _chain_graph(1, [1])
        with pytest.raises(TypeError):
            await ExecutableGraphOperations.execute_graph(
                graph, stop_after_n_firings=1, max_transitions=1
            )

    @pytest.mark.asyncio
    async def test_default_is_still_one_firing(self):
        graph, fired = await ExecutableGraphOperations.execute_graph(_chain_graph(2, [1]))
        assert fired == 1

    @pytest.mark.asyncio
    async def test_positional_second_argument_is_stop_after(self):
        # The new parameter took max_transitions' position (same meaning), so a
        # legacy positional call migrates silently — no warning.
        graph, fired = await ExecutableGraphOperations.execute_graph(_chain_graph(2, [1]), 2)
        assert fired == 2
