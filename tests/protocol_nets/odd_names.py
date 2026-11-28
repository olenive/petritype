"""The shared conformance net ``odd-names`` (PROTOCOL.md §2, §11, §12.1): a
net whose places, transitions, port, subnet place and template all have names
holding a slash, a hash, a space and a double quote, so that a transcript
shows such names addressed by path and carried in requests, results,
notifications and the log, compared exactly as given.

Its names, places, transitions, template and seed are fixed in PROTOCOL.md
§11, so that the Rust runner can build the same net from the text alone and
replay ``tests/protocol/15_odd_names.json``. Every place holds strings and is
serializable. A memo goes from the outer place ``Inbox #1 / "new"`` by the
transition ``File it #1 / "now"`` into the port ``Clerk's tray #4 / "in"`` of
the subnet place ``Cabinet "C" #2/3``, whose own transition ``Sort #2 / "A-Z"``
moves it into its place ``Folder "D" #5/6``. Every body hands its memo on
unchanged, and none fails. The inbox is the net's port, where a transcript
``inject``s a note: since 2026-11-27 every net not declared open is closed, and
from outside a closed net takes tokens only at its ports.

The cabinet's net is stamped with the template name ``Cabinet #9 / "steel"``
by ``construct_graph(template=...)`` and not registered by that name, as the
``levels`` net does with ``Level``: nothing in the protocol rebuilds a subnet by
its template's name (§12.5).
"""

from petritype import petri_net
from petritype.core.events import Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    SubnetPlaceNode,
)

INBOX = 'Inbox #1 / "new"'
CABINET = 'Cabinet "C" #2/3'
TRAY = 'Clerk\'s tray #4 / "in"'
FOLDER = 'Folder "D" #5/6'
FILE_IT = 'File it #1 / "now"'
SORT = 'Sort #2 / "A-Z"'
TEMPLATE = 'Cabinet #9 / "steel"'


def moves(memo: str) -> str:
    """A memo moves on, unchanged."""
    return memo


def cabinet() -> ExecutableGraph:
    """The cabinet's template: its port, the tray, then the folder, with the
    sort from one to the other. No parameters, no tokens."""
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode(TRAY, str, port=True),
        ListPlaceNode(FOLDER, str),
        FunctionTransitionNode(SORT, moves),
        ArgumentEdgeToTransition(TRAY, SORT, "memo"),
        ReturnedEdgeFromTransition(SORT, FOLDER),
    ], template=TEMPLATE)


@petri_net(name="odd-names", description="Every name holds a slash, a hash, a space and a double quote")
def odd_names() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode(INBOX, str, ["Memo"], port=True),
        SubnetPlaceNode(CABINET, cabinet()),
        FunctionTransitionNode(FILE_IT, moves),
        ArgumentEdgeToTransition(INBOX, FILE_IT, "memo"),
        ReturnedEdgeFromTransition(FILE_IT, Port(CABINET, TRAY)),
    ])
