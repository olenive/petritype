"""Nets inside nets: the one import for programs whose tokens or places hold Petri nets.

A token of a place declared ``ListPlaceNode(..., nested=True)`` holds inner nets:
it is an ``ExecutableGraph`` itself (one unnamed inner net) or implements
``__petritype_nets__(self) -> dict[str, ExecutableGraph]``. A
``SubnetPlaceNode(name, graph)`` holds one net, its subnet, for the outer net's
whole life, reached from outside only through the places the subnet marks
``ListPlaceNode(..., port=True)``, which an edge names by ``Port(name, port)``.
The outer net runs them as part of running itself, in the order the place's
``Stepping`` rule gives (``stepping="inner_first"``, the default,
``"take_turns"``, ``"outer_first"`` or ``"passive"``), gathers their events into
its own log as ``Nested`` records with the path to the net they happened in, a
tuple of ``PathStep`` values, tells a firing inside as an ``InnerFiring``, and
lets its bodies reach inside the tokens they hold with ``NestedNets.put`` and
``take``, at the ports their edges name in ``reaches``. ``NetParams`` are the
net-level parameters an inner net exposes to the program and the runner
(``construct_graph(..., params={...})``, read as ``graph.params``). The program
reads a subnet with ``graph.subnet(name)`` and steps or changes it inside
``NestedNets.subnet_scope``.

Every net is closed unless it declares itself open (``construct_graph(...,
open=True)``): from outside it, tokens are put, taken and replaced only at its
ports (``ClosedNetError`` otherwise), and a body of the net around it may not set
its knobs, fire it by name or load a marking into it (``HeldByBodyError``).
``ClosedNets`` says who is outside and what stays allowed.

Everything here lives in ``petritype.core.executable_graph_components`` and
``petritype.core.events`` (the engine needs it); this module is the short
spelling.
"""

from petritype.core.events import InnerFiring, Nested, PathStep, Port
from petritype.core.executable_graph_components import (
    NESTED_NETS_METHOD, ClosedNetError, ClosedNets, HeldByBodyError, NestedNets, NetParams, Stepping,
    SubnetHeldError, SubnetPlaceNode,
)

__all__ = [
    "NESTED_NETS_METHOD", "ClosedNetError", "ClosedNets", "HeldByBodyError", "InnerFiring", "Nested", "NestedNets",
    "NetParams", "PathStep", "Port", "Stepping", "SubnetHeldError", "SubnetPlaceNode",
]
