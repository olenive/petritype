from typing import Callable, Sequence, Union
from rustworkx import NodeIndices, PyDiGraph

from petritype.core.data_structures import (
    FunctionFullName, FunctionWithAnnotations, NodeIndex, TypeName, TypeRelationship, TypeVariableWithAnnotations
)
from petritype.core.executable_graph_components import ExecutableGraph, FunctionTransitionNode, ListPlaceNode
from petritype.core.relationship_graph_components import (
    FunctionToTypeEdges, TypeToFunctionEdges, TypeToTypeEdges
)
from petritype.core.rustworkx_graph import RustworkxArgumentEdgeData, RustworkxGraph, RustworkxReturnedEdgeData
from petritype.helpers.structures import SafeMerge


type NamesToTypeRelationships = set[tuple[tuple[str, str], TypeRelationship]]
type TypeNamesToNodeIndices = dict[TypeName, int]
type FunctionNamesToNodeIndices = dict[FunctionFullName, int]


class NodeLabel:

    def default_token_summariser(token: any, maximum_length=100) -> str:
        token_info = str(token)
        if len(token_info) > maximum_length:
            token_info = token_info[:maximum_length] + "..."
        return token_info

    def default_place(node: ListPlaceNode, token_summariser=default_token_summariser) -> str:
        label = f"{node.name}\n({node.type.__name__})"
        value_strings = [token_summariser(x) for x in node.tokens]
        tokens_string = "\n".join(value_strings)
        return f"{label}\n{tokens_string}"

    def default_transition(node: FunctionTransitionNode) -> str:
        return f"{node.name}\n({node.function.__qualname__})"


class RustworkxToGraphviz:

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

    def activation_coloured_attr_functions(
        graph: ExecutableGraph,
        token_summariser=NodeLabel.default_token_summariser,
    ) -> tuple[
        Callable[[Union[ListPlaceNode, FunctionTransitionNode]], dict[str, Union[str, int, float, bool]]],
        Callable[[str], dict[str, Union[str, int, float, bool]]]
    ]:
        """Generate a function that returns node attributes for the flow graph.

        This is used to colour nodes depending on recent graph activation history stored in the ExecutableGraph.
        """

        # Extract the last entry from each kind of history
        last_transition = graph.transition_history[-1] if graph.transition_history else None
        last_input_place = graph.input_place_history[-1] if graph.input_place_history else None
        last_output_place = graph.output_place_history[-1] if graph.output_place_history else None

        # Create a set of node names to be outlined in magenta
        magenta_outlined_node_names = set()
        if last_transition:
            magenta_outlined_node_names.add(last_transition.name)
        if last_input_place:
            magenta_outlined_node_names = magenta_outlined_node_names.union({x.name for x in last_input_place})
        if last_output_place:
            magenta_outlined_node_names = magenta_outlined_node_names.union({x.name for x in last_output_place})

        def node_attr_fn(node):
            if isinstance(node, ListPlaceNode):
                attrs = {
                    "label": NodeLabel.default_place(node, token_summariser),
                    'fillcolor': 'deepskyblue',
                    'style': 'filled',
                    'shape': 'oval',
                    'color': 'black',  # default outline color
                    'penwidth': '1'
                }
                if node.name in magenta_outlined_node_names:
                    attrs['color'] = 'magenta'
                    attrs['penwidth'] = '2'
                return attrs
            elif isinstance(node, FunctionTransitionNode):
                attrs = {
                    "label": NodeLabel.default_transition(node),
                    'fillcolor': 'lightgreen',
                    'style': 'filled',
                    'shape': 'box',
                    'color': 'black',  # default outline color
                    'penwidth': '1'
                }
                if node.name in magenta_outlined_node_names:
                    attrs['color'] = 'magenta'
                    attrs['penwidth'] = '3'
                return attrs
            else:
                raise ValueError("Invalid node data type.")

        def edge_attr_fn(edge: str) -> dict[str, Union[str, int, float, bool]]:

            def should_be_magenta(edge_data: RustworkxArgumentEdgeData | RustworkxReturnedEdgeData) -> bool:
                if isinstance(edge_data, RustworkxArgumentEdgeData):
                    return (
                        edge_data.target_transition_node_name in magenta_outlined_node_names
                        and edge_data.source_place_node_name in magenta_outlined_node_names
                    )
                elif isinstance(edge_data, RustworkxReturnedEdgeData):
                    return (
                        edge_data.source_transition_node_name in magenta_outlined_node_names
                        and edge_data.target_place_node_name in magenta_outlined_node_names
                    )
                else:
                    raise ValueError(f"Invalid edge data type: {type(edge_data)}")

            if should_be_magenta(edge):
                return {'color': 'magenta', 'penwidth': '2'}
            else:
                return {'color': 'black', 'penwidth': '1'}

        return node_attr_fn, edge_attr_fn
