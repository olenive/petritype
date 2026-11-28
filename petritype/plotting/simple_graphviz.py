"""
Simple Graphviz Visualization used in examples.
"""

from petritype._optional import require

require("rustworkx", "viz")

from petritype.core.executable_graph_components import (
    ListPlaceNode, FunctionTransitionNode, ExecutableGraph, ExecutableGraphOperations, SubnetPlaceNode,
)
from petritype.core.type_names import TypeNames
from petritype.plotting.rustworkx_graph import RustworkxGraph
from petritype.plotting.rustworkx_to_graphviz import NodeLabel, RustworkxToGraphviz


class SimpleGraphvizVisualization:

    @staticmethod
    def place_node_label(node: ListPlaceNode) -> str:
        label = f"{node.name}\n({TypeNames.of(node.type)})"
        value_strings = [str(x) for x in node.tokens]
        tokens_string = "\n".join(value_strings)
        return NodeLabel.escape_quoted(f"{label}\n{tokens_string}")


    @staticmethod
    def transition_node_label(node: FunctionTransitionNode) -> str:
        return NodeLabel.escape_quoted(f"{node.name}\n({TypeNames.callable_name(node.function)})")


    @staticmethod
    def flow_node_attr_fn(node, open: bool = False):
        """A node's Graphviz attributes. A port, a place its net marks ``port=True``, has
        ``dotted`` added to its style (``NodeLabel.add_style``), drawn ``"filled,dotted"``
        with one ring (since 2026-11-28, where it had a second ring). ``open`` says the
        node's net is declared open (since 2026-11-28): a place of it that is no port
        then has ``dashed`` added, drawn ``"filled,dashed"``, while a port stays dotted
        and not dashed; a subnet place's box and a transition are drawn as for a closed
        net."""
        if isinstance(node, ListPlaceNode):
            attrs = {
                "label": SimpleGraphvizVisualization.place_node_label(node),
                'color': 'deepskyblue',
                'style': 'filled',
                'shape': 'oval'
            }
            if node.port:
                NodeLabel.add_style(attrs, 'dotted')  # a port of this net, dotted in an open net too
            elif open:
                NodeLabel.add_style(attrs, 'dashed')  # an open net: written from outside at any place
            return attrs
        elif isinstance(node, SubnetPlaceNode):
            # Closed: a box with rounded corners, a cell per port (NodeLabel.subnet_place).
            return {
                "label": NodeLabel.subnet_place(node),
                'color': 'lightskyblue',
                'style': 'filled',
                'shape': 'Mrecord'
            }
        elif isinstance(node, FunctionTransitionNode):
            return {
                "label": SimpleGraphvizVisualization.transition_node_label(node),
                'color': 'lightgreen',
                'style': 'filled',
                'shape': 'box'
            }
        else:
            raise ValueError("Invalid node data type.")

    @staticmethod
    def graph(executable_pydigraph, method: str = 'dot'):
        """The drawing of a net's rustworkx graph; an edge on a port of a subnet
        place ends at the port's cell, each group is a dotted frame titled with its
        name (``RustworkxToGraphviz.draw``), a port is dotted, and the places of a
        net declared open that are no ports are dashed (``RustworkxGraph.is_open``)."""
        cells = NodeLabel.port_cells(executable_pydigraph.nodes())
        open_net = RustworkxGraph.is_open(executable_pydigraph)
        return RustworkxToGraphviz.draw(
            executable_pydigraph,
            node_attr_fn=lambda node: SimpleGraphvizVisualization.flow_node_attr_fn(node, open=open_net),
            edge_attr_fn=lambda edge: NodeLabel.edge_port(edge, cells),
            method=method,
        )

    @staticmethod
    async def animate_execution_generator(
        executable_graph: ExecutableGraph,
        executable_pydigraph,
        max_iterations: int = 100,
        max_transitions_per_step: int = 1,
        verbose: bool = True,
        method: str = 'dot'
    ):
        """
        Generator that executes an executable graph step by step and yields diagrams.

        This is an async generator that yields tuples of (step_number, diagram, transitions_fired)
        for use in Jupyter notebooks with display() and clear_output().

        Example usage in Jupyter:
            async for step, diagram, fired in SimpleGraphvizVisualization.animate_execution_generator(...):
                clear_output(wait=True)
                print(f"Step {step}")
                display(diagram)
                print(f"Transitions fired: {fired}")
                if not fired:
                    break
                await asyncio.sleep(1.0)
        """
        for i in range(max_iterations):
            _, transitions_fired = await ExecutableGraphOperations.execute_graph(
                executable_graph=executable_graph,
                stop_after_n_firings=max_transitions_per_step,
                verbose=verbose,
            )

            node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(
                executable_graph
            )
            diagram = RustworkxToGraphviz.draw(
                executable_pydigraph,
                node_attr_fn=node_attr_fn,
                edge_attr_fn=edge_attr_fn,
                method=method,
            )

            yield (i, diagram, transitions_fired)

            if not transitions_fired:
                break
