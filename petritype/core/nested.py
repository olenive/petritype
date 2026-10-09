"""Nets as tokens: the one import for programs whose tokens hold Petri nets.

A token of a place declared ``ListPlaceNode(..., nested=True)`` holds inner nets:
it is an ``ExecutableGraph`` itself (one unnamed inner net) or implements
``__petritype_nets__(self) -> dict[str, ExecutableGraph]``. The outer net runs
them as part of running itself, gathers their events into its own log as
``Nested`` records with the path to the net they happened in, and lets its
bodies reach inside the tokens they hold with ``NestedNets.put``, ``take`` and
``set_param``. ``NetParams`` are the net-level parameters an inner net exposes
for that (``construct_graph(..., params={...})``, read as ``graph.params``).

Everything here lives in ``petritype.core.executable_graph_components`` (the
engine needs it); this module is the short spelling.
"""

from petritype.core.events import Nested
from petritype.core.executable_graph_components import NESTED_NETS_METHOD, NestedNets, NetParams

__all__ = ["NESTED_NETS_METHOD", "Nested", "NestedNets", "NetParams"]
