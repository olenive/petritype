from typing import Optional, Union, Sequence, Iterable

from pydantic import BaseModel
from rustworkx import PyDiGraph

from petritype.core.data_structures import (
    ArgumentName, FunctionFullName, NodeIndex, PlaceNodeName, ReturnIndex, TransitionNodeName, TypeName,
    TypeRelationship
)
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition, ExecutableGraph, ReturnedEdgeFromTransition
)
from petritype.core.relationship_graph_components import TypeToTypeEdges, TypeToFunctionEdges, FunctionToTypeEdges


class RustworkxArgumentEdgeData(BaseModel):
    source_place_node_name: PlaceNodeName
    target_transition_node_name: TransitionNodeName
    argument: ArgumentName

    def __hash__(self):
        return hash((self.source_place_node_name, self.target_transition_node_name, self.argument))

    def __eq__(self, other):
        if not isinstance(other, RustworkxArgumentEdgeData):
            return NotImplemented
        return (
            self.source_place_node_name == other.source_place_node_name and
            self.target_transition_node_name == other.target_transition_node_name and
            self.argument == other.argument
        )


class RustworkxReturnedEdgeData(BaseModel):
    source_transition_node_name: TransitionNodeName
    target_place_node_name: PlaceNodeName
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


class RustworkxGraph:

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
                print(f"KeyError: {name_from}")
                continue
            try:
                index_to: NodeIndex = node_names_to_node_indices[name_to]
            except KeyError:
                # NOTE: This often occurs when name_to is a common type that is not included in COMMON_TYPES.
                print(f"KeyError: {name_to}")
                continue
            out.append((index_from, index_to, relationship))
        return tuple(out)

    def argument_edges_to_transitions(
        edges_to_transitions: Iterable[ArgumentEdgeToTransition],
        place_names_to_indices: dict[PlaceNodeName, NodeIndex],
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
                ),
            ))
        return tuple(out)

    def returned_edges_from_transitions(
        edges_from_transitions: Iterable[ReturnedEdgeFromTransition],
        place_names_to_indices: dict[PlaceNodeName, NodeIndex],
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

    def from_executable_graph(executable_graph: ExecutableGraph) -> PyDiGraph:
        graph = PyDiGraph()
        place_node_indices = graph.add_nodes_from(executable_graph.places)
        transition_node_indices = graph.add_nodes_from(executable_graph.transitions)
        place_names_to_indices = {
            place.name: index
            for index, place in zip(place_node_indices, executable_graph.places)
        }
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
        graph.add_edges_from(g_edges_to_transitions)
        graph.add_edges_from(g_edges_from_transitions)
        return graph
