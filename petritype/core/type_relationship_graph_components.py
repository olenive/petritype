from ast import Module
from typing import Callable, Iterable, Optional, Sequence
from pydantic import BaseModel
from rustworkx import PyDiGraph

from petritype.core.ast_extraction import ASTClass, ASTModule
from petritype.core.data_structures import (
    FunctionWithAnnotations, NodeIndex, NodeIndicesFromTo, TypeName, TypeRelationship, TypeVariableWithAnnotations
)


"""Relationship Graph Components"""


class RelationshipGraphTypeNodeData(BaseModel):
    name: str
    type_with_annotations: TypeVariableWithAnnotations


class RelationshipGraphFunctionNodeData(BaseModel):
    name: str
    function_with_annotations: FunctionWithAnnotations


type RelationshipGraphTypeNodes = dict[NodeIndex, RelationshipGraphTypeNodeData]
type RelationshipFunctionNodes = dict[NodeIndex, RelationshipGraphFunctionNodeData]
type RelationshipGraphEdges = Sequence[tuple[NodeIndicesFromTo, TypeRelationship]]


class RelationshipGraphComponent:

    def type_nodes_data(
        module_tree: Module, excluded_types: set[TypeName]
    ) -> Sequence[RelationshipGraphTypeNodeData]:
        relevant_nodes = ASTModule.relevant_types(module_tree)
        return tuple(
            RelationshipGraphTypeNodeData(
                name=name,
                type_relationships=ASTClass.type_relationships(node, excluded_types)
            ) for name, node in relevant_nodes.items()
        )

    def switch_child_to_parent_edge_direction(edges: RelationshipGraphEdges) -> RelationshipGraphEdges:
        """Swap the node indices of an edge and relabel child-to-parent relationships as parent-to-child relationships.

        When parsing code it is easier to determine the parent of a class rather than finding all its children.
        However, plots feel more intuitive if arrows point from the parent to the child.
        """
        out = []
        for (i, j, relationship) in edges:
            if relationship == TypeRelationship.CHILD_OF:
                out.append((j, i, TypeRelationship.PARENT_OF))
            else:
                out.append((i, j, relationship))
        return out

    def switch_contains_to_contained_in_edge_direction(edges: RelationshipGraphEdges) -> RelationshipGraphEdges:
        """Swap the node indices of an edge and relabel contains relationships as contained_in relationships.

        When parsing types it is easier to figure out what other types are contained in a type rather than what types
        contain a type. However, plots feel more intuitive if arrows point from the contained to the container types.
        """
        out = []
        for (i, j, relationship) in edges:
            if relationship == TypeRelationship.CONTAINS_AS_ATTRIBUTE_TYPE:
                out.append((j, i, TypeRelationship.CONTAINED_IN_ATTRIBUTE_TYPE))
            elif relationship == TypeRelationship.CONTAINS_AS_ATTRIBUTE_SUBTYPE:
                out.append((j, i, TypeRelationship.CONTAINED_IN_ATTRIBUTE_SUBTYPE))
            else:
                out.append((i, j, relationship))
        return out

    def switch_relationship_edge_directions_between_types(edges: RelationshipGraphEdges) -> RelationshipGraphEdges:
        return RelationshipGraphComponent.switch_contains_to_contained_in_edge_direction(
            RelationshipGraphComponent.switch_child_to_parent_edge_direction(edges)
        )

    def function_node_data(function_with_annotations: FunctionWithAnnotations) -> RelationshipGraphFunctionNodeData:
        node_name = function_with_annotations.class_name + "." if function_with_annotations.class_name else ""
        node_name += function_with_annotations.function_name
        return RelationshipGraphFunctionNodeData(
            name=node_name,
            function_with_annotations=function_with_annotations
        )

    def function_nodes_data(
        functions_with_annotations: Iterable[FunctionWithAnnotations]
    ) -> Sequence[RelationshipGraphFunctionNodeData]:
        return [RelationshipGraphComponent.function_node_data(f) for f in functions_with_annotations]

    def edges_between_types(
        type_nodes: RelationshipGraphTypeNodes,
        type_to_type_matching_function: Callable[[TypeName], Optional[TypeName]]
    ) -> RelationshipGraphEdges:
        out = []
        for i, type_node in type_nodes.items():
            for variable_type_relationship in type_node.type_relationships:
                j = type_to_type_matching_function(variable_type_relationship.type_annotation)
                if j is not None:
                    out.append(
                        (i, j, variable_type_relationship.relationship)
                    )
        return out

    # def edges_between_functions_and_types(
    #     function_nodes: RelationshipFunctionNodes,
    #     matching_function: Callable[[TypeName], Optional[TypeName]],
    # ) -> RelationshipGraphEdges:
    #     out = []
    #     for i, data in function_nodes.items():
    #         # Input argument type relationships.
    #         for type_annotation in data.function_with_annotations.argument_types.values():
    #             if type_annotation is not None:
    #                 j = matching_function(type_annotation)
    #                 if j is not None:
    #                     out.append(
    #                         (j, i, TypeRelationship.ARGUMENT_TO)
    #                     )
    #         # Return type relationship.
    #         return_type = data.function_with_annotations.return_type
    #         if return_type:
    #             j = matching_function(return_type)
    #             if j is not None:
    #                 out.append(
    #                     (i, j, TypeRelationship.RETURNS)
    #                 )
    #     return out

    def assemble(
        *,
        type_nodes_data: Sequence[RelationshipGraphTypeNodeData],
        function_nodes_data: Sequence[RelationshipGraphFunctionNodeData],
        matching_function_maker: Callable[[dict[TypeName, NodeIndex]], Callable[[TypeName], Optional[TypeName]]],
    ) -> PyDiGraph:
        """We need to know the node indices before we can create edges.

        The matching_function_maker is used to determine the rules for when an edge should be created between two nodes.
        The dictionary needed to create the matching_function only exists once nodes are created - this is why the
        matching_function_maker higher order function is used to create the matching_function.
        """

        g = PyDiGraph()
        type_node_indices = g.add_nodes_from(type_nodes_data)
        type_nodes: RelationshipGraphTypeNodes = {
            index: data for index, data in zip(type_node_indices, type_nodes_data)
        }
        matching_function = matching_function_maker({data.name: i for i, data in type_nodes.items()})
        function_node_indices = g.add_nodes_from(function_nodes_data)
        function_nodes: RelationshipFunctionNodes = {
            index: data for index, data in zip(function_node_indices, function_nodes_data)
        }
        edges_between_types = RelationshipGraphComponent.switch_relationship_edge_directions_between_types(
            RelationshipGraphComponent.edges_between_types(type_nodes, matching_function=matching_function)
        )
        edges_between_functions_and_types = RelationshipGraphComponent.edges_between_functions_and_types(
            function_nodes=function_nodes, matching_function=matching_function
        )
        g.add_edges_from(edges_between_types)
        g.add_edges_from(edges_between_functions_and_types)
        return g
