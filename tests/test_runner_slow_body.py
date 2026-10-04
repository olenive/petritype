"""What ``python -m petritype.runner`` writes while a slow body runs, on the
``slow-body`` net (``tests/protocol_nets/slow_body.py``).

Since 2026-10-04 this pins the two-piece firing shape (PROTOCOL.md §6.3, §7.1).
The runner writes ``launched`` the moment it hands a firing out, so the token
``Work`` takes leaves ``In`` on the wire at once; while the body sleeps the wire
carries heartbeats naming ``Work``, and a ``get_state`` is answered from the
marking ``launched`` describes; when the body lands it writes ``distributed``
and then the ``step`` response. (Until then this test characterised the old
gap: one ``fired`` at the landing, and nothing but heartbeats before it.)

The process is spawned with ``Runner`` from ``test_runner_process.py`` (100 ms
heartbeat, stdout read on a thread). The timings are printed (``pytest -s``).
"""

import os
import time
from pathlib import Path

from test_runner_process import Runner

ROOT = Path(__file__).resolve().parent.parent
DELAY_S = 1.0


def is_heartbeat(line: dict) -> bool:
    return line.get("method") == "heartbeat"


def test_a_python_body_is_launched_at_once_and_distributed_when_it_lands(monkeypatch):
    # ``tests.protocol_nets.slow_body`` is imported from the repo root.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])))
    runner = Runner("--module", "tests.protocol_nets.slow_body", heartbeat_ms=100)
    try:
        describe = runner.request("describe")
        assert [net["name"] for net in describe[-1]["result"]["nets"]] == ["slow-body"]
        load = runner.request("load", {"net": "slow-body", "params": {"delay_s": DELAY_S}})
        assert load[-1]["result"]["status"] == "loaded"
        before = runner.request("get_state")[-1]["result"]
        first_job = before["marking"]["places"]["In"]["tokens"][0]["id"]

        sent = time.monotonic()
        step_id = runner.send("step")

        # The first line after ``step`` is ``launched``; a heartbeat may only
        # race ahead of it, never another notification.
        lines = runner.until(lambda line: not is_heartbeat(line))
        launched_at = time.monotonic() - sent
        launched = lines[-1]
        assert launched["method"] == "launched", lines
        assert launched["params"]["transition"] == "Work"
        assert launched["params"]["consumed"] == [["In", first_job]]
        assert launched["params"]["marking_version"] == before["marking_version"] + 1

        # Mid-body: ``step`` is answered out of order, so this comes first, and
        # shows the marking ``launched`` describes.
        state_id = runner.send("get_state")
        lines = runner.until(lambda line: line.get("id") == state_id)
        assert all(is_heartbeat(line) for line in lines[:-1]), lines
        state = lines[-1]["result"]
        assert state["marking_version"] == launched["params"]["marking_version"]
        assert state["marking"]["places"]["In"]["count"] == 1
        assert state["marking"]["places"]["Out"]["count"] == 0
        assert first_job not in [token["id"] for token in state["marking"]["places"]["In"]["tokens"]]
        assert state["transitions"]["Work"]["in_flight"] is True
        assert state["transitions"]["Work"]["fireable"] is False

        # Then heartbeats naming ``Work`` until the landing: ``distributed``,
        # then the response.
        lines = runner.until(lambda line: not is_heartbeat(line))
        distributed_at = time.monotonic() - sent
        beats, distributed = lines[:-1], lines[-1]
        assert distributed["method"] == "distributed", lines
        assert distributed["params"]["transition"] == "Work"
        assert [place for place, *_ in distributed["params"]["produced"]] == ["Out"]
        assert distributed["params"]["marking_version"] == launched["params"]["marking_version"] + 1
        assert beats, "no heartbeat while the body ran"
        assert all(line["params"]["executing_transition"] == "Work" for line in beats), beats
        lines = runner.until(lambda line: line.get("id") == step_id)
        answered_at = time.monotonic() - sent
        assert all(is_heartbeat(line) for line in lines[:-1]), lines
        response = lines[-1]["result"]
        assert response["fired"] is True and response["transition"] == "Work"
        assert response["marking_version"] == distributed["params"]["marking_version"]

        print(
            f"\nlaunched after {launched_at * 1000:.1f} ms, distributed after "
            f"{distributed_at * 1000:.1f} ms, step answered after {answered_at * 1000:.1f} ms"
        )
        assert launched_at < 0.3 * DELAY_S, f"launched came after {launched_at:.3f}s"
        assert answered_at >= 0.8 * DELAY_S, f"step answered after {answered_at:.3f}s"

        assert runner.request("shutdown")[-1]["result"] == {"status": "ok"}
        assert runner.finish() == 0
    finally:
        runner.kill()
