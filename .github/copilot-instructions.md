# Copilot Instructions for Petritype

## Project Overview
Petritype is an experimental tool inspired by Petri nets, designed for prototyping and visualizing data processing pipelines. It provides a structured way to manage stateful processes, especially when outcomes are unpredictable or non-deterministic. The core abstraction revolves around tokens, places, and transitions, which are used to model data flow and processing.

### Key Concepts
- **Tokens**: Represent data elements.
- **Places**: Hold tokens.
- **Transitions**: Define rules for processing tokens and updating places.
- **Priority Functions**: Determine the order of transition execution.

## Codebase Structure
- **Core Logic**: Located in `petritype/core/`, including modules like:
  - `ast_extraction.py`: Handles abstract syntax tree operations.
  - `data_structures.py`: Defines key data structures.
  - `executable_graph_components.py`: The engine — graph construction, firing, validation.
  - `runtime.py` (top level): Runner / RunContext for observable, interactive nets.
- **Helpers**: Utility functions in `petritype/helpers/`.
- **Visualization**: Graph rendering in `petritype/plotting/` (needs the `viz` extra).
- **Examples**: marimo notebooks in `examples/`.
- **Tests**: Unit tests in `tests/`.

## Developer Workflows

### Building the Project
- Packaging uses the `uv_build` backend; there is no `setup.py` or `requirements.txt`.
- Sync dependencies (extras: `dev`, `viz`, `marimo`, `examples`):
  ```bash
  uv sync --extra dev --extra examples
  ```
- Build distributions:
  ```bash
  uv build
  ```

### Running Tests
- Tests live in `tests/`. Run them from the repository root: some fixtures
  resolve paths relative to the process working directory.
- Run the unit suite:
  ```bash
  uv run --extra dev pytest
  ```
- The marimo notebook suite is deselected by default and needs the `examples`
  extra plus the Graphviz `dot` binary:
  ```bash
  uv run --extra dev --extra examples pytest -m notebooks
  ```

### Debugging
- Use the `examples/` directory to explore practical use cases.
- Modify or extend example notebooks to test new features.

## Project-Specific Conventions
- **Mutable State**: Be cautious of in-place modifications to the Petri net during transition firing.
- **Transition Priority**: Ensure priority functions are well-defined to avoid deadlocks or infinite loops.
- **Token Priority**: Use token priorities to control processing order.
- **Edge Direction**: While edges are directed, transitions can affect both input and output places.

## Integration Points
- **Visualization**: Use `petritype/plotting/rustworkx_to_graphviz.py` for rendering graphs.
- **External Dependencies**: Key libraries include `rustworkx` for graph operations and `graphviz` for visualization.

## Examples
- Explore the `examples/` directory for:
  - Token distribution (`toy/distribution_function/`)
  - Matching tokens (`toy/match_up_tokens/`)
  - One-to-many relationships (`toy/one_to_many/`)

## Notes for AI Agents
- Follow the Petri net formalism strictly to maintain consistency.
- Prioritize readability and modularity when extending core components.
- Refer to `README.md` and `examples/` for context on usage patterns.