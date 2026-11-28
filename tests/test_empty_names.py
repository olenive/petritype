"""No name is empty (since 2026-11-28). A name may hold any character, and at
least one: the empty string is refused where each name is given, with
``ValueError`` (pydantic's ``ValidationError``, a ``ValueError``, for a node or a
net), as the build refuses an empty entry of an edge's ``reaches``. Until then
only that entry was refused.

The names the library takes, and where an empty one is refused:

- a place, a subnet place and a transition, as the node is built, and so a port,
  which is a place of its subnet; a ``Port`` pair naming an empty port finds no
  port, as before;
- the group of each of the three;
- a key of a place, given as a dict or as attribute names;
- a keyword of a transition, as the node is built, and in a ``SetParam`` the
  runtime applies;
- a parameter of the net, its template and the argument of an input edge, as the
  net is built;
- a net, in ``petri_net``, and a control, in ``petri_net`` when the control-map is
  a dict, and when the protocol runner loads the net, a callable's map included.

A token that is itself a net carries it under the empty name (``""`` in
``__petritype_nets__`` and in a path), which is the one empty name with a meaning,
and stays. A control's ``node`` of ``""`` says it targets no node, as before."""

import asyncio
from datetime import datetime, timezone

import pytest

from petritype import petri_net
from petritype.core.events import Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    MutateEdge,
    ReadEdge,
    ReturnedEdgeFromTransition,
    SnapshotEdge,
    SubnetPlaceNode,
)
from petritype.runner.owner import Owner
from petritype.runner.registry import Registry
from petritype.runtime import ControlSpec, RunContext, Runner, SetParam

construct = ExecutableGraphOperations.construct_graph


def same(x: int) -> int:
    return x


def keyed(x: int, k: int = 1) -> int:
    return x


def pipe(**settings) -> ExecutableGraph:
    return construct([
        ListPlaceNode("In", int, port=True), ListPlaceNode("Out", int, port=True),
        FunctionTransitionNode("Pass", same),
        ArgumentEdgeToTransition("In", "Pass", "x"), ReturnedEdgeFromTransition("Pass", "Out"),
    ], **settings)


def taking(edge) -> list:
    """A net whose ``T`` reads ``A`` through ``edge``."""
    return [ListPlaceNode("A", int), ListPlaceNode("B", int), FunctionTransitionNode("T", same), edge,
            ReturnedEdgeFromTransition("T", "B")]


EMPTY = {
    "place": lambda: ListPlaceNode("", int),
    "port": lambda: ListPlaceNode("", int, port=True),
    "subnet place": lambda: SubnetPlaceNode("", pipe()),
    "transition": lambda: FunctionTransitionNode("", same),
    "group of a place": lambda: ListPlaceNode("A", int, group=""),
    "group of a subnet place": lambda: SubnetPlaceNode("S", pipe(), group=""),
    "group of a transition": lambda: FunctionTransitionNode("T", same, group=""),
    "key": lambda: ListPlaceNode("A", int, keys={"": lambda token: token}),
    "key by attribute": lambda: ListPlaceNode("A", int, keys=[""]),
    "keyword": lambda: FunctionTransitionNode("T", keyed, kwargs={"": 2}),
    "parameter": lambda: pipe(params={"speed": 1, "": 2}),
    "template": lambda: pipe(template=""),
    "argument": lambda: construct(taking(ArgumentEdgeToTransition("A", "T", ""))),
    "snapshot argument": lambda: construct(taking(SnapshotEdge("A", "T", ""))),
    "mutate argument": lambda: construct(taking(MutateEdge("A", "T", ""))),
    "read argument": lambda: construct(taking(ReadEdge("A", "T", ""))),
}


@pytest.mark.parametrize("make", EMPTY.values(), ids=EMPTY)
def test_an_empty_name_is_refused_as_it_is_given(make):
    with pytest.raises(ValueError, match="named by the empty string; a name holds at least one character"):
        make()


def test_a_port_pair_naming_no_port_is_refused_as_before():
    with pytest.raises(ValueError, match="port"):
        construct([
            SubnetPlaceNode("S", pipe()), FunctionTransitionNode("T", same),
            ArgumentEdgeToTransition(Port("S", ""), "T", "x"), ReturnedEdgeFromTransition("T", Port("S", "Out")),
        ])


def test_a_name_of_one_space_is_a_name():
    graph = construct([
        ListPlaceNode(" ", int, [1], group=" ", keys={" ": lambda token: token}),
        ListPlaceNode("B", int),
        FunctionTransitionNode("  ", keyed, kwargs={"k": 2}, group=" "),
        ArgumentEdgeToTransition(" ", "  ", "x"), ReturnedEdgeFromTransition("  ", "B"),
    ], params={" ": 1}, template=" ")
    assert graph.groups == (" ",) and graph.params == {" ": 1}


def test_a_set_param_naming_an_empty_keyword_is_refused():
    graph = construct([ListPlaceNode("A", int), FunctionTransitionNode("T", keyed, kwargs={"k": 2})])
    strict = RunContext(graph=graph, inbox=asyncio.Queue(), strict=True)
    strict.inbox.put_nowait(SetParam("T", {"": 3}))
    with pytest.raises(ValueError, match="empty string"):
        Runner.drain_inbox(strict)
    loose = RunContext(graph=graph, inbox=asyncio.Queue(), strict=False)
    loose.inbox.put_nowait(SetParam("T", {"": 3}))
    assert Runner.drain_inbox(loose) == 0
    assert graph.transitions[0].kwargs == {"k": 2}


def test_a_net_and_a_control_need_a_name():
    with pytest.raises(ValueError, match="a net is named by the empty string"):
        petri_net(name="")(pipe)
    with pytest.raises(ValueError, match="a control of net 'pipe' is named by the empty string"):
        petri_net(name="pipe", controls={"": ControlSpec.fire("Pass", "Pass")})(pipe)


def test_the_runner_refuses_to_load_a_net_whose_control_map_names_a_control_by_the_empty_string():
    @petri_net(name="pipe", controls=lambda: {"": ControlSpec.fire("Pass", "Pass")})
    def built() -> ExecutableGraph:
        return pipe()

    async def go():
        owner = Owner(Registry.from_factories([built]))
        out = await owner.handle({"id": "1", "method": "load", "params": {"net": "pipe"}},
                                 datetime(2026, 11, 28, tzinfo=timezone.utc))
        assert out.lines[-1]["error"]["code"] == "load_failed"
        assert "empty string" in out.lines[-1]["error"]["message"]
    asyncio.run(go())


def test_a_token_that_is_a_net_carries_it_under_the_empty_name():
    net = pipe()
    assert net.__petritype_nets__() == {"": net}
