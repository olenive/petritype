"""``python -m petritype.runner``: the process layer, end to end over pipes, and its
pure pieces in process.

The process tests spawn the runner with a 100 ms heartbeat and read its stdout
on a thread, line by line, waiting for an expected line with a generous timeout
rather than sleeping. The nets are in ``tests/runner_nets``, which is put
on the child's ``PYTHONPATH``.
"""

import io
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from petritype.runner.owner import Owner
from petritype.runner.process import (
    Arguments,
    CommandLine,
    DEFAULT_HEARTBEAT_S,
    Frames,
    Gauges,
    ProtocolStream,
    Reading,
    UsageError,
)
from petritype.runner.protocol import Malformed, Request
from petritype.runner.registry import Registry

NETS = Path(__file__).parent / "runner_nets"
TIMEOUT = 30.0


# --- the process, over pipes ---------------------------------------------------------------


def child_env(heartbeat_ms: int = 100) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(NETS), env.get("PYTHONPATH")]))
    env["PETRITYPE_RUNNER_HEARTBEAT_MS"] = str(heartbeat_ms)
    env["PYTHONUNBUFFERED"] = "1"
    return env


class Runner:
    """A runner subprocess. Every stdout line is kept raw (to prove it is JSON)
    and decoded; stderr is collected whole."""

    def __init__(self, *args: str, heartbeat_ms: int = 100):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "petritype.runner", *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env(heartbeat_ms),
        )
        self.lines: queue.Queue = queue.Queue()
        self.raw: list[bytes] = []
        self.stderr = bytearray()
        self.next_id = 0
        self._out = threading.Thread(target=self._read_stdout, daemon=True)
        self._err = threading.Thread(target=self._read_stderr, daemon=True)
        self._out.start()
        self._err.start()

    def _read_stdout(self):
        for raw in iter(self.process.stdout.readline, b""):
            self.raw.append(raw)
            try:
                self.lines.put(json.loads(raw))
            except ValueError:
                self.lines.put({"not_json": raw.decode("utf-8", "replace")})
        self.lines.put(None)

    def _read_stderr(self):
        for chunk in iter(lambda: self.process.stderr.read1(65536), b""):
            self.stderr += chunk

    def send_raw(self, data: bytes):
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def send(self, method: str, params: dict | None = None) -> str:
        self.next_id += 1
        request = {"id": str(self.next_id), "method": method}
        if params is not None:
            request["params"] = params
        self.send_raw((json.dumps(request) + "\n").encode())
        return str(self.next_id)

    def next_line(self):
        try:
            return self.lines.get(timeout=TIMEOUT)
        except queue.Empty:
            raise AssertionError(f"no line within {TIMEOUT}s; stderr:\n{self.stderr.decode()}")

    def until(self, predicate) -> list[dict]:
        """Lines up to and including the first that satisfies ``predicate``."""
        seen = []
        while True:
            line = self.next_line()
            assert line is not None, f"stdout closed before the expected line; saw {seen}"
            seen.append(line)
            if predicate(line):
                return seen

    def answer(self, request_id: str) -> list[dict]:
        """Every line up to and including the response to ``request_id``,
        heartbeats left out."""
        lines = self.until(lambda line: line.get("id") == request_id)
        return [line for line in lines if line.get("method") != "heartbeat"]

    def request(self, method: str, params: dict | None = None) -> list[dict]:
        return self.answer(self.send(method, params))

    def finish(self) -> int:
        """Wait for exit; the rest of stdout must be heartbeats at most."""
        code = self.process.wait(timeout=TIMEOUT)
        self._out.join(TIMEOUT)
        self._err.join(TIMEOUT)
        rest = []
        while True:
            line = self.lines.get_nowait()
            if line is None:
                break
            rest.append(line)
        assert all(line.get("method") == "heartbeat" for line in rest), rest
        return code

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()


@pytest.fixture
def spawn():
    started = []

    def make(*args, **kwargs):
        runner = Runner(*args, **kwargs)
        started.append(runner)
        return runner

    yield make
    for runner in started:
        runner.kill()


def kinds(lines):
    return [line.get("method") or ("result" if "result" in line else "error") for line in lines]


def test_a_session_answers_in_order_with_notifications_first(spawn):
    runner = spawn("--module", "runner_process_nets")

    describe = runner.request("describe")
    assert kinds(describe) == ["result"]
    assert [net["name"] for net in describe[0]["result"]["nets"]] == ["greeter", "watcher"]

    load = runner.request("load", {"net": "greeter", "params": {"n": 2}})
    assert kinds(load) == ["load_progress", "reloaded", "result"]
    assert load[-1]["result"]["status"] == "loaded"

    step = runner.request("step")
    assert kinds(step)[-1] == "result"
    # The firing in two pieces, nothing between them but heartbeats.
    pieces = [line for line in step[:-1] if line.get("method") != "heartbeat"]
    assert kinds(pieces)[:2] == ["launched", "distributed"]
    launched, distributed = pieces[0]["params"], pieces[1]["params"]
    assert launched["transition"] == distributed["transition"] == "Greet"
    assert launched["consumed"] == [["In", "1"]]
    assert distributed["marking_version"] == launched["marking_version"] + 1
    assert step[-1]["result"]["fired"] is True
    assert step[-1]["result"]["transition"] == "Greet"
    assert step[-1]["result"]["marking_version"] == distributed["marking_version"]

    state = runner.request("get_state")
    assert kinds(state) == ["result"]
    counts = {name: place["count"] for name, place in state[0]["result"]["marking"]["places"].items()}
    assert counts == {"In": 1, "Out": 1}

    # A net is loaded, so heartbeats come; nothing is running between requests.
    beat = runner.until(lambda line: line.get("method") == "heartbeat")[-1]
    assert beat["params"] == {
        "step_started_at": None, "executing_transition": None,
        "marking_version": state[0]["result"]["marking_version"], "running": False,
    }

    shutdown = runner.request("shutdown")
    assert shutdown[-1] == {"id": str(runner.next_id), "result": {"status": "ok"}}
    runner.process.stdin.close()
    assert runner.finish() == 0
    assert "hello from a body" in runner.stderr.decode()
    for raw in runner.raw:
        assert raw.endswith(b"\n")
        json.loads(raw)


def test_a_continuous_net_runs_idles_and_keeps_beating(spawn):
    runner = spawn("--module", "runner_process_nets")
    runner.request("load", {"net": "watcher"})
    start = runner.request("start")
    assert start[-1]["result"]["status"] == "started"
    runner.until(lambda line: line.get("method") == "idle")
    # Ticks keep coming while idle; heartbeats report a running net.
    beat = runner.until(lambda line: line.get("method") == "heartbeat")[-1]
    assert beat["params"]["running"] is True
    assert beat["params"]["executing_transition"] is None
    runner.request("stop")
    runner.request("shutdown")
    assert runner.finish() == 0


def test_end_of_file_without_shutdown_exits_cleanly(spawn):
    runner = spawn("--module", "runner_process_nets")
    runner.request("load", {"net": "greeter"})
    runner.process.stdin.close()
    assert runner.finish() == 0


def test_no_heartbeat_before_a_net_is_loaded(spawn):
    runner = spawn("--module", "runner_process_nets", heartbeat_ms=20)
    # Ticks run (period 20 ms) while nothing is loaded; then a response must be
    # the first line written.
    threading.Event().wait(0.3)
    assert kinds(runner.request("describe")) == ["result"]
    assert runner.raw[0].startswith(b'{"id"')
    runner.process.stdin.close()
    assert runner.finish() == 0


def test_heartbeats_name_the_body_and_shutdown_abandons_it(spawn):
    runner = spawn("--module", "runner_slow_nets")
    runner.request("load", {"net": "sleeper"})
    step_id = runner.send("step")  # the body sleeps for a minute
    beat = runner.until(
        lambda line: line.get("method") == "heartbeat" and line["params"]["executing_transition"]
    )[-1]
    assert beat["params"]["executing_transition"] == "Linger"
    assert beat["params"]["step_started_at"].endswith("Z")
    shutdown = runner.answer(runner.send("shutdown"))
    assert shutdown[-1]["result"] == {"status": "ok"}
    assert all(line.get("id") != step_id for line in shutdown)
    assert runner.finish() == 0  # well inside the body's minute


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGINT"])
def test_a_signal_exits_cleanly(spawn, sig):
    runner = spawn("--module", "runner_process_nets")
    runner.request("load", {"net": "greeter"})
    runner.process.send_signal(getattr(signal, sig))
    assert runner.finish() == 0


def test_a_malformed_line_gets_invalid_params_with_an_empty_id(spawn):
    runner = spawn()
    runner.send_raw(b"this is not json\n")
    line = runner.next_line()
    assert line["id"] == ""
    assert line["error"]["code"] == "invalid_params"
    runner.send_raw(b'{"id": "7", "params": {}}\n')
    line = runner.next_line()
    assert line["id"] == "7"
    assert line["error"]["code"] == "invalid_params"
    runner.send_raw(b"\n")  # blank lines are ignored
    assert kinds(runner.request("describe")) == ["result"]
    runner.process.stdin.close()
    assert runner.finish() == 0


def test_prints_at_import_go_to_stderr(spawn):
    runner = spawn("--module", "runner_noisy_import")
    assert [net["name"] for net in runner.request("describe")[0]["result"]["nets"]] == ["greeter", "noisy"]
    runner.process.stdin.close()
    assert runner.finish() == 0
    stderr = runner.stderr.decode()
    assert "print at import" in stderr
    assert "descriptor one at import" in stderr
    assert all(raw.startswith(b"{") for raw in runner.raw)


def test_the_version_0_load_works_without_any_module(spawn):
    runner = spawn()
    describe = runner.request("describe")
    assert describe[0]["result"]["nets"] == []
    load = runner.request("load", {"module": "runner_process_nets", "function": "greeter",
                                   "factory_params": {"n": 3}})
    assert kinds(load) == ["load_progress", "reloaded", "result"]
    state = runner.request("get_state")
    counts = {name: place["count"] for name, place in state[0]["result"]["marking"]["places"].items()}
    assert counts == {"In": 3, "Out": 0}
    runner.request("shutdown")
    assert runner.finish() == 0


def run_once(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "petritype.runner", *args],
        stdin=subprocess.DEVNULL, capture_output=True, env=child_env(), timeout=TIMEOUT,
    )


def test_describe_flag_prints_one_line_and_exits(spawn):
    done = run_once("--describe", "--module", "runner_noisy_import")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert len(lines) == 1
    describe = json.loads(lines[0])
    assert describe["protocol_version"] == 1
    assert describe["runtime"] == "python"
    assert [net["name"] for net in describe["nets"]] == ["greeter", "noisy"]
    assert b"print at import" in done.stderr


@pytest.mark.parametrize("args, message", [
    (["--module", "no_such_module_anywhere"], b"no_such_module_anywhere"),
    (["--module", "runner_broken_import"], b"this module refuses to import"),
    (["--describe", "--module", "no_such_module_anywhere"], b"no_such_module_anywhere"),
    (["--describe"], b"--describe needs at least one --module"),
    (["--frobnicate"], b"unknown argument"),
    (["--module"], b"--module needs a module name"),
])
def test_bad_arguments_and_failed_imports_exit_2(args, message):
    done = run_once(*args)
    assert done.returncode == 2
    assert done.stdout == b""
    assert message in done.stderr


# --- pure pieces ---------------------------------------------------------------------------


def test_arguments_parse():
    assert CommandLine.parse([]) == Arguments()
    assert CommandLine.parse(["--module", "a", "--module=b"]) == Arguments(modules=("a", "b"))
    assert CommandLine.parse(["--describe", "--module", "a"]) == Arguments(modules=("a",), describe=True)
    assert CommandLine.parse(["--module", "a", "--describe"]) == Arguments(modules=("a",), describe=True)
    assert CommandLine.parse(["--help"]).help
    for bad in (["--describe"], ["--module"], ["--module="], ["--module", "--describe"], ["-x"], ["pkg"]):
        with pytest.raises(UsageError):
            CommandLine.parse(bad)


def test_heartbeat_period_from_the_environment():
    assert CommandLine.heartbeat_period({}) == DEFAULT_HEARTBEAT_S == 30.0
    assert CommandLine.heartbeat_period({"PETRITYPE_RUNNER_HEARTBEAT_MS": "100"}) == 0.1
    for bad in ("0", "-5", "soon", ""):
        assert CommandLine.heartbeat_period({"PETRITYPE_RUNNER_HEARTBEAT_MS": bad}) == 30.0


def chunks(data: bytes, size: int):
    view = io.BytesIO(data)
    return lambda _wanted: view.read(size)


def test_reading_splits_lines_across_chunks():
    data = b'{"a": 1}\n\n' + b"x" * 1000 + b"\nlast"
    for size in (1, 3, 7, 4096):
        assert list(Reading.lines(chunks(data, size))) == [b'{"a": 1}', b"", b"x" * 1000, b"last"]
    assert list(Reading.lines(chunks(b"", 5))) == []


def test_reading_a_long_line():
    line = b'{"id": "1", "method": "describe", "params": {"pad": "' + b"y" * (17 << 20) + b'"}}'
    (read,) = Reading.lines(chunks(line + b"\n", 1 << 16))
    assert read == line
    assert isinstance(Reading.parse(read), Request)


def test_a_line_parses_as_a_request_a_malformed_or_nothing():
    assert Reading.parse(b'{"id": "1", "method": "describe"}\r') == Request("1", "describe", {})
    assert Reading.parse(b"   ") is None
    assert Reading.parse(b"") is None
    not_json = Reading.parse(b"{oops")
    assert isinstance(not_json, Malformed) and not_json.id == ""
    not_utf8 = Reading.parse(b"\xff\xfe")
    assert isinstance(not_utf8, Malformed) and not_utf8.id == ""
    no_method = Reading.parse(b'{"id": "9"}')
    assert no_method == Malformed("9", 'a request needs a string "method"')
    assert Reading.parse(b"[1, 2]") == Malformed("", "a request must be a JSON object")


@pytest.mark.asyncio
async def test_the_owner_answers_a_malformed_line_with_invalid_params():
    out = await Owner(Registry()).handle(Reading.parse(b"{oops"), datetime.now(timezone.utc))
    assert len(out.lines) == 1
    assert out.lines[0]["id"] == ""
    assert out.lines[0]["error"]["code"] == "invalid_params"


class Recording(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.writes = 0
        self.flushes = 0

    def write(self, data):
        self.writes += 1
        return super().write(data)

    def flush(self):
        self.flushes += 1


def test_a_batch_is_one_write_and_one_flush():
    sink = Recording()
    stream = ProtocolStream(sink)
    lines = [{"method": "distributed", "params": {"text": "a\nb", "name": "café"}}, {"id": "1", "result": {}}]
    assert stream.write(lines)
    assert (sink.writes, sink.flushes) == (1, 1)
    written = sink.getvalue()
    assert written.count(b"\n") == 2
    assert "café".encode() in written  # not escaped
    assert [json.loads(raw) for raw in written.splitlines()] == lines


def test_heartbeats_follow_the_gauges_written_with_the_last_batch():
    sink = Recording()
    stream = ProtocolStream(sink)
    stream.heartbeat()
    assert sink.getvalue() == b""  # nothing loaded: no heartbeat
    started = datetime(2026, 10, 3, 9, 15, 2, 413000, tzinfo=timezone.utc)
    stream.write([], Gauges(loaded=True, marking_version=4, running=True, executing=("Greet", started)))
    stream.heartbeat()
    stream.write([], Gauges(loaded=True, marking_version=5, running=False))
    stream.heartbeat()
    first, second = (json.loads(raw) for raw in sink.getvalue().splitlines())
    assert first == {"method": "heartbeat", "params": {
        "step_started_at": "2026-10-03T09:15:02.413Z", "executing_transition": "Greet",
        "marking_version": 4, "running": True,
    }}
    assert second == {"method": "heartbeat", "params": {
        "step_started_at": None, "executing_transition": None, "marking_version": 5, "running": False,
    }}


def test_a_broken_stream_reports_false():
    class Closed(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError

    stream = ProtocolStream(Closed())
    assert not stream.write([{"id": "1", "result": {}}])
    assert not stream.write([{"id": "2", "result": {}}])


def test_frames_encode_one_object_per_line():
    assert Frames.encode([{"a": "x\ny"}, {"b": 1}]) == b'{"a": "x\\ny"}\n{"b": 1}\n'
