"""The runner's process layer: the command line, the stdout guard, the threads
and the loop around ``Owner``. The only part of ``petritype.runner`` that does I/O.

It mirrors the Rust runner's ``io.rs`` (petritype-rs DESIGN §19.3.2 item 3)
and keeps no protocol logic of its own: every line it writes comes from the
owner, except ``heartbeat``, which it builds from the owner's read-only views.

Command line
------------
``python -m petritype.runner [--module M ...]`` serves the protocol on stdin
and stdout until ``shutdown`` or end of file and exits 0. Each ``--module`` is
imported and its ``@petri_net`` factories are what ``describe`` lists and
``load {net, params}`` builds; with none, only the version 0 ``load {module,
function, factory_params}`` form can load anything.
``python -m petritype.runner --describe --module M ...`` prints the
``describe`` answer as one JSON line and exits 0. A ``--describe`` with no
``--module``, an unknown argument, or a module that fails to import: a message
on stderr and exit 2. ``-h``/``--help`` prints the usage and exits 0.

The heartbeat (and the clock tick that keeps a ``"24/7"`` net checking) fires
every 30 seconds, or every ``PETRITYPE_RUNNER_HEARTBEAT_MS`` milliseconds when
that environment variable holds a positive integer (meant for tests).

Order at start
--------------
1. Parse the arguments (nothing imported from the user yet).
2. Take stdout for the protocol: flush ``sys.stdout``, duplicate file
   descriptor 1 (``os.dup``, so the copy is not inherited by child processes)
   and open the copy as the binary protocol stream; point descriptor 1 at
   stderr with ``os.dup2``, so C extensions and subprocesses that write to
   descriptor 1 land on stderr; and set ``sys.stdout = sys.stderr`` so a
   ``print`` does too.
3. Only then import the ``--module`` modules, so a ``print`` at import time
   already goes to stderr. ``--describe`` writes on the protocol stream too.
4. Serve.

While serving
-------------
- A **reader** thread reads stdin (descriptor 0, raw ``os.read``; lines of
  any length) and posts each parsed line to the owner's ``asyncio.Queue``
  with ``loop.call_soon_threadsafe``; end of file posts ``_EOF``. A line that
  is not a request becomes a ``Malformed`` that the owner answers at once with
  ``invalid_params``.
- The **owner loop** takes one item at a time (a request, a landed firing, a
  tick, end of file), calls ``handle``/``completed``/``tick`` with
  ``datetime.now(UTC)``, hands the batch to the writer, and runs a ``Launch``
  as a task that posts its ``Landing`` back to the queue.
- The **writer**, ``ProtocolStream``, owns the protocol stream. It writes one
  batch as one ``write`` of newline-terminated JSON and one flush, under a
  lock, so a heartbeat can never land inside a request's batch.
- The **heartbeat** thread wakes every period; while a net is loaded it
  writes one ``heartbeat``, and it always posts a tick. It never touches the
  owner: after each owner call, on the loop, the owner loop reads the owner's
  views into an immutable ``Gauges`` and hands it to the writer with the
  batch, under the same lock; the heartbeat reads the latest ``Gauges``
  there. So a heartbeat describes exactly the state the lines already written
  describe, and it still goes out while an ``async`` body blocks the loop,
  which a ``call_soon_threadsafe`` round trip would not.
- ``SIGTERM`` and ``SIGINT`` count as end of file.

Exit
----
On ``shutdown``, end of file or a broken protocol stream the loop stops the
heartbeat and flushes. A body still running (a synchronous body on a thread
of the executor) is abandoned with the process, as ``shutdown`` asks: the
process ends with ``os._exit(0)``, since a normal interpreter exit would wait
for that thread. Otherwise ``main`` returns 0.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO, Callable, Iterator, Optional, Union

from petritype.runner import protocol as wire
from petritype.runner.owner import Landing, Launch, Owner
from petritype.runner.protocol import Malformed, Request, Requests
from petritype.runner.registry import Registry


HEARTBEAT_ENV = "PETRITYPE_RUNNER_HEARTBEAT_MS"
DEFAULT_HEARTBEAT_S = 30.0

USAGE = (
    "usage: python -m petritype.runner [--module MODULE ...]\n"
    "           serve the net protocol on stdin and stdout\n"
    "       python -m petritype.runner --describe --module MODULE [--module MODULE ...]\n"
    "           print the nets the modules carry as one JSON line"
)

# How much the reader asks of stdin at a time.
READ_CHUNK = 1 << 16


# --- the command line ----------------------------------------------------------------------


class UsageError(Exception):
    """Arguments the runner does not accept; exit 2 with usage on stderr."""


@dataclass(frozen=True)
class Arguments:
    modules: tuple[str, ...] = ()
    describe: bool = False
    help: bool = False


class CommandLine:
    """Functions that read the command line."""

    def parse(argv: list[str]) -> Arguments:
        """``argv`` without the program name. ``--module NAME`` and
        ``--module=NAME`` repeat; ``--describe`` needs at least one."""
        modules: list[str] = []
        describe = False
        index = 0
        while index < len(argv):
            arg = argv[index]
            if arg in ("-h", "--help"):
                return Arguments(help=True)
            if arg == "--describe":
                describe = True
            elif arg == "--module":
                index += 1
                if index >= len(argv) or not argv[index] or argv[index].startswith("-"):
                    raise UsageError("--module needs a module name")
                modules.append(argv[index])
            elif arg.startswith("--module="):
                name = arg.removeprefix("--module=")
                if not name:
                    raise UsageError("--module needs a module name")
                modules.append(name)
            else:
                raise UsageError(f"unknown argument: {arg!r}")
            index += 1
        if describe and not modules:
            raise UsageError("--describe needs at least one --module")
        return Arguments(modules=tuple(modules), describe=describe)

    def heartbeat_period(environ: Union[dict, os._Environ]) -> float:
        """Seconds between heartbeats: ``PETRITYPE_RUNNER_HEARTBEAT_MS`` when it
        is a positive integer, else 30."""
        raw = environ.get(HEARTBEAT_ENV, "").strip()
        try:
            ms = int(raw)
        except ValueError:
            return DEFAULT_HEARTBEAT_S
        return ms / 1000.0 if ms > 0 else DEFAULT_HEARTBEAT_S


# --- reading -------------------------------------------------------------------------------


class Reading:
    """Functions that turn stdin bytes into requests."""

    def lines(read: Callable[[int], bytes]) -> Iterator[bytes]:
        """The lines (without ``\\n``) of whatever ``read`` returns until it
        returns ``b""``; a last line without a newline counts. Each byte is
        scanned once, so a very long line costs no more than its length."""
        pending = bytearray()
        scanned = 0
        while True:
            chunk = read(READ_CHUNK)
            if not chunk:
                if pending:
                    yield bytes(pending)
                return
            pending += chunk
            while (end := pending.find(b"\n", scanned)) >= 0:
                yield bytes(pending[:end])
                del pending[:end + 1]
                scanned = 0
            scanned = len(pending)

    def parse(raw: bytes) -> Optional[Union[Request, Malformed]]:
        """One line as a request, a ``Malformed`` for the owner to refuse, or
        ``None`` for a blank line (ignored)."""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return Malformed("", "a request line must be UTF-8")
        if not text.strip():
            return None
        try:
            value = json.loads(text)
        except ValueError as error:
            return Malformed("", f"a request line must be JSON: {error}")
        return Requests.parse(value)


# --- writing -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gauges:
    """What a heartbeat reports, read off the owner on the loop after each call."""

    loaded: bool = False
    marking_version: int = 0
    running: bool = False
    executing: Optional[tuple[str, datetime]] = None

    @classmethod
    def of(cls, owner: Owner) -> "Gauges":
        return cls(owner.loaded, owner.marking_version, owner.running, owner.status)

    def heartbeat(self) -> Optional[dict]:
        """The ``heartbeat`` line, or ``None`` when no net is loaded."""
        if not self.loaded:
            return None
        name, started = self.executing if self.executing is not None else (None, None)
        return wire.Heartbeat(
            step_started_at=Frames.iso(started) if started is not None else None,
            executing_transition=name,
            marking_version=self.marking_version,
            running=self.running,
        ).line()


class Frames:
    """Functions that encode lines."""

    def encode(lines: list[dict]) -> bytes:
        """One JSON object per line, UTF-8, each ending in ``\\n``. ``json.dumps``
        without ``indent`` escapes every newline inside a string, so none can
        appear inside an object."""
        return b"".join(
            (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8") for line in lines
        )

    def iso(moment: datetime) -> str:
        """ISO 8601 in UTC with milliseconds and ``Z``, as the owner writes times."""
        return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ProtocolStream:
    """The one writer of the protocol stream. ``write`` takes a whole batch;
    ``heartbeat`` writes one line from the latest ``Gauges``. Both hold the
    lock, so lines never interleave."""

    def __init__(self, stream: BinaryIO):
        self._stream = stream
        self._lock = threading.Lock()
        self._gauges = Gauges()
        self.broken = False

    def write(self, lines: list[dict], gauges: Optional[Gauges] = None) -> bool:
        """Write ``lines`` with one write and one flush, then record ``gauges``.
        False once the stream is broken (the host has gone)."""
        data = Frames.encode(lines) if lines else b""
        with self._lock:
            if gauges is not None:
                self._gauges = gauges
            return self._put(data)

    def heartbeat(self) -> bool:
        with self._lock:
            line = self._gauges.heartbeat()
            return self._put(Frames.encode([line])) if line is not None else not self.broken

    def flush(self) -> None:
        with self._lock:
            self._put(b"")

    def _put(self, data: bytes) -> bool:
        if self.broken:
            return False
        try:
            if data:
                self._stream.write(data)
            self._stream.flush()
        except (OSError, ValueError):  # a closed pipe, or a closed file
            self.broken = True
        return not self.broken


# --- the process ---------------------------------------------------------------------------


# Queue items besides requests and landings.
_EOF = object()
_TICK = object()


class StdoutGuard:
    """Functions that take stdout for the protocol."""

    def take() -> BinaryIO:
        """The real stdout as a binary stream for the protocol; afterwards
        descriptor 1 and ``sys.stdout`` are stderr. Where stdout has no
        descriptor (an embedding host), only ``sys.stdout`` is swapped."""
        try:
            sys.stdout.flush()
        except (OSError, ValueError, AttributeError):
            pass
        try:
            stdout_fd = sys.stdout.fileno()
            stderr_fd = sys.stderr.fileno()
        except (OSError, ValueError, AttributeError):
            stream = sys.stdout.buffer
        else:
            protocol_fd = os.dup(stdout_fd)  # not inheritable: children never see it
            os.dup2(stderr_fd, 1)
            stream = os.fdopen(protocol_fd, "wb")
        sys.stdout = sys.stderr
        return stream


class Process:
    """Functions that run the process."""

    def import_modules(names: tuple[str, ...]) -> Registry:
        modules = [importlib.import_module(name) for name in names]
        return Registry.from_modules(modules)

    async def serve(registry: Registry, stream: ProtocolStream, stdin_fd: int, period: float) -> bool:
        """Serve until ``shutdown``, end of file, a signal or a broken stream.
        True when a body was still airborne at the end."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        owner = Owner(registry)
        stopping = threading.Event()
        bodies: set[asyncio.Task] = set()

        def post(item: Any) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:  # the loop has closed: the process is ending
                pass

        def read() -> None:
            try:
                for raw in Reading.lines(lambda size: os.read(stdin_fd, size)):
                    item = Reading.parse(raw)
                    if item is not None:
                        post(item)
            except OSError as error:
                print(f"petritype.runner: reading stdin: {error}", file=sys.stderr)
            post(_EOF)

        def beat() -> None:
            while not stopping.wait(period):
                stream.heartbeat()
                post(_TICK)

        async def fly(launch: Launch) -> None:
            landing = await launch.coroutine
            queue.put_nowait(landing)

        threading.Thread(target=read, name="petritype-runner-reader", daemon=True).start()
        threading.Thread(target=beat, name="petritype-runner-heartbeat", daemon=True).start()
        handled = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, queue.put_nowait, _EOF)
                handled.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):  # not on this platform
                pass
        try:
            while True:
                item = await queue.get()
                if item is _EOF:
                    break
                now = datetime.now(UTC)
                if item is _TICK:
                    out = await owner.tick(now)
                elif isinstance(item, Landing):
                    out = await owner.completed(item, now)
                else:
                    out = await owner.handle(item, now)
                if not stream.write(out.lines, Gauges.of(owner)):
                    break
                if out.launch is not None:
                    task = loop.create_task(fly(out.launch))
                    bodies.add(task)
                    task.add_done_callback(bodies.discard)
                if out.exit:
                    break
        finally:
            stopping.set()
            for sig in handled:
                loop.remove_signal_handler(sig)
            stream.flush()
        return owner.airborne


    async def run(registry: Registry, stream: ProtocolStream, period: float) -> None:
        """``serve`` on stdin, then abandon a body still running: leaving
        ``asyncio.run`` normally would wait for its executor thread."""
        if await Process.serve(registry, stream, 0, period):
            sys.stderr.flush()
            os._exit(0)


def main(argv: Optional[list[str]] = None) -> int:
    """The whole of ``python -m petritype.runner`` (see the module docstring)."""
    try:
        arguments = CommandLine.parse(sys.argv[1:] if argv is None else argv)
    except UsageError as error:
        print(f"petritype.runner: {error}\n{USAGE}", file=sys.stderr)
        return 2
    if arguments.help:
        print(USAGE)
        return 0
    stream = ProtocolStream(StdoutGuard.take())
    try:
        registry = Process.import_modules(arguments.modules)
    except Exception as error:
        traceback.print_exc()
        print(f"petritype.runner: cannot import the --module modules: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 2
    if arguments.describe:
        return 0 if stream.write([registry.describe().wire()]) else 1
    period = CommandLine.heartbeat_period(os.environ)
    asyncio.run(Process.run(registry, stream, period))
    return 0
