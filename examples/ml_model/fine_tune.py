import marimo

__generated_with = "0.23.9"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        r"""
        # ML model fine-tuning pipeline

        A Petri net describing a vision-model fine-tuning process: split & augment the data,
        fine-tune, evaluate on validation data, optionally select another model and retry,
        then evaluate on the test data, package, and run integration tests.

        The graph is rendered statically (this notebook builds the structure rather than
        executing it). The types and functions are pulled from the domain module
        `hypothetical_training_steps.py`.
        """
    )
    return


@app.cell
def _(mo):
    from pathlib import Path

    def _find_repo_root() -> Path:
        # marimo puts the notebook's own dir on sys.path/CWD, not the repo root, so anchor
        # file reads explicitly by walking up to the folder containing pyproject.toml.
        here = Path(mo.notebook_dir())
        for parent in [here, *here.parents]:
            if (parent / "pyproject.toml").exists():
                return parent
        raise RuntimeError("repo root (pyproject.toml) not found")

    REPO_ROOT = _find_repo_root()
    return (REPO_ROOT,)


@app.cell
def _():
    import io
    from typing import Sequence

    from rustworkx.visualization import graphviz_draw

    from petritype.core.ast_extraction import FunctionWithAnnotations
    from petritype.core.data_structures import TypeVariableWithAnnotations
    from petritype.core.parse_modules import ExtractFunctions, ExtractTypes, ParseModule
    from petritype.core.relationship_graph_components import (
        FunctionToTypeEdges,
        RelationshipEdges,
        TypeToFunctionEdges,
        TypeToTypeEdges,
    )
    from petritype.core.executable_graph_components import (
        ArgumentEdgeToTransition,
        ExecutableGraphOperations,
        FunctionTransitionNode,
        ListPlaceNode,
        ReturnedEdgeFromTransition,
    )
    from petritype.core.rustworkx_graph import RustworkxGraph
    from petritype.plotting.rustworkx_to_graphviz import RustworkxToGraphviz

    # Domain types & functions, imported by BARE module name — the sibling file is on
    # marimo's sys.path. (Original used `from examples.ml_model.hypothetical_training_steps import *`.)
    from hypothetical_training_steps import (
        AugmentedTrainingData,
        AvailableData,
        DeploymentModel,
        EvaluationData,
        EvaluationMetrics,
        FineTunedVisionModel,
        TestData,
        TrainingData,
        VisionModel,
        data_augmentation,
        evaluate_model,
        fine_tune,
        integration_testing,
        package_model,
        select_another_model,
        train_validation_test_split,
    )

    return (
        ArgumentEdgeToTransition,
        AugmentedTrainingData,
        AvailableData,
        DeploymentModel,
        EvaluationData,
        EvaluationMetrics,
        ExecutableGraphOperations,
        ExtractFunctions,
        ExtractTypes,
        FineTunedVisionModel,
        FunctionToTypeEdges,
        FunctionTransitionNode,
        FunctionWithAnnotations,
        ListPlaceNode,
        ParseModule,
        RelationshipEdges,
        ReturnedEdgeFromTransition,
        RustworkxGraph,
        RustworkxToGraphviz,
        Sequence,
        TestData,
        TrainingData,
        TypeToFunctionEdges,
        TypeToTypeEdges,
        TypeVariableWithAnnotations,
        VisionModel,
        data_augmentation,
        evaluate_model,
        fine_tune,
        graphviz_draw,
        integration_testing,
        io,
        package_model,
        select_another_model,
        train_validation_test_split,
    )


@app.cell
def _(io, mo):
    def half_image(pil_image):
        """Render a PIL image at ~three-quarters its native width so it fits on screen."""
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        return mo.image(src=buffer.getvalue(), width=pil_image.width * 3 // 4)

    return (half_image,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        r"""
        ## Relationship graph extraction

        Parse the domain module and extract its types and functions via AST, then build the
        type/function relationship edges. (`RELEVANT_CLASSES` is empty here, so only the
        module-level types are extracted — the relationship graph is set up but not drawn.)
        """
    )
    return


@app.cell
def _(
    ExtractFunctions,
    ExtractTypes,
    FunctionToTypeEdges,
    FunctionWithAnnotations,
    ParseModule,
    REPO_ROOT,
    RelationshipEdges,
    Sequence,
    TypeToFunctionEdges,
    TypeToTypeEdges,
    TypeVariableWithAnnotations,
):
    _path_components = ("examples", "ml_model", "hypothetical_training_steps.py")
    _relevant_classes = tuple()

    module_from_py_file = ParseModule.from_file(
        path_to_file=str(REPO_ROOT / "examples" / "ml_model" / "hypothetical_training_steps.py"),
        import_path_components=_path_components,
    )
    functions: Sequence[FunctionWithAnnotations] = ExtractFunctions.from_selected_classes_in_parsed_modules(
        parsed_modules=(module_from_py_file,),
        selected_classes=_relevant_classes,
    )
    types: Sequence[TypeVariableWithAnnotations] = ExtractTypes.from_parsed_modules(
        parsed_modules=(module_from_py_file,),
    )
    edges_type_to_type: TypeToTypeEdges = RelationshipEdges.type_to_type(types)
    edges_type_to_function: TypeToFunctionEdges = RelationshipEdges.type_to_function(types, functions)
    edges_function_to_type: FunctionToTypeEdges = RelationshipEdges.function_to_type(functions, types)
    return (
        edges_function_to_type,
        edges_type_to_function,
        edges_type_to_type,
        functions,
        module_from_py_file,
        types,
    )


@app.cell
def _(
    RustworkxToGraphviz,
    edges_function_to_type,
    edges_type_to_function,
    edges_type_to_type,
    functions,
    types,
):
    (
        relationship_graph,
        type_names_to_node_indices,
        function_names_to_node_indices,
        type_relationship_edges,
    ) = RustworkxToGraphviz.digraph(
        types=types,
        functions=functions,
        edges_type_to_function=edges_type_to_function,
        edges_function_to_type=edges_function_to_type,
        edges_type_to_type=edges_type_to_type,
    )
    return (
        function_names_to_node_indices,
        relationship_graph,
        type_names_to_node_indices,
        type_relationship_edges,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        r"""
        ## Executable fine-tuning graph

        The Petri net for the fine-tuning process. Places (blue ovals) hold typed tokens;
        transitions (green boxes) are the pipeline steps. The "Select Another Model or Accept
        Current Model" transition reviews the fine-tuned model and its validation metrics, and
        either proposes another `VisionModel` to try (looping back) or accepts the current
        `FineTunedVisionModel`.
        """
    )
    return


@app.cell
def _(
    ArgumentEdgeToTransition,
    AugmentedTrainingData,
    AvailableData,
    DeploymentModel,
    EvaluationData,
    EvaluationMetrics,
    ExecutableGraphOperations,
    FineTunedVisionModel,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
    TestData,
    TrainingData,
    VisionModel,
    data_augmentation,
    evaluate_model,
    fine_tune,
    integration_testing,
    package_model,
    select_another_model,
    train_validation_test_split,
):
    fine_tuning_nodes_and_edges = (
        # Split and augment data
        ListPlaceNode(name="Available Data", type=AvailableData),
        ArgumentEdgeToTransition("Available Data", "Train/Validation/Test Split", "data"),
        FunctionTransitionNode(name="Train/Validation/Test Split", function=train_validation_test_split),
        ReturnedEdgeFromTransition("Train/Validation/Test Split", "Training Data"),
        ReturnedEdgeFromTransition("Train/Validation/Test Split", "Validation Data"),
        ReturnedEdgeFromTransition("Train/Validation/Test Split", "Test Data"),
        ListPlaceNode(name="Training Data", type=TrainingData),
        ListPlaceNode(name="Validation Data", type=EvaluationData),
        ListPlaceNode(name="Test Data", type=TestData),
        ArgumentEdgeToTransition("Training Data", "Data Augmentation", "data"),
        FunctionTransitionNode(name="Data Augmentation", function=data_augmentation),
        ReturnedEdgeFromTransition("Data Augmentation", "Augmented Training Data"),
        ListPlaceNode(name="Augmented Training Data", type=AugmentedTrainingData),
        # Fine-tune model
        ListPlaceNode(name="Proposed Model & Hyperparameters", type=VisionModel),
        ArgumentEdgeToTransition("Proposed Model & Hyperparameters", "Fine-tuning", "model"),
        ArgumentEdgeToTransition("Augmented Training Data", "Fine-tuning", "data"),
        FunctionTransitionNode(name="Fine-tuning", function=fine_tune),
        ReturnedEdgeFromTransition("Fine-tuning", "Fine-tuned Model"),
        ListPlaceNode(name="Fine-tuned Model", type=FineTunedVisionModel),
        # Evaluate on validation data
        ArgumentEdgeToTransition("Fine-tuned Model", "Evaluate Model with Validation Data", "model"),
        ArgumentEdgeToTransition("Validation Data", "Evaluate Model with Validation Data", "data"),
        FunctionTransitionNode(name="Evaluate Model with Validation Data", function=evaluate_model),
        ReturnedEdgeFromTransition(
            "Evaluate Model with Validation Data", "Evaluation Metrics from Validation Data"
        ),
        ListPlaceNode(name="Evaluation Metrics from Validation Data", type=EvaluationMetrics),
        # Retry with another model, or accept the current fine-tuned one
        ArgumentEdgeToTransition(
            "Fine-tuned Model", "Select Another Model or Accept Current Model", "model"
        ),
        ArgumentEdgeToTransition(
            "Evaluation Metrics from Validation Data",
            "Select Another Model or Accept Current Model",
            "metrics",
        ),
        FunctionTransitionNode(
            name="Select Another Model or Accept Current Model", function=select_another_model
        ),
        ReturnedEdgeFromTransition(
            "Select Another Model or Accept Current Model", "Proposed Model & Hyperparameters"
        ),
        ReturnedEdgeFromTransition("Select Another Model or Accept Current Model", "Accepted Model"),
        ListPlaceNode(name="Accepted Model", type=FineTunedVisionModel),
        # Final evaluation on the held-out test data
        ArgumentEdgeToTransition("Accepted Model", "Final Model Evaluation", "model"),
        ArgumentEdgeToTransition("Test Data", "Final Model Evaluation", "data"),
        FunctionTransitionNode(name="Final Model Evaluation", function=evaluate_model),
        ReturnedEdgeFromTransition("Final Model Evaluation", "Evaluation Metrics from Test Data"),
        ListPlaceNode(name="Evaluation Metrics from Test Data", type=EvaluationMetrics),
        # Package the accepted model and run integration tests
        ArgumentEdgeToTransition("Accepted Model", "Package For Deployment", "model"),
        FunctionTransitionNode(name="Package For Deployment", function=package_model),
        ReturnedEdgeFromTransition("Package For Deployment", "Packaged Model"),
        ListPlaceNode(name="Packaged Model", type=DeploymentModel),
        ArgumentEdgeToTransition("Packaged Model", "Integration Testing", "model"),
        ArgumentEdgeToTransition("Test Data", "Integration Testing", "data"),
        FunctionTransitionNode(name="Integration Testing", function=integration_testing),
    )
    executable_fine_tuning_graph = ExecutableGraphOperations.construct_graph(fine_tuning_nodes_and_edges)
    return (executable_fine_tuning_graph,)


@app.cell
def _(RustworkxGraph, executable_fine_tuning_graph):
    executable_fine_tuning_pydigraph = RustworkxGraph.from_executable_graph(executable_fine_tuning_graph)
    return (executable_fine_tuning_pydigraph,)


@app.cell
def _(FunctionTransitionNode, ListPlaceNode, executable_fine_tuning_pydigraph, graphviz_draw, half_image):
    def _place_node_label(node: ListPlaceNode) -> str:
        _values = "\n".join(str(x) for x in node.tokens)
        return f"{node.name}\n{_values}"

    def _flow_node_attr_fn(node):
        if isinstance(node, ListPlaceNode):
            return {
                "label": _place_node_label(node),
                "color": "deepskyblue",
                "style": "filled",
                "shape": "oval",
            }
        elif isinstance(node, FunctionTransitionNode):
            return {
                "label": node.name,
                "color": "lightgreen",
                "style": "filled",
                "shape": "box",
            }
        else:
            raise ValueError("Invalid node data type.")

    half_image(
        graphviz_draw(executable_fine_tuning_pydigraph, node_attr_fn=_flow_node_attr_fn, method="dot")
    )
    return


if __name__ == "__main__":
    app.run()
