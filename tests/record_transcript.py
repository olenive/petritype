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
in a send stands for the string an earlier line bound to ``NAME``, whole or
inside a longer string (``"transit/Depot/#<id:bus>/cabin"``).

Masking, per §11: every token id becomes an ``"<id:NAME>"`` capture, named
readably from the token's label (or its JSON, or failing both its place) and
qualified by its place when the name is taken (``ada``, ``bus_ada`` once it is in
the bus's cabin, ``bus_ada_seats``, ``ada_arrived``; ``bus``, ``bus_in_transit``,
``bus_central``); a token of an inner net is named after the vehicle, or
whatever token, holding it. The first occurrence binds; later ones reuse. A
nested id is a capture named after its holder and its name there
(``bus_cabin``). Every path is ``"<path>"``; every ``marking_version``
``"<version>"``; every ``label``, ``type_name``, ``message``, ``runtime`` and
``runtime_version`` ``"<label>"``; every ``duration_ms`` ``"<ms>"``; every time
``"<time>"``; every ``detail`` ``"<any>"``; ``topology.edges`` is
``{"<unordered>": [...]}``. A line carrying a ``color``, or any other value or
key that embeds a token id (an outer ``fired_counts`` after inner firings, say),
cannot be shared and stops the recording: change the scenario.

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

from tests.test_protocol_transcripts import NET_MODULES, Process, fold, substitute  # noqa: E402

LABEL_KEYS = frozenset({"label", "type_name", "message", "runtime", "runtime_version"})
TIME_KEYS = frozenset({"started_at", "last_progress_at", "idle_since"})
EMBEDDED_ID = re.compile(r"/#\d")
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


class Masker:
    """Masks the lines of one recording, naming token ids and nested ids as it goes."""

    def __init__(self):
        self.bindings: dict[str, str] = {}   # capture name -> the string it stands for
        self.forget()

    def forget(self) -> None:
        """A new generation: token ids and nested ids are named afresh."""
        self.tokens: dict[tuple[str, str], str] = {}   # (net, token id) -> name
        self.stems: dict[tuple[str, str], str] = {}    # (net, token id) -> name before qualifying
        self.nets: dict[str, str] = {}                 # nested id -> name
        self.owners: dict[str, str] = {}               # nested id -> stem of the token holding it

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

    def token(self, net: str, token_id: str, place: str | None, hint) -> str:
        key = (net, token_id)
        if key not in self.tokens:
            stem = readable(hint) or slug(place or "token")
            base = f"{self.owners[net]}_{stem}" if net in self.owners else stem
            name = self._new_name(base, place)
            self.tokens[key], self.stems[key] = name, stem
            self.bindings[name] = token_id
        return f"<id:{self.tokens[key]}>"

    def nested(self, nested_id: str, path, holder: str | None = None, net_name: str | None = None) -> str:
        if nested_id not in self.nets:
            if holder is None and isinstance(path, str):
                segments = path.split("/")
                if len(segments) == 4 and segments[2].startswith("#"):
                    holder = self.stems.get(("", segments[2][1:]))
                    net_name = segments[3]
            owner = holder or "net"
            name = self._new_name(f"{owner}_{slug(net_name or 'net')}" if net_name else owner, None)
            self.nets[nested_id], self.owners[nested_id] = name, owner
            self.bindings[name] = nested_id
        return f"<id:{self.nets[nested_id]}>"

    # --- walking a line ---

    def line(self, line: dict, net: str = "") -> dict:
        """One line, masked. ``net`` is the nested id the line is about when the line
        does not say (the response to a request addressed to an inner net by
        ``nested_id``: an ``inject`` result names its token but not the net)."""
        if line.get("method") == "reloaded":
            self.forget()
        masked = {key: (value if key == "id" else self.walk(value, net, None)) for key, value in line.items()}
        self._check_shareable(masked, "")
        return masked

    def walk(self, value, net: str, place: str | None):
        if isinstance(value, list):
            return [self.walk(item, net, place) for item in value]
        if not isinstance(value, dict):
            return value
        if isinstance(value.get("nested_id"), str):
            net = value["nested_id"]
            self.nested(net, value.get("path"))
        if isinstance(value.get("place"), str):
            place = value["place"]
        masked = {}
        # The token's own id first, so that the nets it holds are named after it.
        for key in sorted(value, key=lambda k: k != "id"):
            masked[key] = self.field(key, value[key], value, net, place)
        return {key: masked[key] for key in value}

    def field(self, key: str, item, parent: dict, net: str, place: str | None):
        if key == "color":
            raise ValueError("a shared transcript uses no color: this net's tokens have one")
        if key == "id" and isinstance(item, str):
            return self.token(net, item, place, parent.get("label", parent.get("value")))
        if key == "nested_id" and isinstance(item, str):
            return self.nested(item, parent.get("path"))
        if key == "path" and isinstance(item, str):
            return "<path>"
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
            return {"<unordered>": self.walk(item, net, place)}
        if key == "places" and isinstance(item, dict):  # a marking, by place name
            return {name: self.walk(entry, net, name) for name, entry in item.items()}
        if key == "nets" and isinstance(item, list):
            holder = self.stems.get((net, parent.get("id"))) if isinstance(parent.get("id"), str) else None
            return [self.nets_entry(entry, holder) for entry in item]
        if key in ("consumed", "produced") and isinstance(item, list):
            return [self.pair(entry, net) for entry in item]
        return self.walk(item, net, place)

    def nets_entry(self, entry, holder: str | None):
        """``{name, nested_id, path, template?, count}``: the nested id is named after
        the token holding the net and the net's name there."""
        if not isinstance(entry, dict) or not isinstance(entry.get("nested_id"), str):
            return entry
        masked = dict(entry)
        masked["nested_id"] = self.nested(entry["nested_id"], entry.get("path"), holder=holder,
                                          net_name=entry.get("name") or None)
        if "path" in masked:
            masked["path"] = "<path>"
        return masked

    def pair(self, entry, net: str):
        """``[place, id, label?, color?, keys?]`` of a firing or a log entry."""
        if not (isinstance(entry, list) and len(entry) >= 2 and isinstance(entry[1], str)):
            return entry
        place, token_id, *rest = entry
        label = rest[0] if rest else None
        masked = [place, self.token(net, token_id, place, label)]
        if rest:
            masked.append("<label>")
        if len(rest) >= 2:
            if rest[1] is not None:
                raise ValueError("a shared transcript uses no color: this net's tokens have one")
            masked.extend(rest[1:])
        return masked

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
            addressed = request["params"].get("nested_id") if isinstance(request["params"], dict) else None
            try:
                expect = [masker.line(line, addressed if line.get("id") == request_id and isinstance(addressed, str)
                                      and addressed in masker.nets else "") for line in lines]
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
