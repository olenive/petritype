from typing import Any, Callable, Optional, Sequence, Union
from petritype._optional import require

require("rustworkx", "viz")

from rustworkx import NodeIndices, PyDiGraph

from petritype.core.data_structures import (
    FunctionFullName, FunctionWithAnnotations, NodeIndex, TypeName, TypeRelationship, TypeVariableWithAnnotations
)
from petritype.core.events import PlaceKey, Port
from petritype.core.executable_graph_components import (
    ExecutableGraph, FunctionTransitionNode, ListPlaceNode, MapPlaceNames, NestedNets, SubnetPlaceNode,
)
from petritype.core.keys import Filter
from petritype.core.relationship_graph_components import (
    FunctionToTypeEdges, TypeToFunctionEdges, TypeToTypeEdges
)
from petritype.plotting.rustworkx_graph import (
    RustworkxArgumentEdgeData, RustworkxGraph, RustworkxReadEdgeData, RustworkxReturnedEdgeData
)
from petritype.helpers.structures import SafeMerge


type NamesToTypeRelationships = set[tuple[tuple[str, str], TypeRelationship]]
type TypeNamesToNodeIndices = dict[TypeName, int]
type FunctionNamesToNodeIndices = dict[FunctionFullName, int]


class NodeLabel:

    def default_token_summariser(token: any, maximum_length=100) -> str:
        """One line per token in a place's label. A token that holds nets is
        marked as such, with each inner net's name and token count in place of
        the net itself (``NestedNets.summary``); any other token is ``str``, cut."""
        if NestedNets.holds_nets(token):
            return NestedNets.summary(token, maximum_length)
        token_info = str(token)
        if len(token_info) > maximum_length:
            token_info = token_info[:maximum_length] + "..."
        return token_info

    def inner_nets(token: any) -> dict[str, ExecutableGraph]:
        """The nets a token holds, by name, for a descend view: each is an
        ``ExecutableGraph`` that ``RustworkxGraph.from_executable_graph`` renders
        like any other net."""
        return NestedNets.of(token)

    def default_place(
        node: ListPlaceNode, token_summariser=default_token_summariser,
        filter: Optional[Filter] = None, params: Optional[dict[str, Any]] = None,
    ) -> str:
        """A place's label: its name and type, then one line per token. With a
        ``filter`` (``petritype.core.keys``) only the tokens it keeps are listed,
        the header reads ``matching / count`` and a last line says how many the
        filter hides, so a hidden token is never mistaken for an absent one;
        ``params`` are the net's, for a clause that names one."""
        if filter is None:
            label = f"{node.name}\n({node.type.__name__})"
            value_strings = [token_summariser(x) for x in node.tokens]
        else:
            kept = [x for x in node.tokens if filter.keeps(node, x, params or {})]
            label = f"{node.name}\n({node.type.__name__}) {len(kept)} / {len(node.tokens)}"
            value_strings = [token_summariser(x) for x in kept]
            hidden = len(node.tokens) - len(kept)
            if hidden:
                value_strings.append(f"({hidden} hidden by filter)")
        tokens_string = "\n".join(value_strings)
        return f"{label}\n{tokens_string}"

    def default_transition(node: FunctionTransitionNode) -> str:
        return f"{node.name}\n({node.function.__qualname__})"

    def record_text(text: str) -> str:
        """Text for one field of a Graphviz record label: the characters a record
        or a quoted string gives a meaning (``\\ { } | < > "``) escaped with a
        backslash, and each line break written as the record's ``\\n``, so a name
        is drawn as it is, whatever characters it holds."""
        escaped = "".join("\\" + character if character in '\\{}|<>"' else character for character in text)
        return escaped.replace("\n", "\\n")

    def port_cells(places) -> dict[Port, str]:
        """For every port of every subnet place among ``places``, the record field
        its cell is drawn as, by position (``port0``, ``port1``, ...), so no
        character of a port's name reaches the drawing's syntax."""
        return {
            Port(place.name, port.name): f"port{index}"
            for place in places if isinstance(place, SubnetPlaceNode)
            for index, port in enumerate(place.ports)
        }

    def subnet_place(node: SubnetPlaceNode) -> str:
        """A closed subnet place's record label, for ``shape=Mrecord`` (a box with
        rounded corners): its name as the title, the template that built its
        subnet below it when there is one, as a place shows its type, then one cell
        per port, in the subnet's order, with the port's name and its count. An
        edge ends at a port's cell through the cell's field (``port_cells``)."""
        title = node.name if node.graph.template is None else f"{node.name}\n({node.graph.template})"
        cells = "|".join(
            f"<port{index}> " + NodeLabel.record_text(port.name + "\n" + str(len(port.tokens)))
            for index, port in enumerate(node.ports)
        )
        if not cells:
            return "{" + NodeLabel.record_text(title) + "}"
        return "{" + NodeLabel.record_text(title) + "|{" + cells + "}}"

    def edge_port(edge: Any, cells: dict[Port, str]) -> dict[str, str]:
        """The attribute that ends an edge on a port at the port's cell: the edge's
        tail for an input or read edge, its head for an output edge; nothing for
        an edge on a place of the net's own."""
        if isinstance(edge, RustworkxReturnedEdgeData):
            cell = cells.get(edge.target_place_node_name)
            return {"headport": cell} if cell is not None else {}
        cell = cells.get(getattr(edge, "source_place_node_name", None))
        return {"tailport": cell} if cell is not None else {}


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
        filter: Optional[Filter] = None,
    ) -> tuple[
        Callable[[Union[ListPlaceNode, FunctionTransitionNode]], dict[str, Union[str, int, float, bool]]],
        Callable[[str], dict[str, Union[str, int, float, bool]]]
    ]:
        """Generate a function that returns node attributes for the flow graph.

        This is used to colour nodes depending on recent graph activation history stored in the ExecutableGraph.
        With a ``filter`` every place lists only the tokens it keeps and says how many it hides
        (``NodeLabel.default_place``).

        A subnet place is drawn closed, as one box with rounded corners holding a cell per
        port with the port's name and count (``NodeLabel.subnet_place``); an edge on a port
        ends at the port's cell, and the box is outlined when the last firing touched one of
        its ports. Filters apply per net, so the cells count every token: a port is filtered
        in its subnet's own drawing. There, in the drawing of the subnet, a port place has a
        second ring, so that the border shows from inside.
        """

        # Extract the last entry from each kind of history
        last_transition = graph.transition_history[-1] if graph.transition_history else None
        last_output_place = graph.output_place_history[-1] if graph.output_place_history else None

        # The places the last firing touched, by the keys its edges name them by (a port by
        # its pair), and the transition itself, to be outlined in magenta. Every argument
        # edge took from its place (the input history holds one copy per argument edge),
        # every read edge read its place, and the output history holds the very places the
        # firing deposited into, matched here by identity to their keys.
        magenta_outlined_places: set[PlaceKey] = set()
        magenta_outlined_transition = last_transition.name if last_transition else None
        if last_transition:
            for edge in (*graph.argument_edges, *graph.snapshot_edges, *graph.mutate_edges):
                if edge.transition_node_name == last_transition.name:
                    magenta_outlined_places.add(edge.place_node_name)
        if last_output_place:
            deposited = {id(place) for place in last_output_place}
            magenta_outlined_places |= {
                key for key, place in MapPlaceNames.to_list_place_nodes(graph).items() if id(place) in deposited
            }
        cells = NodeLabel.port_cells(graph.places)

        def node_attr_fn(node):
            if isinstance(node, ListPlaceNode):
                attrs = {
                    "label": NodeLabel.default_place(node, token_summariser, filter, graph.params),
                    'fillcolor': 'deepskyblue',
                    'style': 'filled',
                    'shape': 'oval',
                    'color': 'black',  # default outline color
                    'penwidth': '1'
                }
                if node.port:
                    attrs['peripheries'] = '2'  # a port of this net: the border, seen from inside
                if node.name in magenta_outlined_places:
                    attrs['color'] = 'magenta'
                    attrs['penwidth'] = '2'
                return attrs
            elif isinstance(node, SubnetPlaceNode):
                attrs = {
                    "label": NodeLabel.subnet_place(node),
                    'fillcolor': 'lightskyblue',
                    'style': 'filled',
                    'shape': 'Mrecord',
                    'color': 'black',
                    'penwidth': '1'
                }
                if any(Port(node.name, port.name) in magenta_outlined_places for port in node.ports):
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
                if node.name in graph.in_flight:
                    # Body is executing right now (concurrent mode) — show it "working".
                    attrs['fillcolor'] = 'gold'
                    attrs['color'] = 'darkorange'
                    attrs['penwidth'] = '2'
                elif node.name == magenta_outlined_transition:
                    attrs['color'] = 'magenta'
                    attrs['penwidth'] = '3'
                return attrs
            else:
                raise ValueError("Invalid node data type.")

        def edge_attr_fn(edge: str) -> dict[str, Union[str, int, float, bool]]:

            def should_be_magenta(edge_data) -> bool:
                if isinstance(edge_data, RustworkxArgumentEdgeData):
                    return (
                        edge_data.target_transition_node_name == magenta_outlined_transition
                        and edge_data.source_place_node_name in magenta_outlined_places
                    )
                elif isinstance(edge_data, RustworkxReturnedEdgeData):
                    return (
                        edge_data.source_transition_node_name == magenta_outlined_transition
                        and edge_data.target_place_node_name in magenta_outlined_places
                    )
                elif isinstance(edge_data, RustworkxReadEdgeData):
                    return (
                        edge_data.target_transition_node_name == magenta_outlined_transition
                        and edge_data.source_place_node_name in magenta_outlined_places
                    )
                else:
                    raise ValueError(f"Invalid edge data type: {type(edge_data)}")

            # An edge on a port ends at the port's cell of its subnet place's box.
            port = NodeLabel.edge_port(edge, cells)
            if isinstance(edge, RustworkxReadEdgeData):
                # Read edges keep their distinct shape but recolour magenta when their transition
                # fires, like the consuming / producing edges. Resting colour is the same dark grey.
                colour = 'magenta' if should_be_magenta(edge) else 'dimgray'
                if edge.mutate:
                    # MutateEdge: bidirectional, dot ends, slightly thicker — reads AND writes.
                    return {
                        'dir': 'both',
                        'arrowhead': 'dot',
                        'arrowtail': 'dot',
                        'color': colour,
                        'penwidth': '2',
                        **port,
                    }
                # SnapshotEdge: a dashed arrow towards the transition — a non-consuming read.
                # SurveyEdge: the same with a hollow head — it reads an empty place too.
                return {
                    'style': 'dashed',
                    'color': colour,
                    'arrowhead': 'empty' if edge.survey else 'normal',
                    'penwidth': '1',
                    **port,
                }

            if should_be_magenta(edge):
                attrs = {'color': 'magenta', 'penwidth': '2'}
            else:
                attrs = {'color': 'black', 'penwidth': '1'}
            if isinstance(edge, RustworkxArgumentEdgeData) and edge.drain:
                # DrainEdge: a consuming arrow with a hollow head — it takes from an empty place too.
                attrs['arrowhead'] = 'empty'
            attrs.update(port)
            return attrs

        return node_attr_fn, edge_attr_fn
