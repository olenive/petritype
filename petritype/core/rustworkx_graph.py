from typing import Union, Sequence, Iterable

from rustworkx import PyDiGraph

from petritype.core.data_structures import (
    ArgumentName, FunctionFullName, NodeIndex, PlaceNodeName, ReturnIndex, TransitionNodeName, TypeName,
    TypeRelationship
)
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition, ExecutableGraph, ReturnedEdgeFromTransition
)
from petritype.core.relationship_graph_components import TypeToTypeEdges, TypeToFunctionEdges, FunctionToTypeEdges


class RustworkxGraph:

    def type_relationship_edges(
        node_names_to_type_relationships: TypeToTypeEdges | TypeToFunctionEdges | FunctionToTypeEdges,
        node_names_to_node_indices: dict[Union[TypeName, FunctionFullName], int]
    ) -> Sequence[tuple[NodeIndex, NodeIndex, TypeRelationship]]:
        out = []
        for (name_from, name_to), relationship in node_names_to_type_relationships.items():
            index_from: NodeIndex = node_names_to_node_indices[name_from]
            try:
                index_to: NodeIndex = node_names_to_node_indices[name_to]
            except KeyError:
                print(f"KeyError: {name_to}")
                continue
            out.append((index_from, index_to, relationship))
        return tuple(out)

    def argument_edges_to_transitions(
        edges_to_transitions: Iterable[ArgumentEdgeToTransition],
        place_names_to_indices: dict[PlaceNodeName, NodeIndex],
        transition_names_to_indices: dict[TransitionNodeName, NodeIndex],
    ) -> Sequence[tuple[NodeIndex, NodeIndex, ArgumentName]]:
        out = []
        for edge in edges_to_transitions:
            if not isinstance(edge, ArgumentEdgeToTransition):
                raise ValueError("All edges must be of type ArgumentEdgeToTransition.")
            out.append((
                place_names_to_indices[edge.place_node_name],
                transition_names_to_indices[edge.transition_node_name],
                edge.argument,
            ))
        return tuple(out)

    def returned_edges_from_transitions(
        edges_from_transitions: Iterable[ReturnedEdgeFromTransition],
        place_names_to_indices: dict[PlaceNodeName, NodeIndex],
        transition_names_to_indices: dict[TransitionNodeName, NodeIndex],
    ) -> Sequence[tuple[NodeIndex, NodeIndex, ReturnIndex]]:
        out = []
        for edge in edges_from_transitions:
            if not isinstance(edge, ReturnedEdgeFromTransition):
                raise ValueError("All edges must be of type ReturnedEdgeFromTransition.")
            out.append((
                transition_names_to_indices[edge.transition_node_name],
                place_names_to_indices[edge.place_node_name],
                edge.return_index,
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
