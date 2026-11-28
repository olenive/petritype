# Copilot Instructions for Petritype

## Project Overview
Petritype is a typed, executable, visual Petri net library for Python, designed for prototyping, running, and visualising stateful data processing pipelines. The core abstraction revolves around tokens, places, edges, and transitions, which model typed data flow and stateful execution.

### Key Concepts
- **Tokens**: Typed data elements held in places, identified by stable integer token IDs and optional derived keys.
- **Places**: Hold tokens (`ListPlaceNode`, `SubnetPlaceNode`, etc.) and validate token types.
- **Transitions**: Define functions, guards, and priority rules for consuming, reading, mutating, and producing tokens.
- **Edges**: Directed connections between places and transitions (consuming argument/drain/take-where edges, non-consuming snapshot/survey/read/mutate edges, and return edges).
- **Priority Functions**: Determine the order of transition execution when multiple transitions are enabled.

## Codebase Structure
- **Core Engine (`petritype/core/`)**:
  - `executable_graph_components.py`: The engine — graph construction, validation, firing, subnet places, and edges.
  - `data_structures.py`: Core data types and graph-level structures.
  - `events.py`: Frozen event records (`Fired`, `Failed`, `Mutated`, `Nested`, etc.) emitted while `record_events` is enabled.
  - `keys.py`: Token keys, `Filter`, `Clause`, and `Query` for keyed edges and token querying/filtering.
  - `nested.py`: Nested nets (nets as tokens and subnet places).
  - `serialisation.py`: JSON encoding/decoding of tokens and nets (`JsonForm`, `Templates`, `NetJson`, marking export/import).
  - `type_comparisons.py`: Runtime type compatibility checks for places, edges, and transitions.
  - `type_names.py`: Short display names for Python type annotations and callables (`TypeNames`).
  - `ast_extraction.py`, `parse_modules.py`, `relationship_graph_components.py`, `descriptions.py`: Module AST parsing and type-relationship graphs.
- **Runtime & Decorator**:
  - `petritype/runtime.py`: Async `Runner` / `RunContext` for observable, interactive net execution.
  - `petritype/decorator.py`: `@petri_net` decorator attaching discovery metadata (`_petri_net_config`).
  - `petritype/marimo_controls.py`: Interactive marimo notebook controls (requires the `marimo` extra).
- **Protocol Runner (`petritype/runner/`)**:
  - `__main__.py` & `process.py`: Entry point (`python -m petritype.runner`) and stdio protocol loop.
  - `protocol.py`, `owner.py`, `registry.py`, `translate.py`: Net discovery, ownership/lifecycle, JSON translation, and protocol handling.
- **Helpers**: Utility functions in `petritype/helpers/` (`structures.py`, `io/io_helper.py`).
- **Visualization**: Graph rendering in `petritype/plotting/` (`rustworkx_graph.py`, `rustworkx_to_graphviz.py`, `simple_graphviz.py`; requires the `viz` extra and Graphviz `dot`).
- **Examples**: Runnable quickstart (`examples/quickstart.py`) and marimo notebooks in `examples/`.
- **Tests**: Unit, protocol, runner, and notebook tests in `tests/`.

## Developer Workflows

### Packaging & Dependencies
- Packaging uses `uv_build`; there is no `setup.py` or `requirements.txt`.
- The base install must only depend on `pydantic` (enforced by `tests/test_dependency_boundaries.py` and CI). Optional dependencies live in extras: `dev`, `viz`, `marimo`, `examples`.
- Sync dependencies:
  ```bash
  uv sync --extra dev --extra examples
  ```
- Build distributions:
  ```bash
  uv build
  ```

### Running Checks (`Justfile`)
- Common tasks are defined in `Justfile`; run `just check` before a release (runs `lint`, `typecheck`, `test`, `notebooks`, `gallery`, `build`).
- Lint with `ruff` (do not introduce `mypy`):
  ```bash
  uv run --extra dev ruff check petritype tests examples scripts
  ```
- Type-check with `pyright` (`just typecheck`): it checks user-style code against the public API, `tests/typecheck/user_code.py`, as set in `pyproject.toml`'s `[tool.pyright]`, and not the library's own modules:
  ```bash
  uv run --extra dev pyright
  ```
- Run tests from the repository root (some fixtures resolve paths relative to cwd):
  ```bash
  uv run --extra dev --extra examples pytest -q
  ```
- The marimo notebook suite is deselected by default and requires the `examples` extra plus Graphviz (`dot` / `sfdp`):
  ```bash
  uv run --extra dev --extra examples pytest -m notebooks -q
  ```

### Debugging
- Use the `examples/` directory to explore practical use cases.
- Modify or extend example notebooks to test new features.

## Project-Specific Conventions
- **Namespace Classes**: Group related stateless functions in namespace classes **without** `self`, and decorate every one of them `@staticmethod` (outermost, above any other decorator) so that type checkers, pyright among them, accept calls through the class (e.g., `ExecutableGraphOperations.construct_graph(...)`, `TypeNames.of(...)`). `tests/test_namespace_classes.py` enforces this.
- **Mutable State**: Be cautious of in-place modifications to tokens and the Petri net during transition firing (especially with mutate/read edges).
- **Transition Priority**: Ensure priority functions are well-defined to avoid deadlocks or infinite loops.
- **Edge Direction**: While edges are directed, transitions can affect both input and output places (e.g., mutate edges update tokens in input places).
- **Library Logging**: Use `logging.getLogger(__name__)` rather than `print(...)` inside library code.

## Integration Points
- **Visualization**: Use `petritype/plotting/rustworkx_to_graphviz.py` (and `simple_graphviz.py`) for rendering graphs via `rustworkx` and Graphviz.
- **Protocol Runner**: External hosts drive nets over the JSON protocol implemented in `petritype/runner/`.

## Examples
- Explore `examples/` for runnable patterns including:
  - `quickstart.py` (checked against `README.md` by unit tests)
  - `toy/` (`distribution_function/`, `match_up_tokens/`, `one_to_many/`, etc.)
  - `subnet_places/`, `tokens_as_nets/`, `groups/`, `edges_on_empty_places/`, `token_view_filtering/`, `execution_modes/`, `interactive/`, `time_series_stats/`

## Notes for AI Agents
- Follow the Petri net formalism strictly to maintain consistency.
- Keep the base dependency boundary (`pydantic` only) intact; gate optional extras with `petritype._optional.require`.
- Prioritize readability and modularity when extending core components.
- Refer to `README.md` and `examples/` for context on usage patterns.
