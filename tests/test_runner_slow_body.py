"""Characterisation test: what ``python -m petritype.runner`` writes while a slow
body runs, on the ``slow-body`` net (``tests/protocol_nets/slow_body.py``).

This pins the present behaviour, not a wish. The Python runner runs every
synchronous body on a thread, but it never writes ``launched`` (PROTOCOL.md §6.3,
§7.1): it writes one ``fired`` when the body lands, then the ``step`` response.
So while a Python body runs the wire carries nothing but heartbeats, whose
``executing_transition`` is the only sign that anything was taken from ``In``. A
viewer sees the consumed token still at rest until ``fired``. The Rust runner
writes ``launched`` the moment the inputs are taken. If the Python runner learns
``launched``, rewrite this test.

The process is spawned with ``Runner`` from ``test_runner_process.py`` (100 ms
heartbeat, stdout read on a thread).
"""

import os
import time
from pathlib import Path

from test_runner_process import Runner

ROOT = Path(__file__).resolve().parent.parent
DELAY_S = 1.0


def test_a_python_body_in_flight_shows_only_as_heartbeats(monkeypatch):
    # ``tests.protocol_nets.slow_body`` is imported from the repo root.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])))
    runner = Runner("--module", "tests.protocol_nets.slow_body", heartbeat_ms=100)
    try:
        describe = runner.request("describe")
        assert [net["name"] for net in describe[-1]["result"]["nets"]] == ["slow-body"]
        load = runner.request("load", {"net": "slow-body", "params": {"delay_s": DELAY_S}})
        assert load[-1]["result"]["status"] == "loaded"

        sent = time.monotonic()
        step_id = runner.send("step")
        lines = runner.until(lambda line: line.get("id") == step_id)
        elapsed = time.monotonic() - sent

        response, before = lines[-1], lines[:-1]
        assert response["result"]["fired"] is True
        assert response["result"]["transition"] == "Work"
        assert elapsed >= 0.8 * DELAY_S, f"step answered after {elapsed:.3f}s"

        working = [
            line for line in before
            if line.get("method") == "heartbeat" and line["params"]["executing_transition"] == "Work"
        ]
        assert working, f"no heartbeat named Work before the step response: {before}"
        assert all(line.get("method") != "launched" for line in before), before
        # Nothing but heartbeats until the body lands: then ``fired``, then the response.
        assert before[-1]["method"] == "fired"
        assert before[-1]["params"]["transition"] == "Work"
        assert all(line.get("method") == "heartbeat" for line in before[:-1]), before

        assert runner.request("shutdown")[-1]["result"] == {"status": "ok"}
        assert runner.finish() == 0
    finally:
        runner.kill()
