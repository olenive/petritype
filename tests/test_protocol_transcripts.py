"""The shared protocol transcripts in ``tests/protocol/*.json`` (PROTOCOL.md §11),
replayed through ``Owner`` with no process, and those in ``OVER_PIPES`` over pipes
against ``python -m petritype.runner`` too.

The transcript format and the matching rules are the Rust harness's
(``petritype-rs/petritype-runner/tests/transcripts.rs``), so a transcript means
the same to both:

- ``{"net", "params", "exchanges": [{"send", "expect"}]}``. ``send`` is a
  request without its ``id``: the harness numbers them ``"1"``, ``"2"``, ... in
  order, and a missing ``params`` is ``{}``. ``expect`` is every line the request
  causes, in order: its notifications, then its response, and, for a ``start``,
  the firings that follow it. A launched body is awaited at once and its landing
  fed back, so the lines of a whole firing belong to the exchange that caused it.
- Masks in any value position: ``"<label>"``, ``"<time>"`` and ``"<id>"`` match
  any string; ``"<ms>"`` and ``"<version>"`` any number (a boolean is not a
  number). Numbers compare by value (``0`` matches ``0.0``); objects must have
  exactly the expected keys; arrays must have the expected length and match
  element by element; anything else compares equal.
- A ``launched`` and the next ``distributed`` of the same transition are folded
  into one ``fired {transition, consumed, produced, duration_ms}`` stamped with
  the ``distributed``'s ``marking_version`` and ``running``, standing where the
  ``distributed`` stood, before comparing. Both runners split every firing (the
  Python one since 2026-10-04); the fold is what lets a transcript read the same
  for any runtime, one-piece ones included. Only the firing's own lines may stand
  between the two pieces (``BETWEEN_PIECES``), and they keep their places before
  the ``fired`` (PROTOCOL.md §11, 2026-10-12, as the Rust harness folds). A
  ``launched`` whose firing failed is left as it is, before its ``failed``. The
  expected lines are folded as well as the actual ones, so a transcript may hold
  either shape.

Three additions over the Rust harness, each needed for a transcript that holds
against both runtimes:

- ``"<any>"`` matches any value, objects and arrays included: ``detail`` of a
  transition is runtime-specific introspection (§5.1).
- ``{"<unordered>": [...]}`` matches an array holding the same elements in any
  order (each expected element taken by a distinct actual one): neither runtime
  promises an order for ``topology.edges`` and they list them differently.
- ``"<id:NAME>"`` matches any string and binds it to ``NAME`` for the rest of the
  transcript; a later ``"<id:NAME>"`` in an ``expect`` must be the same string,
  and one in a ``send`` is replaced by it, also inside a longer string
  (``"transit/Depot/#<id:bus>/cabin"``), which is how a ``send`` spells a path
  (§12.1). Token ids are the runtime's (§2), so a request about a token
  (``get_token``) must name the id the runner handed out.

With nested nets (§12) two more: ``"<path>"`` matches any string, for a path
that embeds a token id; and a ``nested`` wrapping a ``launched`` and the next
``nested`` wrapping its ``distributed`` (same path and nested id) are folded
into a ``nested`` wrapping a ``fired``, as the outer pair is, over the same
kinds of line wrapped for that net or for the nets inside it.

A transcript is replayed against a registry reduced to the net it names
(§11), so each may pin ``describe`` in full.

A mismatch is reported with the file, the exchange index and method, the line
index, and both lines pretty-printed; a line count mismatch lists every actual
line.
"""

import copy
import importlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from petritype.runner.owner import Owner
from petritype.runner.registry import Registry

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS = sorted((ROOT / "tests" / "protocol").glob("*.json"))
# The module carrying each conformance net; a transcript replays against its own net alone.
NET_MODULES = {
    "parcel-distribution": "tests.protocol_nets.parcel_distribution",
    "transit": "tests.protocol_nets.transit",
    "wildlife": "tests.protocol_nets.wildlife",
    "shop": "tests.protocol_nets.shop",
    "traffic-light": "tests.protocol_nets.traffic_light",
}
# The process-mode transcripts: a running net, whose lines arrive after the response;
# the nested-net one, whose example module and templates must import in the process; and
# the two over the edges that accept an empty place, whose example modules must too.
OVER_PIPES = ("04_start_run.json", "05_nested_nets.json", "07_empty_place_edges.json", "08_traffic_light.json")

START = datetime(2026, 10, 3, 9, 15, 2, 413000, tzinfo=timezone.utc)
STEP = timedelta(seconds=1)  # ``now`` moves on by this much per exchange
PROCESS_TIMEOUT = 60.0

STRING_MASKS = frozenset({"<label>", "<time>", "<id>", "<path>"})
NUMBER_MASKS = frozenset({"<ms>", "<version>"})
ANY = "<any>"
UNORDERED = "<unordered>"
CAPTURE_PREFIX, CAPTURE_SUFFIX = "<id:", ">"


# --- matching ------------------------------------------------------------------------------


def _capture_name(value) -> str | None:
    if isinstance(value, str) and value.startswith(CAPTURE_PREFIX) and value.endswith(CAPTURE_SUFFIX):
        name = value[len(CAPTURE_PREFIX):-len(CAPTURE_SUFFIX)]
        return name or None
    return None


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def matches(expected, actual, bindings: dict) -> bool:
    """Whether ``actual`` matches ``expected`` with its masks; binds captures into
    ``bindings`` (left as they were when the match fails)."""
    trial = dict(bindings)
    if _match(expected, actual, trial):
        bindings.update(trial)
        return True
    return False


def _match(expected, actual, bindings: dict) -> bool:
    if isinstance(expected, str):
        if expected == ANY:
            return True
        if expected in STRING_MASKS:
            return isinstance(actual, str)
        if expected in NUMBER_MASKS:
            return _is_number(actual)
        name = _capture_name(expected)
        if name is not None:
            if not isinstance(actual, str):
                return False
            bound = bindings.setdefault(name, actual)
            return bound == actual
        return expected == actual
    if isinstance(expected, bool) or expected is None:
        return actual is expected
    if _is_number(expected):
        return _is_number(actual) and float(expected) == float(actual)
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(_match(e, a, bindings) for e, a in zip(expected, actual))
        )
    if isinstance(expected, dict):
        if set(expected) == {UNORDERED}:
            return _match_unordered(expected[UNORDERED], actual, bindings)
        return (
            isinstance(actual, dict)
            and expected.keys() == actual.keys()
            and all(_match(expected[key], actual[key], bindings) for key in expected)
        )
    return expected == actual


def _match_unordered(expected: list, actual, bindings: dict) -> bool:
    """Same elements in any order: a pairing of every expected element with a
    distinct actual one. Backtracks, because a masked element (``"<id>"``) can
    greedily take the actual element a later literal needed; PROTOCOL.md §11
    asks only that some pairing exist, as the Rust harness implements it."""
    if not isinstance(actual, list) or len(expected) != len(actual):
        return False

    def pair(remaining: list, unused: list[int], trial: dict) -> dict | None:
        if not remaining:
            return trial
        head, *rest = remaining
        for index in unused:
            attempt = dict(trial)
            if _match(head, actual[index], attempt):
                found = pair(rest, [i for i in unused if i != index], attempt)
                if found is not None:
                    return found
        return None

    found = pair(expected, list(range(len(actual))), dict(bindings))
    if found is None:
        return False
    bindings.update(found)
    return True


def substitute(value, bindings: dict, where: str):
    """``value`` with every ``"<id:NAME>"`` replaced by its binding, whole or
    inside a longer string."""
    name = _capture_name(value)
    if name is not None:
        assert name in bindings, f"{where}: {value} is sent before any line bound it"
        return bindings[name]
    if isinstance(value, str) and CAPTURE_PREFIX in value:
        def bound(match):
            assert match.group(1) in bindings, f"{where}: {match.group(0)} is sent before any line bound it"
            return bindings[match.group(1)]
        return re.sub(r"<id:([^>]+)>", bound, value)
    if isinstance(value, list):
        return [substitute(item, bindings, where) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, bindings, where) for key, item in value.items()}
    return value


def _fired(launched: dict, distributed: dict) -> dict:
    return {"method": "fired", "params": {
        "transition": distributed.get("transition"),
        "consumed": launched.get("consumed"),
        "produced": distributed.get("produced"),
        "duration_ms": distributed.get("duration_ms"),
        "marking_version": distributed.get("marking_version"),
        "running": distributed.get("running"),
    }}


def _same_inner_net(a: dict, b: dict) -> bool:
    return a["params"].get("path") == b["params"].get("path") and a["params"].get("nested_id") == b["params"].get("nested_id")


# The lines a firing's own body causes, which may stand between its ``launched``
# and its ``distributed`` (PROTOCOL.md §11): what it did inside the tokens it holds
# (``nested``), its own mutations, and, under a connection's filter, the ``hidden``,
# ``added`` or ``removed`` that stand in for a mutation (§13.4). The Rust harness's
# ``BETWEEN_PIECES``. For an inner net's pair the same lines, wrapped for that net,
# and anything wrapped for another net (the nets inside it).
BETWEEN_PIECES = frozenset({"nested", "mutated", "hidden", "added", "removed"})


def _event(line: dict) -> dict:
    return line.get("params", {}).get("event", {}) if line.get("method") == "nested" else {}


def _between(line: dict, nested_id=None) -> bool:
    """Whether ``line`` may stand between the two pieces of a firing of the outer
    net (``nested_id`` None) or of the inner net with that nested id."""
    if nested_id is None:
        return line.get("method") in BETWEEN_PIECES
    if line.get("method") != "nested":
        return False
    if line["params"].get("nested_id") != nested_id:
        return True
    return _event(line).get("method") in BETWEEN_PIECES - {"nested"}


def _launched_before(folded: list[dict], nested_id=None) -> int:
    """The index in ``folded`` just past the firing's own lines at its end (those
    that may stand between the pieces): where its ``launched`` must stand, less one."""
    at = len(folded)
    while at > 0 and _between(folded[at - 1], nested_id):
        at -= 1
    return at


def fold(lines: list[dict]) -> list[dict]:
    """A ``launched`` and the next ``distributed`` of the same transition as one
    ``fired`` where the ``distributed`` stood, the firing's own lines between them
    keeping their places before it; the same inside ``nested`` wrappers for one
    inner net (§11, §12.4). Idempotent, so it applies to expected lines too."""
    folded: list[dict] = []
    for line in lines:
        if line.get("method") == "distributed":
            at = _launched_before(folded)
            if (
                at > 0
                and folded[at - 1].get("method") == "launched"
                and folded[at - 1]["params"].get("transition") == line["params"].get("transition")
            ):
                launched = folded.pop(at - 1)
                folded.append(_fired(launched["params"], line["params"]))
                continue
        if line.get("method") == "nested" and _event(line).get("method") == "distributed":
            at = _launched_before(folded, line["params"].get("nested_id"))
            previous = folded[at - 1] if at > 0 else None
            if (
                previous is not None
                and previous.get("method") == "nested"
                and _same_inner_net(previous, line)
                and _event(previous).get("method") == "launched"
                and _event(previous)["params"].get("transition") == _event(line)["params"].get("transition")
            ):
                launched = folded.pop(at - 1)
                folded.append({"method": "nested", "params": {
                    **line["params"], "event": _fired(_event(launched)["params"], _event(line)["params"]),
                }})
                continue
        folded.append(line)
    return folded


def _waiting(folded: list[dict]) -> bool:
    """Whether a firing told in two pieces still waits for its second piece."""
    waiting: set = set()
    for line in folded:
        event, net = (_event(line), line["params"].get("nested_id")) if line.get("method") == "nested" else (line, None)
        if event.get("method") == "launched":
            waiting.add(net)
        elif event.get("method") in ("distributed", "failed", "fired"):
            waiting.discard(net)
    return bool(waiting)


def _pretty(line) -> str:
    return json.dumps(line, indent=2, sort_keys=False)


def check_exchange(name: str, index: int, method: str, expected: list, actual: list, bindings: dict) -> None:
    where = f"{name}, exchange {index} ({method})"
    for line, (e, a) in enumerate(zip(expected, actual)):
        if not matches(e, a, bindings):
            pytest.fail(f"{where}, line {line}:\nexpected:\n{_pretty(e)}\nactual:\n{_pretty(a)}", pytrace=False)
    if len(expected) != len(actual):
        shown = "\n".join(json.dumps(line) for line in actual)
        pytest.fail(f"{where}: expected {len(expected)} lines, got {len(actual)}:\n{shown}", pytrace=False)


# --- the transcripts -----------------------------------------------------------------------


def load_transcript(path: Path) -> dict:
    try:
        transcript = json.loads(path.read_text())
    except ValueError as error:
        raise AssertionError(f"{path.name}: not a transcript: {error}") from error
    net = transcript["net"]
    assert any(
        exchange["send"]["method"] == "load" and exchange["send"].get("params", {}).get("net") == net
        for exchange in transcript["exchanges"]
    ), f"{path.name}: never loads its own net {net!r}"
    return transcript


def request(exchange: dict, index: int, bindings: dict, where: str) -> dict:
    send = exchange["send"]
    return {
        "id": str(index + 1),
        "method": send["method"],
        "params": substitute(send.get("params", {}), bindings, where),
    }


def registry(net: str) -> Registry:
    """A registry carrying the transcript's net alone."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    assert net in NET_MODULES, f"no conformance module carries {net!r}"
    return Registry.from_modules([importlib.import_module(NET_MODULES[net])])


async def run(owner: Owner, line: dict, now: datetime) -> list[dict]:
    """Every line one request causes, each launch awaited inline and landed,
    until nothing is airborne."""
    out = await owner.handle(line, now)
    lines = list(out.lines)
    while out.launch is not None:
        landing = await out.launch.coroutine
        out = await owner.completed(landing, now)
        lines.extend(out.lines)
    return lines


def test_the_transcripts_are_there():
    assert len(TRANSCRIPTS) >= 4, f"the transcripts are missing from {ROOT / 'tests' / 'protocol'}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", TRANSCRIPTS, ids=[path.stem for path in TRANSCRIPTS])
async def test_transcript_holds(path: Path):
    transcript = load_transcript(path)
    owner = Owner(registry(transcript["net"]))
    bindings: dict = {}
    for index, exchange in enumerate(transcript["exchanges"]):
        where = f"{path.name}, exchange {index}"
        line = request(exchange, index, bindings, where)
        actual = fold(await run(owner, line, START + index * STEP))
        check_exchange(path.name, index, line["method"], fold(exchange["expect"]), actual, bindings)


# --- shared-safe: what a transcript over both runtimes may pin -----------------------------

# Keys whose value is the runtime's own and so must be masked in a shared transcript.
MASKED_KEYS = {
    "path": {"<path>"},
    "marking_version": NUMBER_MASKS,
    "duration_ms": NUMBER_MASKS,
    "type_name": STRING_MASKS,
    "label": STRING_MASKS,
    "message": STRING_MASKS,
    "runtime": STRING_MASKS,
    "runtime_version": STRING_MASKS,
    "started_at": STRING_MASKS,
    "idle_since": STRING_MASKS,
    "detail": {ANY},
}


# A colour both runtimes give alike (§11): Rust writes ``#rrggbb`` in lowercase.
HEX_COLOUR = re.compile(r"#[0-9a-f]{6}")


def _hex(colour) -> bool:
    return isinstance(colour, str) and HEX_COLOUR.fullmatch(colour) is not None


def _unsafe(value, path: str, top: bool = False) -> list[str]:
    problems = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}"
            if key == "value":
                continue  # a token's own JSON (get_token): the net's data, the same in both
            if key == "controls":
                continue  # what the net declares (§5.1), labels included: the same in both
            if key == "color":
                if not _hex(item):
                    problems.append(f"{here}: a shared transcript carries a colour only as #rrggbb")
            elif key in MASKED_KEYS and item not in MASKED_KEYS[key]:
                problems.append(f"{here}: {item!r} must be one of {sorted(MASKED_KEYS[key])}")
            elif key in ("id", "nested_id") and not top and not (item == "<id>" or _capture_name(item)):
                problems.append(f"{here}: {key} {item!r} must be masked")
            elif key in ("consumed", "produced") and isinstance(item, list):
                for n, pair in enumerate(item):
                    if not (isinstance(pair, list) and len(pair) >= 2
                            and (pair[1] == "<id>" or _capture_name(pair[1]))):
                        problems.append(f"{here}[{n}]: token id must be masked")
                    elif len(pair) > 2 and pair[2] not in STRING_MASKS:
                        problems.append(f"{here}[{n}]: label must be masked")
                    elif len(pair) >= 4 and not (pair[3] is None and len(pair) == 5) and not _hex(pair[3]):
                        problems.append(f"{here}[{n}]: a shared transcript carries a colour only as #rrggbb")
                    elif len(pair) > 5:
                        problems.append(f"{here}[{n}]: a produced entry has at most five elements")
            else:
                problems.extend(_unsafe(item, here))
    elif isinstance(value, list):
        for n, item in enumerate(value):
            problems.extend(_unsafe(item, f"{path}[{n}]"))
    return problems


@pytest.mark.parametrize("path", TRANSCRIPTS, ids=[path.stem for path in TRANSCRIPTS])
def test_transcript_is_shared_safe(path: Path):
    """Every token id, version, label, duration, time, type name and ``detail``
    masked, and a ``color`` only as a hex ``#rrggbb`` (§11), so the Rust runner can
    replay it too."""
    transcript = load_transcript(path)
    problems = []
    for index, exchange in enumerate(transcript["exchanges"]):
        for n, line in enumerate(exchange["expect"]):
            problems.extend(_unsafe(line, f"exchange {index} line {n}", top=True))
    assert not problems, f"{path.name}:\n" + "\n".join(problems)


# --- the harness itself --------------------------------------------------------------------


def test_the_masks():
    b: dict = {}
    assert matches({"a": "<time>", "b": "<ms>"}, {"a": "x", "b": 1.5}, b)
    assert not matches({"a": "<ms>"}, {"a": "1"}, b)
    assert not matches({"a": "<version>"}, {"a": True}, b), "a boolean is not a number"
    assert not matches({"a": 1}, {"a": 1, "b": 2}, b), "exact keys"
    assert matches(0, 0.0, b)
    assert not matches(1, True, b) and not matches(True, 1, b)
    assert matches({"d": "<any>"}, {"d": {"x": [1, 2]}}, b)
    assert matches({UNORDERED: [1, 2, 2]}, [2, 1, 2], b)
    assert not matches({UNORDERED: [1, 2, 2]}, [2, 1, 1], b)
    assert not matches({UNORDERED: [1]}, [1, 1], b)


def test_captures():
    b: dict = {}
    assert matches(["<id:x>", "<id:x>"], ["7", "7"], b) and b == {"x": "7"}
    assert not matches("<id:x>", "8", b), "bound already"
    assert not matches(["<id:y>", "<id:y>"], ["1", "2"], b) and "y" not in b, "a failed match binds nothing"
    assert substitute({"id": "<id:x>", "n": ["<id:x>"]}, b, "here") == {"id": "7", "n": ["7"]}


def test_the_nested_fold():
    wrap = lambda event, path="n/P/#1/c", nid="3": {"method": "nested", "params": {
        "path": path, "nested_id": nid, "event": event, "marking_version": 9, "running": False}}
    launched = {"method": "launched", "params": {"transition": "T", "consumed": [["A", "1"]], "marking_version": 1, "running": False}}
    distributed = {"method": "distributed", "params": {"transition": "T", "produced": [], "duration_ms": 2.0, "marking_version": 2, "running": False}}
    folded = fold([wrap(launched), wrap(distributed)])
    assert folded == [wrap({"method": "fired", "params": {
        "transition": "T", "consumed": [["A", "1"]], "produced": [], "duration_ms": 2.0, "marking_version": 2, "running": False,
    }})]
    assert len(fold([wrap(launched), wrap(distributed, nid="4")])) == 2, "another inner net's distributed"


def test_the_fold_keeps_the_firings_own_lines_in_their_places():
    """The fold of PROTOCOL.md §11 (2026-10-12), as the Rust harness folds: the
    lines a firing's body causes may stand between its two pieces and keep their
    places; the ``fired`` stands where the ``distributed`` stood."""
    stamp = lambda version: {"marking_version": version, "running": False}
    launched = {"method": "launched", "params": {"transition": "T", "consumed": [["A", "1"]], **stamp(1)}}
    inner = {"method": "nested", "params": {"path": "n/P/#1/c", "nested_id": "3", "event": {
        "method": "added", "params": {"place": "Door", "id": "1", "label": "x", **stamp(1)}}, **stamp(1)}}
    mutated = {"method": "mutated", "params": {"place": "P", "id": "1", **stamp(2)}}
    hidden = {"method": "hidden", "params": {"kind": "mutated", "place": "P", **stamp(3)}}
    added = {"method": "added", "params": {"place": "P", "id": "2", "label": "y", **stamp(4)}}
    distributed = {"method": "distributed", "params": {"transition": "T", "produced": [], "duration_ms": 2.0, **stamp(5)}}
    fireable = {"method": "fireable", "params": {"transitions": [], **stamp(5)}}
    fired = {"method": "fired", "params": {
        "transition": "T", "consumed": [["A", "1"]], "produced": [], "duration_ms": 2.0, **stamp(5)}}
    folded = fold([launched, inner, mutated, hidden, added, distributed, fireable])
    assert folded == [inner, mutated, hidden, added, fired, fireable]
    assert fold(folded) == folded, "idempotent: an expected transcript may hold either shape"
    assert fold([launched, fireable, distributed]) == [launched, fireable, distributed], "not a line of the firing's"
    failed = {"method": "failed", "params": {"transition": "T", "error": "boom", "restored": True,
                                             "consumed": [["A", "1"]], **stamp(3)}}
    assert fold([launched, mutated, failed]) == [launched, mutated, failed], "a failed firing stays in two pieces"


def test_the_nested_fold_keeps_the_firings_own_lines_in_their_places():
    wrap = lambda event, nid="3": {"method": "nested", "params": {
        "path": f"n/P/#1/c{nid}", "nested_id": nid, "event": event, "marking_version": 9, "running": False}}
    launched = {"method": "launched", "params": {"transition": "T", "consumed": [["A", "1"]], "marking_version": 1, "running": False}}
    mutated = {"method": "mutated", "params": {"place": "B", "id": "4", "marking_version": 2, "running": False}}
    deeper = {"method": "added", "params": {"place": "C", "id": "1", "label": "z", "marking_version": 1, "running": False}}
    distributed = {"method": "distributed", "params": {"transition": "T", "produced": [], "duration_ms": 2.0, "marking_version": 3, "running": False}}
    fireable = {"method": "fireable", "params": {"transitions": [], "marking_version": 3, "running": False}}
    folded = fold([wrap(launched), wrap(mutated), wrap(deeper, nid="5"), wrap(distributed)])
    assert folded == [wrap(mutated), wrap(deeper, nid="5"), wrap({"method": "fired", "params": {
        "transition": "T", "consumed": [["A", "1"]], "produced": [], "duration_ms": 2.0, "marking_version": 3, "running": False,
    }})]
    assert len(fold([wrap(launched), wrap(fireable), wrap(distributed)])) == 3, "the net's own fireable is no line of the firing's"


def test_embedded_captures():
    b = {"bus": "7"}
    assert substitute({"path": "transit/Depot/#<id:bus>/cabin"}, b, "here") == {"path": "transit/Depot/#7/cabin"}


def test_a_shared_colour_is_hex_and_controls_are_pinned():
    """A colour is shared only as ``#rrggbb`` in lowercase, which both runtimes give
    alike (§11, since 2026-10-16): on a token, an ``added``, and as the fourth element
    of a produced entry (``null`` there only before keys). ``topology.controls`` is
    the net's declaration and is pinned as it stands, labels included."""
    assert not _unsafe({"color": "#ff6347"}, "x", top=True)
    assert _unsafe({"color": "red"}, "x", top=True) and _unsafe({"color": "#FF6347"}, "x", top=True)

    def produced(*rest):
        return {"produced": [["Light", "<id:light>", "<label>", *rest]]}

    assert not _unsafe(produced("#32cd32"), "x") and not _unsafe(produced(None, {"k": 1}), "x")
    assert _unsafe(produced("green"), "x") and _unsafe(produced(None), "x")
    assert not _unsafe({"topology": {"controls": [{"name": "car", "label": "+ Car", "kind": "text"}]}}, "x")


def test_the_recorder_keeps_a_hex_colour_and_the_controls():
    from tests.record_transcript import Masker

    masker = Masker()
    line = masker.line({"method": "distributed", "params": {
        "transition": "Change", "produced": [["Light", "2", "'green'", "#32cd32"]], "duration_ms": 0.1,
        "marking_version": 2, "running": False,
    }})
    assert line["params"]["produced"] == [["Light", "<id:green>", "<label>", "#32cd32"]]
    controls = [{"name": "car", "label": "+ Car", "kind": "text", "node": "Waiting"}]
    state = masker.line({"id": "3", "result": {"topology": {"controls": controls}}})
    assert state["result"]["topology"]["controls"] == controls
    with pytest.raises(ValueError, match="#rrggbb"):
        masker.line({"method": "added", "params": {"place": "P", "id": "9", "label": "x", "color": "red"}})


def test_the_fold():
    folded = fold([
        {"method": "launched", "params": {"transition": "T", "consumed": [["A", "1"]],
                                          "marking_version": 1, "running": False}},
        {"method": "distributed", "params": {"transition": "T", "produced": [], "duration_ms": 2.0,
                                             "marking_version": 2, "running": False}},
        {"method": "launched", "params": {"transition": "T", "consumed": [], "marking_version": 3,
                                          "running": False}},
    ])
    assert folded[0] == {"method": "fired", "params": {
        "transition": "T", "consumed": [["A", "1"]], "produced": [], "duration_ms": 2.0,
        "marking_version": 2, "running": False,
    }}
    assert len(folded) == 2 and folded[1]["method"] == "launched", "no distributed follows it"


# --- over pipes ----------------------------------------------------------------------------


class Process:
    """``python -m petritype.runner`` on the conformance nets, with the heartbeat
    pushed out of the way (heartbeat lines are skipped anyway)."""

    def __init__(self, net: str):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH")]))
        env["PETRITYPE_RUNNER_HEARTBEAT_MS"] = "3600000"
        env["PYTHONUNBUFFERED"] = "1"
        modules = ["--module", NET_MODULES[net]]
        self.process = subprocess.Popen(
            [sys.executable, "-m", "petritype.runner", *modules],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env=env,
        )
        self.lines: queue.Queue = queue.Queue()
        self.stderr = bytearray()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for raw in iter(self.process.stdout.readline, b""):
            self.lines.put(json.loads(raw))
        self.lines.put(None)

    def _read_stderr(self):
        for chunk in iter(lambda: self.process.stderr.read1(65536), b""):
            self.stderr += chunk

    def send(self, line: dict) -> None:
        self.process.stdin.write((json.dumps(line) + "\n").encode())
        self.process.stdin.flush()

    def next_line(self):
        """The next line that is not a ``heartbeat``; ``None`` at end of output."""
        while True:
            try:
                line = self.lines.get(timeout=PROCESS_TIMEOUT)
            except queue.Empty:
                raise AssertionError(f"no line within {PROCESS_TIMEOUT}s; stderr:\n{self.stderr.decode()}")
            if line is None or line.get("method") != "heartbeat":
                return line

    def read(self, count: int) -> list[dict]:
        """Lines until ``count`` remain after folding (and no ``launched`` waits
        for its ``distributed``)."""
        lines: list[dict] = []
        while len(fold(lines)) < count or _waiting(fold(lines)):
            line = self.next_line()
            assert line is not None, f"stdout closed early; saw {lines}; stderr:\n{self.stderr.decode()}"
            lines.append(line)
        return fold(lines)

    def close(self) -> list[dict]:
        """Close stdin and return whatever else the process wrote."""
        self.process.stdin.close()
        rest = []
        while (line := self.next_line()) is not None:
            rest.append(line)
        self.process.wait(timeout=PROCESS_TIMEOUT)
        return rest


@pytest.mark.parametrize("name", OVER_PIPES)
def test_a_transcript_over_pipes(name: str):
    """The owner-level replay and the real process agree, on a transcript whose
    firings arrive after the response that set them off, on the nested one, and on
    the two over the edges that accept an empty place."""
    path = ROOT / "tests" / "protocol" / name
    transcript = load_transcript(path)
    runner = Process(transcript["net"])
    try:
        bindings: dict = {}
        for index, exchange in enumerate(transcript["exchanges"]):
            line = request(exchange, index, bindings, f"{path.name}, exchange {index}")
            runner.send(line)
            expected = fold(exchange["expect"])
            actual = runner.read(len(expected))
            check_exchange(f"{path.name} over pipes", index, line["method"], expected, actual, bindings)
        assert runner.close() == [], "lines after the last exchange"
        assert runner.process.returncode == 0
    finally:
        if runner.process.poll() is None:
            runner.process.kill()
            runner.process.wait()
