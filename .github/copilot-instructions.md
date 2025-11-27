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
  - `type_relationship_graph_components.py`: Manages type relationships.
- **Helpers**: Utility functions in `petritype/helpers/`.
- **Visualization**: Graph rendering tools in `petritype/core/visualization/`.
- **Examples**: Demonstrations in `examples/`.
- **Tests**: Unit tests in `test/`.

## Developer Workflows

### Building the Project
- Use `setup.py` for packaging and installation.
- Install dependencies from `requirements.txt`:
  ```bash
  pip install -r requirements.txt
  ```

### Running Tests
- Tests are organized by module in the `test/` directory.
- Run all tests:
  ```bash
  pytest
  ```
- Run specific tests, e.g., for `data_structures.py`:
  ```bash
  pytest test/core/test_data_structures.py
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
- **Visualization**: Use `rustworkx_to_graphviz.py` for rendering graphs.
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