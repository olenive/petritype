"""The shared conformance net ``post-office`` (PROTOCOL.md §11, §5.1, §12.7): a
post is a net of its own that marks one place as a port and is closed, as every
net not declared open is (since 2026-11-27; until then a net was closed because
it marked a port), and a second post is the same net declared open, so that a
transcript shows a closed net and an open one, an outer body that writes at a port and lands, one that
writes to a place that is not a port and fails, and an ``inject`` refused with
``closed_net`` where the same ``inject`` into the open post lands (since
2026-11-23).

Its names, places, transitions, templates and seed are fixed in PROTOCOL.md §11,
so that the Rust runner can build the same net from the text alone and replay
``tests/protocol/19_closed_nets.json``. A post is a net built from the template
``Post`` or ``Open Post``: ``Inbox``, a port, and ``Ledger``, its own place, both
of strings and empty, and no transitions; ``Open Post`` declares itself open.
The net's ``Deliver`` takes a letter and puts it into the front post's ``Inbox``
through a mutate edge that names ``Inbox`` in its ``reaches``, as a body writing
a port of a closed net it holds must since 2026-11-27. Its ``Forge`` takes a
forgery and the front post, and puts the forgery into the post's ``Ledger``,
which the front post, being closed, refuses, so the firing fails. A refusal in
a consuming firing costs what any failure costs: the net restores nothing on
failure, so both tokens are lost (the Rust core cannot restore a token that
carries a net). Had it landed, the body would return the post into ``Posts``.

The post office itself is declared open (since 2026-11-27). A closed net shields
the nets inside it, the nets its tokens carry included, from the program and the
runner: an ``inject`` into a post, by path or by nested id, lands only when every
net around the post is declared open. Open, the post office leaves each post to
its own rule, so the transcript's three injects keep their meaning: the closed
post takes a note at its ``Inbox`` and refuses one at its ``Ledger``
(``closed_net``), both addressed by path, and the open post takes one at its
``Ledger``, addressed by its nested id since 2026-11-27, so that the shared
transcripts hold an ``inject`` by nested id that lands. The bodies are not affected: ``Deliver``
and ``Forge`` are judged by the posts' own rule and the edges' ``reaches``.

The templates are registered, so a post has a saved form and ``Posts`` is
serializable through them, as the ``trays`` net registers its own.
"""

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    NestedNets,
    ReturnedEdgeFromTransition,
)
from petritype.core.serialisation import Templates


def _post(open: bool) -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Inbox", str, port=True),
        ListPlaceNode("Ledger", str),
    ], open=open)


@Templates.template("Post")
def post_template() -> ExecutableGraph:
    """The ``Post`` template: the port ``Inbox`` and the place ``Ledger``, of
    strings, holding no tokens, and no transitions. No parameters. It does not
    declare itself open, so it is closed."""
    return _post(open=False)


@Templates.template("Open Post")
def open_post_template() -> ExecutableGraph:
    """The ``Open Post`` template: a ``Post`` that declares itself open."""
    return _post(open=True)


def deliver(letter: str, post: ExecutableGraph) -> None:
    """The letter goes into the post's ``Inbox``, a port."""
    NestedNets.put(post, "Inbox", [letter])


def forge(forgery: str, post: ExecutableGraph) -> ExecutableGraph:
    """The forgery goes into the post's ``Ledger``, which is not a port: a closed
    post refuses it. Had it landed, the post would go back into ``Posts``."""
    NestedNets.put(post, "Ledger", [forgery])
    return post


@petri_net(
    name="post-office",
    description="Letters are delivered to a post's inbox, and a forgery written into its ledger is refused",
)
def post_office() -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Letters", str, ["Dear Ada"]),
        ListPlaceNode("Forgeries", str, ["IOU"]),
        ListPlaceNode("Posts", ExecutableGraph, [post_template(), open_post_template()], nested=True),
        FunctionTransitionNode("Deliver", deliver),
        ArgumentEdgeToTransition("Letters", "Deliver", "letter"),
        MutateEdge("Posts", "Deliver", "post", reaches=("Inbox",)),
        FunctionTransitionNode("Forge", forge),
        ArgumentEdgeToTransition("Forgeries", "Forge", "forgery"),
        ArgumentEdgeToTransition("Posts", "Forge", "post"),
        ReturnedEdgeFromTransition("Forge", "Posts"),
    ], open=True)
