"""
Simple Graphviz Visualization used in examples.
"""

from petritype._optional import require

require("rustworkx", "viz")

from petritype.core.executable_graph_components import (
    ListPlaceNode, FunctionTransitionNode, ExecutableGraph, ExecutableGraphOperations, SubnetPlaceNode,
)
from petritype.core.type_names import TypeNames
from petritype.plotting.rustworkx_to_graphviz import NodeLabel, RustworkxToGraphviz


class SimpleGraphvizVisualization:

    def place_node_label(node: ListPlaceNode) -> str:
        label = f"{node.name}\n({TypeNames.of(node.type)})"
        value_strings = [str(x) for x in node.tokens]
        tokens_string = "\n".join(value_strings)
        return NodeLabel.escape_quoted(f"{label}\n{tokens_string}")


    def transition_node_label(node: FunctionTransitionNode) -> str:
        return NodeLabel.escape_quoted(f"{node.name}\n({TypeNames.callable_name(node.function)})")


    def flow_node_attr_fn(node):
        if isinstance(node, ListPlaceNode):
            attrs = {
                "label": SimpleGraphvizVisualization.place_node_label(node),
                'color': 'deepskyblue',
                'style': 'filled',
                'shape': 'oval'
            }
            if node.port:
                attrs['peripheries'] = '2'  # a port of this net: the border, seen from inside
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

    def graph(executable_pydigraph, method: str = 'dot'):
        """The drawing of a net's rustworkx graph; an edge on a port of a subnet
        place ends at the port's cell, and each group is a dashed frame titled
        with its name (``RustworkxToGraphviz.draw``)."""
        cells = NodeLabel.port_cells(executable_pydigraph.nodes())
        return RustworkxToGraphviz.draw(
            executable_pydigraph,
            node_attr_fn=SimpleGraphvizVisualization.flow_node_attr_fn,
            edge_attr_fn=lambda edge: NodeLabel.edge_port(edge, cells),
            method=method,
        )

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
