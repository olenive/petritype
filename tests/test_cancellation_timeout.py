"""A concurrent run that is ending cancels the bodies still in flight and waits for
them, for the net's ``cancellation_timeout_seconds`` at most: a body that catches
its cancellation and carries on no longer keeps the run from ending. A stop and a
cancellation from outside cancel them at once; a run that ends on an error, a
failed firing's included, first lets them land for as long, and cancels those
still running then (since 2026-11-28, ``tests/test_concurrent_run_lands_in_flight.py``).
Past the timeout the run says which bodies did not stop, in a note on the error it
was ending with or, when it was ending with none, in a ``CancellationTimeoutError``;
at three fifths of the timeout it logs a warning that names them."""

import asyncio
import logging
import time

import pytest
from pydantic import ValidationError

from petritype import (
    ArgumentEdgeToTransition,
    CancellationTimeoutError,
    ExecutableGraph,
    ExecutableGraphOperations,
    ExecutionMode,
    FunctionTransitionNode,
    ListPlaceNode,
    PetritypeError,
    ReturnedEdgeFromTransition,
    RunContext,
    Runner,
    TransitionFailedError,
)


class Bodies:
    """The bodies of one net: ``Stubborn`` catches its cancellation and carries on
    until ``release`` is set; ``Tidy`` takes ``cleanup`` seconds to stop when it is
    cancelled; ``Fail`` raises once the other two have started."""

    def __init__(self, cleanup: float = 0.0):
        self.release = asyncio.Event()
        self.started = {"Stubborn": asyncio.Event(), "Tidy": asyncio.Event()}
        self.cleanup = cleanup
        self.tidied = False

    async def stubborn(self, x: int) -> int:
        self.started["Stubborn"].set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                pass
        return x

    async def tidy(self, x: int) -> int:
        self.started["Tidy"].set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(self.cleanup)
            self.tidied = True
            raise
        return x

    async def fail(self, x: int) -> int:
        for started in self.started.values():
            await started.wait()
        raise RuntimeError("boom")


def net(bodies: Bodies, transitions: tuple[str, ...], **settings) -> ExecutableGraph:
    functions = {"Stubborn": bodies.stubborn, "Tidy": bodies.tidy, "Fail": bodies.fail}
    for name in bodies.started:
        if name not in transitions:
            bodies.started[name].set()
    parts: list = [ListPlaceNode("Out", int)]
    for name in transitions:
        parts += [
            ListPlaceNode(f"In {name}", int, [1]),
            ArgumentEdgeToTransition(f"In {name}", name, "x"),
            FunctionTransitionNode(name, functions[name]),
            ReturnedEdgeFromTransition(name, "Out"),
        ]
    return ExecutableGraphOperations.construct_graph(parts, **settings)


def warnings_of(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


@pytest.mark.asyncio
async def test_a_failure_beside_a_body_that_will_not_stop_surfaces_after_two_timeouts_and_names_it(caplog):
    """The failure lets ``Stubborn`` land for the timeout, cancels it then, and waits
    the timeout again for it to stop; each wait is told in a note. Until 2026-11-28
    the failure cancelled it at once, and only the second wait was made."""
    bodies = Bodies()
    graph = net(bodies, ("Stubborn", "Fail"), cancellation_timeout_seconds=0.2)
    started = time.perf_counter()
    with caplog.at_level(logging.WARNING, logger="petritype.runtime"):
        with pytest.raises(TransitionFailedError) as raised:
            await asyncio.wait_for(
                Runner.run_to_completion(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)), timeout=5.0,
            )
    elapsed = time.perf_counter() - started
    bodies.release.set()
    await asyncio.sleep(0)

    assert 0.39 <= elapsed < 2.4
    not_landed, not_stopped = raised.value.__notes__
    assert "Stubborn" in not_landed and "cancelled, not landed" in not_landed
    assert "Stubborn" in not_stopped and "cancelled but still running" in not_stopped
    [warning] = warnings_of(caplog)
    assert "Stubborn" in warning and "0.12 s" in warning and "0.2 s" in warning
    assert graph.in_flight == set()


@pytest.mark.asyncio
async def test_a_stopped_run_whose_body_will_not_stop_raises_an_error_of_its_own():
    bodies = Bodies()
    graph = net(bodies, ("Stubborn",), cancellation_timeout_seconds=0.1)
    stop = asyncio.Event()
    run = asyncio.ensure_future(Runner.run_indefinitely(
        RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, stop=stop), tick=0.001,
    ))
    await asyncio.wait_for(bodies.started["Stubborn"].wait(), timeout=1.0)
    stop.set()
    with pytest.raises(CancellationTimeoutError) as raised:
        await asyncio.wait_for(run, timeout=2.0)
    bodies.release.set()
    await asyncio.sleep(0)

    assert raised.value.transitions == ("Stubborn",)
    assert isinstance(raised.value, PetritypeError) and isinstance(raised.value, RuntimeError)
    assert graph.in_flight == set()
    assert graph.place("Out").tokens == [], "what a body let go of returns goes nowhere"


@pytest.mark.asyncio
async def test_cleanup_that_ends_within_the_timeout_is_waited_for_and_nothing_is_said(caplog):
    """A stop cancels the body in flight at once. Until 2026-11-28 this net had
    ``Fail`` beside ``Tidy``, whose failure cancelled it; now that would wait for
    ``Tidy`` to land."""
    bodies = Bodies(cleanup=0.05)
    graph = net(bodies, ("Tidy",), cancellation_timeout_seconds=5.0)
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="petritype.runtime"):
        run = asyncio.ensure_future(Runner.run_indefinitely(
            RunContext(graph=graph, mode=ExecutionMode.CONCURRENT, stop=stop), tick=0.001,
        ))
        await asyncio.wait_for(bodies.started["Tidy"].wait(), timeout=1.0)
        stop.set()
        await asyncio.wait_for(run, timeout=2.0)  # no CancellationTimeoutError
    assert bodies.tidied, "the stopped run returned before the cancelled body had cleaned up"
    assert warnings_of(caplog) == []


@pytest.mark.asyncio
async def test_a_net_with_no_timeout_waits_for_cleanup_however_long_a_timeout_would_cut_it():
    """``None`` waits without limit: cleanup longer than a timeout would allow is
    waited for. The same net with that timeout lets go of the body. The caller
    cancels the run; until 2026-11-28 a failure beside ``Tidy`` did."""
    for timeout, tidied in ((None, True), (0.05, False)):
        bodies = Bodies(cleanup=0.2)
        graph = net(bodies, ("Tidy",), cancellation_timeout_seconds=timeout)
        run = asyncio.ensure_future(
            Runner.run_to_completion(RunContext(graph=graph, mode=ExecutionMode.CONCURRENT)),
        )
        await asyncio.wait_for(bodies.started["Tidy"].wait(), timeout=1.0)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert bodies.tidied is tidied
        await asyncio.sleep(0.25)  # let a body that was let go of finish its cleanup


def test_the_timeout_is_five_minutes_unless_the_net_says_otherwise_and_is_never_negative():
    bodies = Bodies()
    assert net(bodies, ("Fail",)).cancellation_timeout_seconds == 300.0
    assert net(bodies, ("Fail",), cancellation_timeout_seconds=12).cancellation_timeout_seconds == 12
    assert net(bodies, ("Fail",), cancellation_timeout_seconds=None).cancellation_timeout_seconds is None
    with pytest.raises(ValidationError):
        net(bodies, ("Fail",), cancellation_timeout_seconds=-1)
