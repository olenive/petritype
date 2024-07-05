from typing import Sequence, Union
from rustworkx import NodeIndices, PyDiGraph

from petritype.core.data_structures import (
    FunctionFullName, FunctionWithAnnotations, NodeIndex, TypeName, TypeRelationship, TypeVariableWithAnnotations
)
from petritype.core.relationship_graph_components import (
    FunctionToTypeEdges, TypeToFunctionEdges, TypeToTypeEdges
)
from petritype.core.rustworkx_graph import RustworkxGraph
from petritype.helpers.structures import SafeMerge


type NamesToTypeRelationships = set[tuple[tuple[str, str], TypeRelationship]]
type TypeNamesToNodeIndices = dict[TypeName, int]
type FunctionNamesToNodeIndices = dict[FunctionFullName, int]


class RustworkxToGraphviz:

    # def rustworkx_type_relationship_edges(
    #     node_names_to_type_relationships: NamesToTypeRelationships,
    #     node_names_to_node_indices: dict[Union[TypeName, FunctionFullName], int]
    # ) -> Sequence[tuple[NodeIndex, NodeIndex, TypeRelationship]]:
    #     return [
    #         (
    #             node_names_to_node_indices[name_from],
    #             node_names_to_node_indices[name_to],
    #             relationship
    #         )
    #         for (name_from, name_to), relationship in node_names_to_type_relationships
    #     ]

    def type_relationship_edges(
        function_node_indices: NodeIndex,
        functions: Sequence[FunctionWithAnnotations],
        type_node_indices: NodeIndices,
        types: Sequence[TypeVariableWithAnnotations],
        edges_type_to_type: dict[tuple[tuple[TypeName, TypeName], TypeRelationship]],
        edges_type_to_function: dict[tuple[tuple[TypeName, FunctionFullName], TypeRelationship]],
        edges_function_to_type: dict[tuple[tuple[FunctionFullName, TypeName], TypeRelationship]],
    ) -> tuple[NamesToTypeRelationships, TypeNamesToNodeIndices, FunctionNamesToNodeIndices]:
        function_names_to_node_indices = {
            function.function_full_name: node_index
            for node_index, function in zip(function_node_indices, functions)
        }
        type_names_to_node_indices = {
            type_.name: node_index
            for node_index, type_ in zip(type_node_indices, types)
        }
        names_to_node_indices = SafeMerge.dictionaries(function_names_to_node_indices, type_names_to_node_indices)
        type_to_type_relationship_edges = RustworkxGraph.type_relationship_edges(
            node_names_to_type_relationships=edges_type_to_type,
            node_names_to_node_indices=names_to_node_indices
        )
        type_to_function_relationship_edges = RustworkxGraph.type_relationship_edges(
            node_names_to_type_relationships=edges_type_to_function,
            node_names_to_node_indices=names_to_node_indices
        )
        function_to_type_relationship_edges = RustworkxGraph.type_relationship_edges(
            node_names_to_type_relationships=edges_function_to_type,
            node_names_to_node_indices=names_to_node_indices
        )
        type_relationship_edges = (
            type_to_type_relationship_edges + type_to_function_relationship_edges + function_to_type_relationship_edges
        )
        return type_relationship_edges, type_names_to_node_indices, function_names_to_node_indices

    def digraph(
        *,
        types: Sequence[TypeVariableWithAnnotations],
        functions: Sequence[FunctionWithAnnotations],
        edges_type_to_function: TypeToFunctionEdges,
        edges_function_to_type: FunctionToTypeEdges,
        edges_type_to_type: TypeToTypeEdges,
    ) -> tuple[PyDiGraph, TypeNamesToNodeIndices, FunctionNamesToNodeIndices, NamesToTypeRelationships]:
        graph = PyDiGraph()
        function_node_indices: NodeIndices = graph.add_nodes_from(functions)
        type_node_indices: NodeIndices = graph.add_nodes_from(types)
        type_relationship_edges, type_names_to_node_indices, function_names_to_node_indices = \
            RustworkxToGraphviz.type_relationship_edges(
                function_node_indices=function_node_indices,
                functions=functions,
                type_node_indices=type_node_indices,
                types=types,
                edges_type_to_type=edges_type_to_type,
                edges_type_to_function=edges_type_to_function,
                edges_function_to_type=edges_function_to_type,
            )
        graph.add_edges_from(type_relationship_edges)
        return graph, type_names_to_node_indices, function_names_to_node_indices, type_relationship_edges

    def node_name(node_data: Union[FunctionWithAnnotations, TypeVariableWithAnnotations]) -> str:
        if isinstance(node_data, FunctionWithAnnotations):
            return node_data.function_full_name
        elif isinstance(node_data, TypeVariableWithAnnotations):
            return node_data.name
        else:
            raise ValueError(f"Unexpected node data type: {type(node_data)}")

    def node_attr_fn(node):
        return {
            "label": RustworkxToGraphviz.node_name(node),
            'color': 'lightblue' if isinstance(node, TypeVariableWithAnnotations) else 'lightgreen',
            'style': 'filled'
        }

    def edge_value_to_colour(value: str) -> str:
        value_to_colour = {
            TypeRelationship.CONTAINS_AS_SUBTYPE.value: 'deepskyblue',

            TypeRelationship.PARENT_OF.value: 'grey',
            TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE.value: 'blue',
            TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE.value: 'purple',

            TypeRelationship.RETURN_CONTAINS_THIS_AS_SUBTYPE.value: "orange",
            TypeRelationship.RETURNS_EXACTLY_THIS_TYPE.value: "deeppink",

            TypeRelationship.TAKES_AS_EXACT_TYPE_OF_ARGUMENT.value: "green",
            TypeRelationship.TAKES_AS_SUBTYPE_OF_ARGUMENT.value: "lightgreen",
        }
        return value_to_colour.get(value, 'red')

    def edge_attr_fn(edge):
        return {
            'label': edge.value,
            'color': RustworkxToGraphviz.edge_value_to_colour(edge.value)
        }
