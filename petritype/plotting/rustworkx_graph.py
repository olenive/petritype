import logging
from typing import Any, Optional, Union, Sequence, Iterable

from pydantic import BaseModel
from petritype._optional import require

require("rustworkx", "viz")

from rustworkx import PyDiGraph

from petritype.core.data_structures import (
    ArgumentName, FunctionFullName, NodeIndex, ReturnIndex, TransitionNodeName, TypeName,
    TypeRelationship
)
from petritype.core.events import PlaceKey, Port
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition, DrainEdge, ExecutableGraph, MutateEdge, MutateWhereEdge, ReadAllEdge, ReadEdge,
    ReadWhereEdge, ReturnedEdgeFromTransition, SnapshotEdge, SubnetPlaceNode, SurveyEdge, TakeWhereEdge,
)
from petritype.core.relationship_graph_components import TypeToTypeEdges, TypeToFunctionEdges, FunctionToTypeEdges

logger = logging.getLogger(__name__)


class RustworkxArgumentEdgeData(BaseModel):
    source_place_node_name: PlaceKey  # a place's name, or a port's Port pair
    target_transition_node_name: TransitionNodeName
    argument: ArgumentName
    drain: bool = False  # True for DrainEdge: takes every token, zero or more
    where: Optional[Any] = None  # a TakeWhereEdge's Filter, which a drawing writes on the edge
    reaches: tuple[str, ...] = ()  # the ports the edge declares it reaches in its tokens' nets, since 2026-11-23

    def __hash__(self):
        return hash((
            self.source_place_node_name, self.target_transition_node_name, self.argument, self.drain, self.where,
            self.reaches,
        ))

    def __eq__(self, other):
        if not isinstance(other, RustworkxArgumentEdgeData):
            return NotImplemented
        return (
            self.source_place_node_name == other.source_place_node_name and
            self.target_transition_node_name == other.target_transition_node_name and
            self.argument == other.argument and
            self.drain == other.drain and
            self.where == other.where and
            self.reaches == other.reaches
        )


class RustworkxReturnedEdgeData(BaseModel):
    source_transition_node_name: TransitionNodeName
    target_place_node_name: PlaceKey  # a place's name, or a port's Port pair
    return_index: Optional[ReturnIndex]

    def __hash__(self):
        return hash((self.source_transition_node_name, self.target_place_node_name, self.return_index))

    def __eq__(self, other):
        if not isinstance(other, RustworkxReturnedEdgeData):
            return NotImplemented
        return (
            self.source_transition_node_name == other.source_transition_node_name and
            self.target_place_node_name == other.target_place_node_name and
            self.return_index == other.return_index
        )


class RustworkxReadEdgeData(BaseModel):
    """A non-consuming edge, one that leaves its tokens in their places: a snapshot
    or survey, a mutate or mutate-where edge, or a read, read-where or read-all edge
    (since 2026-11-23). The class keeps the name it had when the group was called
    read edges."""
    source_place_node_name: PlaceKey  # a place's name, or a port's Port pair
    target_transition_node_name: TransitionNodeName
    argument: ArgumentName
    mutate: bool  # True for MutateEdge (reads and writes), False for SnapshotEdge (a copy) and a read edge
    survey: bool = False  # True for SurveyEdge: a copy of every token, zero or more
    where: Optional[Any] = None  # a MutateWhereEdge's or ReadWhereEdge's Filter, which a drawing writes on the edge
    read: bool = False  # True for a ReadEdge, ReadWhereEdge or ReadAllEdge: the live tokens, to look at
    read_all: bool = False  # True for ReadAllEdge: every token, live, zero or more
    reaches: tuple[str, ...] = ()  # the ports the edge declares it reaches in its tokens' nets, since 2026-11-23

    def __hash__(self):
        return hash((
            self.source_place_node_name, self.target_transition_node_name, self.argument, self.mutate, self.survey,
            self.where, self.read, self.read_all, self.reaches,
        ))

    def __eq__(self, other):
        if not isinstance(other, RustworkxReadEdgeData):
            return NotImplemented
        return (
            self.source_place_node_name == other.source_place_node_name and
            self.target_transition_node_name == other.target_transition_node_name and
            self.argument == other.argument and
            self.mutate == other.mutate and
            self.survey == other.survey and
            self.where == other.where and
            self.read == other.read and
            self.read_all == other.read_all and
            self.reaches == other.reaches
        )


class RustworkxGraph:

    @staticmethod
    def type_relationship_edges(
        node_names_to_type_relationships: TypeToTypeEdges | TypeToFunctionEdges | FunctionToTypeEdges,
        node_names_to_node_indices: dict[Union[TypeName, FunctionFullName], int]
    ) -> Sequence[tuple[NodeIndex, NodeIndex, TypeRelationship]]:
        out = []
        for (name_from, name_to), relationship in node_names_to_type_relationships.items():
            try:
                index_from: NodeIndex = node_names_to_node_indices[name_from]
            except KeyError:
                # NOTE: This often occurs when name_from is a common type that is not included in COMMON_TYPES.
                logger.debug("KeyError: %s", name_from)
                continue
            try:
                index_to: NodeIndex = node_names_to_node_indices[name_to]
            except KeyError:
                # NOTE: This often occurs when name_to is a common type that is not included in COMMON_TYPES.
                logger.debug("KeyError: %s", name_to)
                continue
            out.append((index_from, index_to, relationship))
        return tuple(out)

    @staticmethod
    def argument_edges_to_transitions(
        edges_to_transitions: Iterable[ArgumentEdgeToTransition],
        place_names_to_indices: dict[PlaceKey, NodeIndex],
        transition_names_to_indices: dict[TransitionNodeName, NodeIndex],
    ) -> Sequence[tuple[NodeIndex, NodeIndex, RustworkxArgumentEdgeData]]:
        out = []
        for edge in edges_to_transitions:
            if not isinstance(edge, ArgumentEdgeToTransition):
                raise ValueError("All edges must be of type ArgumentEdgeToTransition.")
            out.append((
                place_names_to_indices[edge.place_node_name],
                transition_names_to_indices[edge.transition_node_name],
                RustworkxArgumentEdgeData(
                    source_place_node_name=edge.place_node_name,
                    target_transition_node_name=edge.transition_node_name,
                    argument=edge.argument,
                    drain=isinstance(edge, DrainEdge),
                    where=edge.where if isinstance(edge, TakeWhereEdge) else None,
                    reaches=edge.reaches,
                ),
            ))
        return tuple(out)

    @staticmethod
    def returned_edges_from_transitions(
        edges_from_transitions: Iterable[ReturnedEdgeFromTransition],
        place_names_to_indices: dict[PlaceKey, NodeIndex],
        transition_names_to_indices: dict[TransitionNodeName, NodeIndex],
    ) -> Sequence[tuple[NodeIndex, NodeIndex, RustworkxReturnedEdgeData]]:
        out = []
        for edge in edges_from_transitions:
            if not isinstance(edge, ReturnedEdgeFromTransition):
                raise ValueError("All edges must be of type ReturnedEdgeFromTransition.")
            out.append((
                transition_names_to_indices[edge.transition_node_name],
                place_names_to_indices[edge.place_node_name],
                RustworkxReturnedEdgeData(
                    source_transition_node_name=edge.transition_node_name,
                    target_place_node_name=edge.place_node_name,
                    return_index=edge.return_index,
                ),
            ))
        return tuple(out)

    @staticmethod
    def read_edges_to_transitions(
        snapshot_edges: Iterable[SnapshotEdge],
        mutate_edges: Iterable[MutateEdge],
        place_names_to_indices: dict[PlaceKey, NodeIndex],
        transition_names_to_indices: dict[TransitionNodeName, NodeIndex],
        read_edges: Iterable[ReadEdge] = (),
    ) -> Sequence[tuple[NodeIndex, NodeIndex, RustworkxReadEdgeData]]:
        """The non-consuming edges of a net, snapshots and surveys, then mutate
        edges, then read edges, as rustworkx edges."""
        out = []
        for edges, mutate in ((snapshot_edges, False), (mutate_edges, True), (read_edges, False)):
            for edge in edges:
                out.append((
                    place_names_to_indices[edge.place_node_name],
                    transition_names_to_indices[edge.transition_node_name],
                    RustworkxReadEdgeData(
                        source_place_node_name=edge.place_node_name,
                        target_transition_node_name=edge.transition_node_name,
                        argument=edge.argument,
                        mutate=mutate,
                        survey=isinstance(edge, SurveyEdge),
                        where=edge.where if isinstance(edge, (MutateWhereEdge, ReadWhereEdge)) else None,
                        read=isinstance(edge, ReadEdge),
                        read_all=isinstance(edge, ReadAllEdge),
                        reaches=edge.reaches,
                    ),
                ))
        return tuple(out)

    @staticmethod
    def is_open(pydigraph: PyDiGraph) -> bool:
        """Whether the net a rustworkx graph shows is declared open, as
        ``from_executable_graph`` records it in the graph's ``attrs``; False for a
        graph built any other way."""
        attrs = getattr(pydigraph, "attrs", None)
        return isinstance(attrs, dict) and attrs.get("open") is True

    @staticmethod
    def from_executable_graph(executable_graph: ExecutableGraph) -> PyDiGraph:
        """The net as a rustworkx graph: one node per place and per transition, one
        edge per edge. A subnet place is one node, its ports are not nodes of their
        own: an edge on a port joins the subnet place's node, and its data keeps the
        port's ``Port`` pair, so a drawing can end it at the port's cell. The graph's
        ``attrs`` are ``{"open": executable_graph.open}`` (since 2026-11-28), so a
        drawing made from it alone can draw an open net's places, its ports apart,
        dashed (``is_open``)."""
        graph = PyDiGraph(attrs={"open": executable_graph.open})
        place_node_indices = graph.add_nodes_from(executable_graph.places)
        transition_node_indices = graph.add_nodes_from(executable_graph.transitions)
        place_names_to_indices: dict[PlaceKey, NodeIndex] = {}
        for index, place in zip(place_node_indices, executable_graph.places):
            place_names_to_indices[place.name] = index
            if isinstance(place, SubnetPlaceNode):
                for port in place.ports:
                    place_names_to_indices[Port(place.name, port.name)] = index
        transition_names_to_indices = {
            transition.name: index
            for index, transition in zip(transition_node_indices, executable_graph.transitions)
        }
        g_edges_to_transitions = RustworkxGraph.argument_edges_to_transitions(
            edges_to_transitions=executable_graph.argument_edges,
            place_names_to_indices=place_names_to_indices,
            transition_names_to_indices=transition_names_to_indices,
        )
        g_edges_from_transitions = RustworkxGraph.returned_edges_from_transitions(
            edges_from_transitions=executable_graph.return_edges,
            place_names_to_indices=place_names_to_indices,
            transition_names_to_indices=transition_names_to_indices,
        )
        g_read_edges = RustworkxGraph.read_edges_to_transitions(
            snapshot_edges=executable_graph.snapshot_edges,
            mutate_edges=executable_graph.mutate_edges,
            place_names_to_indices=place_names_to_indices,
            transition_names_to_indices=transition_names_to_indices,
            read_edges=executable_graph.read_edges,
        )
        graph.add_edges_from(g_edges_to_transitions)
        graph.add_edges_from(g_edges_from_transitions)
        graph.add_edges_from(g_read_edges)
        return graph
