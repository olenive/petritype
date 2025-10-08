"""
Simple Graphviz Visualization used in examples.
"""

import time
import matplotlib.pyplot as plt
from rustworkx.visualization import graphviz_draw

from petritype.core.executable_graph_components import (
    ListPlaceNode, FunctionTransitionNode, ArgumentEdgeToTransition, ReturnedEdgeFromTransition,
    ExecutableGraph, ExecutableGraphOperations
)
from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz


class SimpleGraphvizVisualization:

    def place_node_label(node: ListPlaceNode) -> str:
        label = f"{node.name}\n({node.type.__name__})"
        value_strings = [str(x) for x in node.tokens]
        tokens_string = "\n".join(value_strings)
        return f"{label}\n{tokens_string}"


    def transition_node_label(node: FunctionTransitionNode) -> str:
        return f"{node.name}\n({node.function.__qualname__})"


    def flow_node_attr_fn(node):
        if isinstance(node, ListPlaceNode):
            return {
                "label": SimpleGraphvizVisualization.place_node_label(node),
                'color': 'deepskyblue',
                'style': 'filled',
                'shape': 'oval'
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
        return graphviz_draw(
            executable_pydigraph,
            node_attr_fn=SimpleGraphvizVisualization.flow_node_attr_fn,
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
                time.sleep(1.0)
        """
        for i in range(max_iterations):
            _, transitions_fired = await ExecutableGraphOperations.execute_graph(
                executable_graph=executable_graph,
                max_transitions=max_transitions_per_step,
                verbose=verbose,
            )
            
            node_attr_fn, edge_attr_fn = RustworkxToGraphviz.activation_coloured_attr_functions(
                executable_graph
            )
            diagram = graphviz_draw(
                executable_pydigraph,
                node_attr_fn=node_attr_fn,
                edge_attr_fn=edge_attr_fn,
                method=method,
            )
            
            yield (i, diagram, transitions_fired)
            
            if not transitions_fired:
                break
