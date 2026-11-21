"""Record a shared protocol transcript (PROTOCOL.md §11) over pipes.

Runs ``python -m petritype.runner --module <the net's conformance module>`` (the
one ``tests/test_protocol_transcripts.py`` replays it against), sends each
request in turn, collects every line it caused (its notifications and its
response, in order, then whatever else arrives before the runner falls quiet,
which is how a ``start``'s firings are caught), folds them as the harness does
and masks them as a transcript shared across runtimes must be masked. The
result replays through the owner and over pipes with no hand edits.

Usage::

    uv run python tests/record_transcript.py SCENARIO.json -o tests/protocol/NN_name.json

``SCENARIO`` is ``{"net", "params", "sends": [...]}`` or an existing transcript,
whose sends are taken and whose expects are recorded afresh, so a transcript is
re-recorded in place by naming it twice. A send is a request without its id; the
recorder numbers them ``"1"``, ``"2"``, ... as the harness does. ``"<id:NAME>"``
in a send stands for the string an earlier line bound to ``NAME``, as a whole
value: a path names its tokens step by step
(``[{"place": "Depot", "token": "<id:bus>", "net": "cabin"}]``, §12.1).

Masking, per §11: every token id becomes an ``"<id:NAME>"`` capture, named
readably from the token's label (or its JSON, or failing both its place) and
qualified by its place when the name is taken (``ada``, ``bus_ada`` once it is in
the bus's cabin, ``bus_ada_seats``, ``ada_arrived``; ``bus``, ``bus_in_transit``,
``bus_central``); a token of an inner net is named after the vehicle, or
whatever token, holding it. The first occurrence binds; later ones reuse. A
nested id is a capture named after its holder and its name there
(``bus_cabin``). A path is kept as the list of steps it is, its place and net
names as they stand and each step's token id the capture of that token, or
``"<id>"`` where the token has not been named yet (a path is no place to name
one: the token is named where its label is seen). Every ``marking_version`` is
``"<version>"``; every ``label``, ``type_name``, ``message``, ``runtime`` and
``runtime_version`` ``"<label>"``; every ``duration_ms`` ``"<ms>"``; every time
``"<time>"``; every ``detail`` ``"<any>"``; ``topology.edges`` is
``{"<unordered>": [...]}``. ``topology.controls`` is kept as it stands, labels
included: it is what the net declares, the same in every runtime. A ``color`` is
kept too, and must be a hex ``"#rrggbb"`` (lowercase), which every runtime gives
alike (§11, since 2026-10-16); a colour spelt any other way, or any value or key
that embeds a token id in the text form of the library's own descent
(``Shelf/#1/counter/Up``, a name its ``fired_counts`` may hold), cannot be
shared and stops the recording: change the scenario.

Re-recording a transcript keeps what it chose: where the line it expected at the
same place in the same exchange binds a capture, masks plainly with ``"<id>"``
or leaves a value unpinned with ``"<any>"``, the new line does the same, and a
topology's edges keep the order it listed them in while they are the same
edges. So the captures a send names stay bound, and a re-recording differs only
where the runner's answer does. Since 2026-11-21, when the four oldest
transcripts, written by hand with names and masks of their own, were first
re-recorded.

A ``reloaded`` line (``load``, ``reset``) starts the naming of token ids afresh,
since a new generation numbers its tokens anew; names already given stay bound.
"""

from __future__ import annotations

import argparse
import ast
import json
import queue
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_protocol_transcripts import (  # noqa: E402
    ANY, NET_MODULES, UNORDERED, Process, _capture_name, fold, matches, substitute,
)

LABEL_KEYS = frozenset({"label", "type_name", "message", "runtime", "runtime_version"})
TIME_KEYS = frozenset({"started_at", "last_progress_at", "idle_since"})
EMBEDDED_ID = re.compile(r"/#\d")
HEX_COLOUR = re.compile(r"#[0-9a-f]{6}")
SETTLE_SECONDS = 0.2


def slug(text: str) -> str:
    """``"Bus 1"`` to ``"bus"``, ``"Arrived at Central"`` to ``"arrived_at_central"``,
    ``"Platform@Depot"`` to ``"platform_depot"``."""
    words = re.sub(r"[^0-9a-z]+", "_", text.lower()).strip("_")
    return re.sub(r"_\d+$", "", words)[:32] or "token"


def readable(label) -> str | None:
    """A name for a token from its label or its JSON value, or ``None``."""
    value = label
    if isinstance(label, str):
        for parse in (json.loads, ast.literal_eval):
            try:
                value = parse(label)
                break
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                continue
    if isinstance(value, dict):
        value = next((value[key] for key in ("name", "kind", "id") if isinstance(value.get(key), (str, int))), None)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return f"n{value}"
    return slug(str(value)) if isinstance(value, str) else None


def _get(old, key):
    """The value at ``key`` of the line a re-recording expected here, if any."""
    return old.get(key) if isinstance(old, dict) else None


def _at(old, index: int):
    """The element at ``index`` of the list a re-recording expected here, if any."""
    return old[index] if isinstance(old, list) and index < len(old) else None


def _same_kind(old, line: dict) -> bool:
    """Whether the line a re-recording expected stands for this line: the same
    notification, or the response to the same request."""
    if not isinstance(old, dict):
        return False
    if "method" in line:
        return old.get("method") == line["method"]
    return old.get("id") == line.get("id") and ("result" in old) == ("result" in line)


class Masker:
    """Masks the lines of one recording, naming token ids and nested ids as it goes.
    Each walk carries ``old``, the value a re-recorded transcript expected at the
    same position, whose captures and plain masks are kept (``_kept``)."""

    def __init__(self):
        self.bindings: dict[str, str] = {}   # capture name -> the string it stands for
        self.forget()

    def forget(self) -> None:
        """A new generation: token ids and nested ids are named afresh."""
        self.tokens: dict[tuple[str, str], str] = {}   # (net, token id) -> name
        self.stems: dict[tuple[str, str], str] = {}    # (net, token id) -> name before qualifying
        self.nets: dict[str, str] = {}                 # nested id -> name
        self.owners: dict[str, str] = {}               # nested id -> stem of the token holding it
        self.reached: dict[str, str] = {}              # a path, as JSON -> the nested id of the net it names

    def _new_name(self, base: str, place: str | None) -> str:
        candidates = [base]
        if place:
            candidates.append(f"{base}_{slug(place)}")
        for candidate in candidates:
            if candidate not in self.bindings:
                return candidate
        number = 2
        while f"{candidates[-1]}_{number}" in self.bindings:
            number += 1
        return f"{candidates[-1]}_{number}"

    def _kept(self, old, value: str) -> str | None:
        """The capture name the re-recorded transcript gave ``value`` here, when it
        is free or already stands for ``value``."""
        name = _capture_name(old)
        if name is None or self.bindings.get(name, value) != value:
            return None
        return name

    def token(self, net: str, token_id: str, place: str | None, hint, old=None) -> str:
        if old == "<id>":
            return "<id>"
        key = (net, token_id)
        if key not in self.tokens:
            stem = readable(hint) or slug(place or "token")
            base = f"{self.owners[net]}_{stem}" if net in self.owners else stem
            name = self._kept(old, token_id) or self._new_name(base, place)
            self.tokens[key], self.stems[key] = name, stem
            self.bindings[name] = token_id
        return f"<id:{self.tokens[key]}>"

    def nested(self, nested_id: str, path, holder: str | None = None, net_name: str | None = None, old=None) -> str:
        if old == "<id>":
            return "<id>"
        if nested_id not in self.nets:
            if holder is None and isinstance(path, list) and len(path) == 1 and isinstance(path[0], dict) \
                    and isinstance(path[0].get("token"), str):
                holder = self.stems.get(("", path[0]["token"]))
                net_name = path[0].get("net")
            owner = holder or "net"
            name = self._kept(old, nested_id) or self._new_name(
                f"{owner}_{slug(net_name or 'net')}" if net_name else owner, None,
            )
            self.nets[nested_id], self.owners[nested_id] = name, owner
            self.bindings[name] = nested_id
        return f"<id:{self.nets[nested_id]}>"

    def path(self, steps, old=None):
        """A path (§12.1): the list of steps as it stands, each step's token id the
        capture of that token in the net the steps before it reach, or ``"<id>"``
        for a token not named yet (it is named where its label is seen)."""
        if not isinstance(steps, list):
            return steps
        masked = []
        net = ""  # the loaded net
        for index, step in enumerate(steps):
            if isinstance(step, dict) and isinstance(step.get("token"), str):
                step = {**step, "token": self.path_token(net, step["token"], _get(_at(old, index), "token"))}
            masked.append(step)
            net = self.reached.get(json.dumps(steps[:index + 1], sort_keys=True))
        return masked

    def path_token(self, net: str | None, token_id: str, old=None) -> str:
        if old == "<id>" or net is None:
            return "<id>"
        if (net, token_id) in self.tokens:
            return f"<id:{self.tokens[(net, token_id)]}>"
        name = self._kept(old, token_id)
        if name is None:
            return "<id>"
        self.tokens[(net, token_id)] = self.stems[(net, token_id)] = name
        self.bindings[name] = token_id
        return f"<id:{name}>"

    # --- walking a line ---

    def line(self, line: dict, net: str = "", old=None) -> dict:
        """One line, masked. ``net`` is the nested id the line is about when the line
        does not say (the response to a request addressed to an inner net by
        ``nested_id``: an ``inject`` result names its token but not the net).
        ``old`` is the line a re-recorded transcript expected in its place."""
        if line.get("method") == "reloaded":
            self.forget()
        masked = {
            key: (value if key == "id" else self.walk(value, net, None, _get(old, key)))
            for key, value in line.items()
        }
        self._check_shareable(masked, "")
        return masked

    def walk(self, value, net: str, place: str | None, old=None):
        if old == ANY:
            return ANY  # what the re-recorded transcript chose not to pin stays unpinned
        if isinstance(value, list):
            return [self.walk(item, net, place, _at(old, index)) for index, item in enumerate(value)]
        if not isinstance(value, dict):
            return value
        if isinstance(value.get("nested_id"), str):
            net = value["nested_id"]
            self.nested(net, value.get("path"), old=_get(old, "nested_id"))
            if isinstance(value.get("path"), list):
                self.reached[json.dumps(value["path"], sort_keys=True)] = net
        if isinstance(value.get("place"), str):
            place = value["place"]
        masked = {}
        # The token's own id first, so that the nets it holds are named after it.
        for key in sorted(value, key=lambda k: k != "id"):
            masked[key] = self.field(key, value[key], value, net, place, _get(old, key))
        return {key: masked[key] for key in value}

    def field(self, key: str, item, parent: dict, net: str, place: str | None, old=None):
        if old == ANY:
            return ANY
        if key == "color":
            return self.colour(item)
        if key == "controls" and isinstance(item, list):
            return item  # the net's own declaration, labels included: the same in every runtime
        if key == "id" and isinstance(item, str):
            return self.token(net, item, place, parent.get("label", parent.get("value")), old)
        if key == "nested_id" and isinstance(item, str):
            return self.nested(item, parent.get("path"), old=old)
        if key == "path" and isinstance(item, list):
            return self.path(item, old)
        if key in LABEL_KEYS and isinstance(item, str):
            return "<label>"
        if key == "marking_version":
            return "<version>"
        if key == "duration_ms":
            return "<ms>"
        if (key in TIME_KEYS or key.endswith("_at")) and isinstance(item, str):
            return "<time>"
        if key == "detail":
            return "<any>"
        if key == "value":
            return item  # a token's own JSON: the net's data, the same in every runtime
        if key == "edges" and isinstance(item, list):
            edges = self.walk(item, net, place)
            kept = _get(old, UNORDERED)
            if isinstance(kept, list) and matches({UNORDERED: kept}, edges, {}):
                edges = kept  # the same edges: in the order the re-recorded transcript lists them
            return {UNORDERED: edges}
        if key == "places" and isinstance(item, dict):  # a marking, by place name
            return {name: self.walk(entry, net, name, _get(old, name)) for name, entry in item.items()}
        if key == "nets" and isinstance(item, list):
            holder = self.stems.get((net, parent.get("id"))) if isinstance(parent.get("id"), str) else None
            return [self.nets_entry(entry, holder, net, place, _at(old, index)) for index, entry in enumerate(item)]
        if key in ("consumed", "produced") and isinstance(item, list):
            return [self.pair(entry, net, _at(old, index)) for index, entry in enumerate(item)]
        return self.walk(item, net, place, old)

    def nets_entry(self, entry, holder: str | None, net: str = "", place: str | None = None, old=None):
        """``{name, nested_id, path, template?, count}``: the nested id is named after
        the token holding the net and the net's name there. Anything else under a
        ``nets`` key (``describe``'s list of nets, whose ``params`` carry a
        ``type_name``) is walked as any other value."""
        if not isinstance(entry, dict) or not isinstance(entry.get("nested_id"), str):
            return self.walk(entry, net, place, old)
        masked = dict(entry)
        masked["nested_id"] = self.nested(entry["nested_id"], entry.get("path"), holder=holder,
                                          net_name=entry.get("name") or None, old=_get(old, "nested_id"))
        if isinstance(entry.get("path"), list):
            self.reached[json.dumps(entry["path"], sort_keys=True)] = entry["nested_id"]
            masked["path"] = self.path(entry["path"], _get(old, "path"))
        return masked

    def pair(self, entry, net: str, old=None):
        """``[place, id, label?, color?, keys?]`` of a firing or a log entry."""
        if not (isinstance(entry, list) and len(entry) >= 2 and isinstance(entry[1], str)):
            return entry
        place, token_id, *rest = entry
        label = rest[0] if rest else None
        masked = [place, self.token(net, token_id, place, label, _at(old, 1))]
        if rest:
            masked.append("<label>")
        if len(rest) >= 2:
            masked.append(None if rest[1] is None else self.colour(rest[1]))
            masked.extend(rest[2:])
        return masked

    def colour(self, value):
        """A token's colour, kept: shared only as a lowercase ``#rrggbb``."""
        if not (isinstance(value, str) and HEX_COLOUR.fullmatch(value)):
            raise ValueError(f"a shared transcript carries a colour only as #rrggbb, not {value!r}")
        return value

    def _check_shareable(self, value, where: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "value":
                    continue
                if EMBEDDED_ID.search(key):
                    raise ValueError(f"{where}.{key}: a key embeds a token id, which a shared transcript cannot pin")
                self._check_shareable(item, f"{where}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._check_shareable(item, f"{where}[{index}]")
        elif isinstance(value, str) and EMBEDDED_ID.search(value):
            raise ValueError(f"{where}: {value!r} embeds a token id, which a shared transcript cannot pin")


# --- the process -------------------------------------------------------------------------------


def collect(process: Process, request_id: str, settle: float) -> list[dict]:
    """Every line one request caused: up to its response, then whatever arrives
    before the runner has been quiet for ``settle`` seconds."""
    lines = []
    while True:
        line = process.next_line()
        if line is None:
            raise RuntimeError(f"the runner closed its output before answering {request_id}; "
                               f"stderr:\n{process.stderr.decode()}")
        lines.append(line)
        if line.get("id") == request_id and ("result" in line or "error" in line):
            break
    while True:
        try:
            line = process.lines.get(timeout=settle)
        except queue.Empty:
            return lines
        if line is None:
            process.lines.put(None)  # for close()
            return lines
        if line.get("method") != "heartbeat":
            lines.append(line)


def record(scenario: dict, settle: float = SETTLE_SECONDS) -> dict:
    net = scenario["net"]
    sends = scenario.get("sends") or [exchange["send"] for exchange in scenario["exchanges"]]
    # What a transcript being re-recorded expected, exchange by exchange: its names are kept.
    olds = [] if scenario.get("sends") else [exchange.get("expect", []) for exchange in scenario["exchanges"]]
    if net not in NET_MODULES:
        raise SystemExit(f"no conformance module carries {net!r}: add it to NET_MODULES")
    masker = Masker()
    exchanges = []
    process = Process(net)
    try:
        for index, send in enumerate(sends):
            request_id = str(index + 1)
            where = f"send {index} ({send.get('method')})"
            request = {"id": request_id, "method": send["method"],
                       "params": substitute(send.get("params", {}), masker.bindings, where)}
            process.send(request)
            lines = fold(collect(process, request_id, settle))
            old_lines = fold(olds[index]) if index < len(olds) else []
            addressed = request["params"].get("nested_id") if isinstance(request["params"], dict) else None
            try:
                expect = [
                    masker.line(
                        line,
                        addressed if line.get("id") == request_id and isinstance(addressed, str)
                        and addressed in masker.nets else "",
                        old_lines[n] if n < len(old_lines) and _same_kind(old_lines[n], line) else None,
                    )
                    for n, line in enumerate(lines)
                ]
            except ValueError as error:
                raise SystemExit(f"{where}: {error}") from None
            exchanges.append({"send": send, "expect": expect})
        rest = process.close()
        if rest:
            raise SystemExit(f"lines after the last request: {rest}")
    finally:
        if process.process.poll() is None:
            process.process.kill()
            process.process.wait()
    return {"net": net, "params": scenario.get("params", {}), "exchanges": exchanges}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenario", type=Path, help='{"net", "params", "sends"} or a transcript to re-record')
    parser.add_argument("-o", "--output", type=Path, help="where to write the transcript (default: stdout)")
    parser.add_argument("--settle", type=float, default=SETTLE_SECONDS,
                        help="seconds of quiet after a response before the next request (default %(default)s)")
    arguments = parser.parse_args()
    transcript = record(json.loads(arguments.scenario.read_text()), arguments.settle)
    text = json.dumps(transcript, indent=1) + "\n"
    if arguments.output is None:
        sys.stdout.write(text)
    else:
        arguments.output.write_text(text)
        print(f"{arguments.output}: {len(transcript['exchanges'])} exchanges, "
              f"{sum(len(e['expect']) for e in transcript['exchanges'])} lines", file=sys.stderr)


if __name__ == "__main__":
    main()
