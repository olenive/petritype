from copy import deepcopy
from typing import TypeAliasType
from types import UnionType
from typing import Callable, Iterable, Literal, NoReturn, Optional, Sequence, Type, Union, Any, get_type_hints, get_origin, get_args
from pydantic import BaseModel, model_validator
import asyncio
import functools
import inspect
import warnings

from petritype.core.data_structures import ArgumentName, FunctionName, KwArgs, PlaceNodeName, ReturnIndex
from petritype.core.type_comparisons import CompareTypes
from petritype.helpers.structures import SafeMerge


type TransitionName = str

# Distinguishes "argument not passed" from an explicit None (which means unlimited)
# while `max_transitions` remains a deprecated alias of `stop_after_n_firings`.
_UNSET: Any = object()


class TransitionFailedError(RuntimeError):
    """A transition body (or its output routing) raised after its input tokens
    had been consumed.

    Attributes:
        transition_name: Name of the transition whose firing failed.
        consumed: The consumed tokens, keyed by argument name. Recover them
            from here — by default they are NOT put back into their places,
            because the body may have mutated them before failing and a place
            should never silently hold corrupted tokens.
        restored: True if restore_tokens_on_failure was enabled and the tokens
            were put back (best effort) into their source places.

    The original exception is chained as __cause__.
    """

    def __init__(self, transition_name: str, consumed: dict, restored: bool = False):
        self.transition_name = transition_name
        self.consumed = consumed
        self.restored = restored
        tokens_note = (
            "consumed tokens were restored to their places (best effort)"
            if restored
            else "consumed tokens were NOT restored; recover them from this error's .consumed"
        )
        super().__init__(
            f"Firing of transition '{transition_name}' failed; {tokens_note}. "
            f"Consumed arguments: {sorted(consumed)}."
        )


class PositionalArgsBaseModel(BaseModel):
    """Base class that enables positional arguments for Pydantic models."""
    
    def __init__(self, *args, **kwargs):
        field_names = list(self.__class__.model_fields.keys())
        # Map positional arguments to field names
        for i, arg in enumerate(args):
            if i < len(field_names):
                field_name = field_names[i]
                if field_name not in kwargs:
                    kwargs[field_name] = arg
        
        super().__init__(**kwargs)

    model_config = {
        "extra": "forbid"
    }


class ListPlaceNode(PositionalArgsBaseModel):
    name: PlaceNodeName
    type: Any  # Temporarily accept any value  
    tokens: list[Any] = []
    # TODO: Add validation to check that type matches tokens

    @model_validator(mode="after")
    def validate_type_field(self):
        type_of_value = self.type
        
        if type_of_value is not None:
            # A valid place type is a plain class, any subscripted generic or union (both the
            # typing.List[int] and builtin list[int] spellings, and both the Union[int, str] and
            # int | str spellings, all of which have a non-None origin), or a PEP 695 alias (a
            # bare alias has no origin, so it needs its own check). get_origin replaces earlier
            # isinstance checks against typing's private _GenericAlias / _UnionGenericAlias, which
            # are deprecated and removed in Python 3.17.
            if not (
                isinstance(type_of_value, type)
                or get_origin(type_of_value) is not None
                or isinstance(type_of_value, TypeAliasType)
            ):
                raise ValueError(
                    f"Expected type to be a class, a subscripted generic/union, or a type alias "
                    f"but got: {type_of_value} (type: {type(type_of_value)})"
                )
        else:
            raise NotImplementedError(
                "Unclear at the time of writing what it means for a place node to have no type."
            )
        
        return self

    @model_validator(mode="after")
    def check_type_matches_tokens(self):
        for token in self.tokens:
            if not CompareTypes.between_value_and_type(token, self.type):
                raise TypeError(
                    f"Expected token to be of type {self.type} in {self.name}, got {type(token)}."
                    f"\nToken: {token}"
                )
        return self

    def copy_sans_tokens(self) -> "ListPlaceNode":
        return ListPlaceNode(self.name, self.type)


# An alias to ListPlaceNode just called PlaceNode.


class FunctionTransitionNode(PositionalArgsBaseModel):
    """A transition node that executes a function when fired.

    Attributes:
        name: Unique identifier for the transition
        function: The function to execute when this transition fires
        output_distribution_function: Optional function to distribute outputs to places
        kwargs: Optional keyword arguments to pass to the function
        guard: Optional (ExecutableGraph) -> bool enabling condition, enforced by the
            engine in every execution mode: a transition whose guard returns False is
            not enabled, regardless of available tokens or selector. The guard runs on
            every enabled-discovery sweep, so keep it a cheap, side-effect-free
            predicate over the marking (None costs nothing — the check is skipped).
        priority: Optional (ExecutableGraph) -> float selection hint. The default
            selector fires the highest-priority enabled transition; transitions
            without one score 0.0, and ties fall back to definition order. Only
            called on enabled transitions at selection time.
        activation_function: Deprecated — use ``guard`` (engine-enforced enabling) or
            ``priority`` (selection ranking) instead. The engine never consults this
            field; it is only visible to custom transition selectors, and it will be
            removed in a future release.
    """
    name: str
    function: Callable
    output_distribution_function: Optional[Callable[[Any], dict[PlaceNodeName, Any]]] = None
    kwargs: Optional[KwArgs] = None
    guard: Optional[Callable] = None
    priority: Optional[Callable] = None
    activation_function: Optional[Callable] = None
    # How a *synchronous* body runs: "inline" (default, on the event loop) or "thread"
    # (offloaded to an executor so a blocking / CPU-bound body doesn't freeze the loop). Async
    # bodies already yield and ignore this. Process-level parallelism is the function's own
    # concern (bring your own process pool inside the body) — the engine never pickles a token.
    execution: Literal["inline", "thread"] = "inline"

    @model_validator(mode="after")
    def _warn_if_activation_function(self) -> "FunctionTransitionNode":
        if self.activation_function is not None:
            warnings.warn(
                "activation_function is deprecated: use `guard` for an engine-enforced "
                "enabling condition or `priority` for selection ranking. The engine ignores "
                "activation_function; it is only visible to custom transition selectors.",
                DeprecationWarning,
                stacklevel=2,
            )
        return self


class ArgumentEdgeToTransition(PositionalArgsBaseModel):
    place_node_name: PlaceNodeName
    transition_node_name: FunctionName
    argument: ArgumentName


class ReturnedEdgeFromTransition(PositionalArgsBaseModel):
    transition_node_name: FunctionName
    place_node_name: PlaceNodeName
    return_index: Optional[ReturnIndex] = None


class SnapshotEdge(PositionalArgsBaseModel):
    """A non-consuming read edge (place -> transition). The transition receives a **deep-copy
    snapshot** of the place's tokens, so reading cannot disturb the place. Enabledness requires
    the place to be non-empty (presence), but firing does not consume the tokens."""
    place_node_name: PlaceNodeName
    transition_node_name: FunctionName
    argument: ArgumentName


class MutateEdge(PositionalArgsBaseModel):
    """A non-consuming read/write edge (place -> transition). The transition receives the
    **live** tokens and may modify them in place; they stay in the place (not consumed) but may
    be changed. Enabledness requires the place to be non-empty (presence)."""
    place_node_name: PlaceNodeName
    transition_node_name: FunctionName
    argument: ArgumentName


class ExecutableGraph(BaseModel):
    """A Petri net graph that can be executed.

    Attributes:
        places: Collection of place nodes that hold tokens
        transitions: Collection of transition nodes that transform tokens
        argument_edges: Edges from places to transitions (input)
        return_edges: Edges from transitions to places (output)
        step_count: Monotonic count of transitions that have fired since this
            graph was created. Increments by one every time ``execute_graph``
            successfully fires a transition. Use this as a stable sequence
            number when you need to identify "which step are we on" — e.g.
            for idempotency / compare-and-swap semantics over an unreliable
            transport, or for replay / fork-from-step features. Independent
            of ``transition_history``, which is capped for memory and is
            therefore unsuitable as an authoritative counter.
        last_fired: Name of the most recently fired transition, or None if the
            last firing attempt fired nothing. ``execute_graph`` resets it per
            call; the sequential Runner resets it per attempt (same contract).
            The concurrent Runner loops also set it as they deposit results,
            where it names only *one* completion of each batch — observers
            that must see every firing should diff ``fired_counts`` instead
            (see ``petritype.runtime.fired_since``).
        fired_counts: Cumulative {transition_name: times_fired} since the graph
            was created. Monotonic and never trimmed (like ``step_count``), so
            it is the reliable way to ask "has transition X ever fired" — the
            capped ``transition_history`` cannot answer that.
        transition_history: History of fired transitions
        input_place_history: History of input place states
        output_place_history: History of output place states
        token_history: History of tokens
        transition_selector: Optional function to select which transition fires next.
            Signature: (graph, enabled_transitions) -> transition_to_fire
            If None, defaults to firing the last enabled transition (current behavior).
            The selector receives the full graph context and list of enabled transitions,
            allowing for sophisticated selection strategies (priority-based, random, etc.).
    """
    places: Sequence[ListPlaceNode]
    transitions: Sequence[FunctionTransitionNode]
    argument_edges: Sequence[ArgumentEdgeToTransition]
    return_edges: Sequence[ReturnedEdgeFromTransition]
    # Non-consuming read edges (place -> transition): SnapshotEdge passes a deep copy (place
    # untouched); MutateEdge passes the live tokens (transition may modify them in place).
    snapshot_edges: Sequence[SnapshotEdge] = []
    mutate_edges: Sequence[MutateEdge] = []
    step_count: int = 0
    # Name of the most recent transition fired by the last firing attempt (an
    # ``execute_graph`` call, or one sequential-Runner attempt), or None if it
    # fired nothing. Reset at the start of every attempt, so it always reflects
    # that attempt (never a stale earlier fire).
    # Unlike ``transition_history`` this is independent of history-retention
    # config, giving callers a reliable "what just fired" signal — e.g. for UI
    # highlighting — without inferring it from token-count diffs (which is
    # ambiguous for self-loops). Transition names are unique (enforced by
    # ``check_unique_names``), so a name unambiguously identifies the node.
    # The concurrent Runner deposit loops also set this (outside
    # ``execute_graph``); a batch of completions leaves only one name here, so
    # it is a convenience signal — diffing ``fired_counts`` via
    # ``petritype.runtime.fired_since`` is the lossless "what fired since I
    # last looked" API.
    last_fired: Optional[str] = None
    # Cumulative count of how many times each transition has fired since this
    # graph was created (keyed by transition name). Like ``step_count`` it is
    # monotonic and never trimmed — independent of ``transition_history``'s
    # retention cap — so it is the authoritative answer to "has transition X
    # fired, and how often". Use it (instead of inspecting the capped history)
    # to drive stage/progress UI that must stay correct after the marking has
    # moved past a stage.
    fired_counts: dict[str, int] = {}
    # Names of transitions whose bodies are currently executing — they consumed their inputs but
    # have not yet deposited their outputs (only populated in concurrent mode). The renderer
    # lights these "in flight" transitions distinctly; each is removed as it completes.
    in_flight: set[str] = set()
    transition_history: Sequence[FunctionTransitionNode] = []
    input_place_history: Sequence[ListPlaceNode] = []
    output_place_history: Sequence[ListPlaceNode] = []
    token_history: Sequence[Any] = []
    transition_selector: Optional[Callable] = None
    allow_token_copying: bool = False
    # When a firing fails after its input tokens were consumed, put them back
    # (best effort) before raising. Off by default: the body may have mutated
    # the tokens before failing, and silently restoring possibly-corrupt tokens
    # to a place is worse than a visible loss. Enable only when tokens are
    # immutable or bodies do not mutate them before they can fail. Either way
    # the raised TransitionFailedError carries the consumed tokens.
    restore_tokens_on_failure: bool = False

    def place_named(self, name: str) -> Optional[ListPlaceNode]:
        place_names_to_nodes = {place.name: place for place in self.places}  # TODO: do we need to check every time?
        if len(set(place_names_to_nodes.keys())) != len(place_names_to_nodes.keys()):
            raise ValueError("Duplicate place names found!")
        return place_names_to_nodes.get(name)

    @model_validator(mode='before')
    def check_unique_names(cls, values):
        place_names = [place.name for place in values.get('places', [])]
        transition_names = [transition.name for transition in values.get('transitions', [])]
        if len(place_names) != len(set(place_names)):
            raise ValueError("Place names must be unique.")
        if len(transition_names) != len(set(transition_names)):
            raise ValueError("Transition names must be unique.")
        return values
    
    @model_validator(mode="before")
    def check_edge_names(cls, values):
        places = values.get('places', [])
        transitions = values.get('transitions', [])
        argument_edges = values.get('argument_edges', [])
        return_edges = values.get('return_edges', [])
        
        place_names = {place.name for place in places}
        transition_names = {transition.name for transition in transitions}
        
        for edge in argument_edges:
            if edge.place_node_name not in place_names:
                raise ValueError(f"Argument edge references unknown place: {edge.place_node_name}")
            if edge.transition_node_name not in transition_names:
                raise ValueError(f"Argument edge references unknown transition: {edge.transition_node_name}")
        
        for edge in return_edges:
            if edge.place_node_name not in place_names:
                raise ValueError(f"Return edge references unknown place: {edge.place_node_name}")
            if edge.transition_node_name not in transition_names:
                raise ValueError(f"Return edge references unknown transition: {edge.transition_node_name}")

        for edge in (*values.get('snapshot_edges', []), *values.get('mutate_edges', [])):
            if edge.place_node_name not in place_names:
                raise ValueError(f"Read edge references unknown place: {edge.place_node_name}")
            if edge.transition_node_name not in transition_names:
                raise ValueError(f"Read edge references unknown transition: {edge.transition_node_name}")

        return values

    @model_validator(mode="before")
    def check_edge_types(cls, values):
        places = values.get('places', [])
        transitions = values.get('transitions', [])
        argument_edges = values.get('argument_edges', [])
        return_edges = values.get('return_edges', [])
        place_names_to_nodes = {place.name: place for place in places}
        transition_names_to_nodes = {transition.name: transition for transition in transitions}

        for edge in argument_edges:
            if not isinstance(edge, ArgumentEdgeToTransition):
                raise TypeError(f"Expected ArgumentEdgeToTransition, got {type(edge)}")
            place = place_names_to_nodes[edge.place_node_name]
            if not isinstance(place, ListPlaceNode):
                raise NotImplementedError("Currently only ListPlaceNode is supported.")
            transition = transition_names_to_nodes[edge.transition_node_name]
            if not isinstance(transition, FunctionTransitionNode):
                raise NotImplementedError("Currently only FunctionTransitionNode is supported.")
            place_type = place.type  # This needs to be the value of the 'type' field of the place.
            argument_type = get_type_hints(transition.function).get(edge.argument)
            if place_type is not None and argument_type is not None:
                if not CompareTypes.between_annotations_where_both_maybe_in_list(
                    annotation1=place_type,  # The place.type contains the inner type event if the place
                    # is a ListPlaceNode that holds a list of tokens.
                    annotation2=argument_type,  # This is to allow the case of passing in all the tokens
                    # at once as a list.
                ):
                    raise TypeError(
                        f"Type mismatch for argument edge from place '{place.name}' to transition "
                        f"'{transition.name}': place type '{place_type}' does not match argument type "
                        f"'{argument_type}'."
                    )

        for edge in return_edges:
            if not isinstance(edge, ReturnedEdgeFromTransition):
                raise TypeError(f"Expected ReturnedEdgeFromTransition, got {type(edge)}")
            place = place_names_to_nodes[edge.place_node_name]
            if not isinstance(place, ListPlaceNode):
                raise NotImplementedError("Currently only ListPlaceNode is supported.")
            transition = transition_names_to_nodes[edge.transition_node_name]
            if not isinstance(transition, FunctionTransitionNode):
                raise NotImplementedError("Currently only FunctionTransitionNode is supported.")
            if transition.output_distribution_function is not None:
                # A distribution function reshapes the result, so the return
                # annotation does not describe individual tokens; those are
                # validated against their destination places at fire time.
                continue
            place_type = place.type
            return_type = get_type_hints(transition.function).get('return')
            if edge.return_index is not None and return_type is not None:
                # Indexed edge: the token flowing along it is the return tuple's
                # element at return_index, not the whole return value.
                if get_origin(return_type) is not tuple:
                    raise TypeError(
                        f"Return edge from transition '{transition.name}' to place '{place.name}' has "
                        f"return_index={edge.return_index} but the return annotation '{return_type}' "
                        f"is not a tuple."
                    )
                tuple_args = get_args(return_type)
                if len(tuple_args) == 2 and tuple_args[1] is Ellipsis:
                    return_type = tuple_args[0]  # tuple[X, ...]: every element is X
                elif edge.return_index < len(tuple_args):
                    return_type = tuple_args[edge.return_index]
                else:
                    raise TypeError(
                        f"Return edge from transition '{transition.name}' to place '{place.name}' has "
                        f"return_index={edge.return_index} but the return annotation '{return_type}' "
                        f"only has {len(tuple_args)} elements."
                    )
            # A union return annotation routes by type at fire time, so the edge
            # is valid if any non-None arm can land in this place (None results
            # are dropped, never deposited).
            if get_origin(return_type) in (Union, UnionType):
                candidate_types = [t for t in get_args(return_type) if t is not type(None)]
            elif return_type is type(None):
                candidate_types = []
            else:
                candidate_types = [return_type]
            if not candidate_types:
                continue
            if place_type is not None and return_type is not None:
                if not any(
                    CompareTypes.between_annotations_where_both_maybe_in_list(
                        annotation1=place_type,  # May itself be a list type — places can hold list tokens.
                        annotation2=candidate,  # All the tokens could be returned at once as a list.
                    )
                    for candidate in candidate_types
                ):
                    raise TypeError(
                        f"Type mismatch for return edge from transition '{transition.name}' to place "
                        f"'{place.name}': place type '{place_type}' does not match return type "
                        f"'{return_type}'."
                    )

        # Indexed and non-indexed return edges cannot be mixed on one transition:
        # the fire path either splits a tuple by index or routes the whole result.
        return_edges_by_transition: dict[str, list] = {}
        for edge in return_edges:
            return_edges_by_transition.setdefault(edge.transition_node_name, []).append(edge)
        for transition_name, edges in return_edges_by_transition.items():
            if ExecutableGraphCheck.return_indices_are_a_mix_of_none_and_non_none(tuple(edges)):
                raise ValueError(
                    f"Return edges of transition '{transition_name}' mix indexed and non-indexed "
                    f"return_index values; use integers on all of them or on none."
                )

        for edge in (*values.get('snapshot_edges', []), *values.get('mutate_edges', [])):
            place = place_names_to_nodes[edge.place_node_name]
            transition = transition_names_to_nodes[edge.transition_node_name]
            place_type = place.type
            argument_type = get_type_hints(transition.function).get(edge.argument)
            if place_type is not None and argument_type is not None:
                if not CompareTypes.between_annotations_where_both_maybe_in_list(
                    annotation1=place_type,
                    annotation2=argument_type,
                ):
                    raise TypeError(
                        f"Type mismatch for read edge from place '{place.name}' to transition "
                        f"'{transition.name}': place type '{place_type}' does not match argument type "
                        f"'{argument_type}'."
                    )

        return values


# This is intended as shorthand for the common case when adding a transition with output(s) to a graph.
def function_transition_node_and_output_edges(
    *,
    name: str,
    function: Callable,
    output_place_names: list[PlaceNodeName],
    kwargs: Optional[KwArgs] = None,
    output_distribution_function: Optional[Callable[[Any], dict[PlaceNodeName, Any]]] = None,
    use_return_indices: bool = False,
) -> Sequence[FunctionTransitionNode | ReturnedEdgeFromTransition]:
    transition = FunctionTransitionNode(
        name=name, function=function, kwargs=kwargs, output_distribution_function=output_distribution_function
    )
    output_edges = []
    if use_return_indices:
        for i, place_name in enumerate(output_place_names):
            output_edges.append(
                ReturnedEdgeFromTransition(transition_node_name=name, place_node_name=place_name, return_index=i)
            )
    else:
        for place_name in output_place_names:
            output_edges.append(ReturnedEdgeFromTransition(transition_node_name=name, place_node_name=place_name))
    return (transition, *output_edges)


def function_transition_node_and_output_places(
    *,
    name: str,
    function: Callable,
    output_place_names_and_types: list[tuple[PlaceNodeName, Any]],
    kwargs: Optional[KwArgs] = None,
    output_distribution_function: Optional[Callable[[Any], dict[PlaceNodeName, Any]]] = None,
    use_return_indices: bool = False,
) -> Sequence[FunctionTransitionNode | ReturnedEdgeFromTransition | ListPlaceNode]:
    transition = FunctionTransitionNode(
        name=name, function=function, kwargs=kwargs, output_distribution_function=output_distribution_function
    )
    output_edges = []
    output_places = []
    if use_return_indices:
        for i, (place_name, place_type) in enumerate(output_place_names_and_types):
            output_edges.append(
                ReturnedEdgeFromTransition(transition_node_name=name, place_node_name=place_name, return_index=i)
            )
            output_places.append(ListPlaceNode(name=place_name, type=place_type))
    else:
        for place_name, place_type in output_place_names_and_types:
            output_edges.append(ReturnedEdgeFromTransition(transition_node_name=name, place_node_name=place_name))
            output_places.append(ListPlaceNode(name=place_name, type=place_type))
    return (transition, *output_edges, *output_places)


class MapPlaceNames:

    def to_list_place_nodes(executable_graph: ExecutableGraph) -> dict[str, ListPlaceNode]:
        list_place_nodes = {}
        for node in executable_graph.places:
            if isinstance(node, ListPlaceNode):
                list_place_nodes[node.name] = node
            else:
                raise ValueError(f"Unexpected node type: {type(node)}")
        return list_place_nodes


class MapTransitionNames:

    def to_function_transition_nodes(executable_graph: ExecutableGraph) -> dict[str, FunctionTransitionNode]:
        function_nodes = {}
        for node in executable_graph.transitions:
            if isinstance(node, FunctionTransitionNode):
                function_nodes[node.name] = node
            else:
                raise ValueError(f"Unexpected node type: {type(node)}")
        return function_nodes

    def to_incoming_edges(executable_graph: ExecutableGraph) -> dict[str, tuple[ArgumentEdgeToTransition, ...]]:
        incoming_edges = {}
        for edge_to in executable_graph.argument_edges:
            if isinstance(edge_to, ArgumentEdgeToTransition):
                if edge_to.transition_node_name in incoming_edges:
                    incoming_edges[edge_to.transition_node_name] += (edge_to,)
                else:
                    incoming_edges[edge_to.transition_node_name] = (edge_to,)
            else:
                raise ValueError(f"Unexpected node type: {type(edge_to)}")
        return incoming_edges

    def to_outgoing_edges(executable_graph: ExecutableGraph) -> dict[str, tuple[ReturnedEdgeFromTransition, ...]]:
        outgoing_edges = {}
        for edge_from in executable_graph.return_edges:
            if isinstance(edge_from, ReturnedEdgeFromTransition):
                if edge_from.transition_node_name in outgoing_edges:
                    outgoing_edges[edge_from.transition_node_name] += (edge_from,)
                else:
                    outgoing_edges[edge_from.transition_node_name] = (edge_from,)
            else:
                raise ValueError(f"Unexpected node type: {type(edge_from)}")
        return outgoing_edges

    def to_read_edges(
        executable_graph: ExecutableGraph,
    ) -> dict[str, tuple[Union["SnapshotEdge", "MutateEdge"], ...]]:
        """Map each transition to its non-consuming read edges (SnapshotEdge + MutateEdge)."""
        read_edges: dict[str, tuple] = {}
        for edge in (*executable_graph.snapshot_edges, *executable_graph.mutate_edges):
            read_edges[edge.transition_node_name] = read_edges.get(edge.transition_node_name, ()) + (edge,)
        return read_edges


class ExecutableGraphCheck:
    """Functions that do not alter the executable graph."""

    def sufficient_tokens_are_available(
        transition: FunctionTransitionNode,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        transition_names_to_read_edges: Optional[dict[str, tuple]] = None,
    ) -> bool:
        # Transitions with no incoming edges (generators) are always ready to fire
        incoming_edges: tuple[ArgumentEdgeToTransition, ...] = transition_names_to_incoming_edges.get(
            transition.name, tuple()
        )
        for edge in incoming_edges:
            place = place_names_to_nodes[edge.place_node_name]
            if len(place.tokens) == 0:
                return False
        # Read edges (snapshot/mutate) require presence too — there must be a token to read —
        # but firing does not consume them.
        if transition_names_to_read_edges:
            for edge in transition_names_to_read_edges.get(transition.name, ()):
                if len(place_names_to_nodes[edge.place_node_name].tokens) == 0:
                    return False
        return True

    def transition_is_enabled(
        transition: FunctionTransitionNode,
        executable_graph: ExecutableGraph,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        transition_names_to_read_edges: Optional[dict[str, tuple]] = None,
    ) -> bool:
        """Full enablement: sufficient tokens plus the transition's guard, if any.

        Every enabled-discovery sweep (sequential execution and both concurrent runner
        loops) must go through this, so a guard means the same thing in every execution
        mode. Guards run on every sweep — they should be cheap, side-effect-free
        predicates over the marking.
        """
        if not ExecutableGraphCheck.sufficient_tokens_are_available(
            transition=transition,
            transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            place_names_to_nodes=place_names_to_nodes,
            transition_names_to_read_edges=transition_names_to_read_edges,
        ):
            return False
        return transition.guard is None or bool(transition.guard(executable_graph))

    def all_return_indices_are_none(outgoing_edges: tuple[ReturnedEdgeFromTransition, ...]) -> bool:
        for edge in outgoing_edges:
            if edge.return_index is not None:
                return False
        return True

    def all_return_indices_are_integers(outgoing_edges: tuple[ReturnedEdgeFromTransition, ...]) -> bool:
        for edge in outgoing_edges:
            if not isinstance(edge.return_index, int):
                return False
        return True

    def value_disposition_for_place(value: Any, place: ListPlaceNode) -> Literal["token", "batch", "nothing"]:
        """The single point of truth for what a value means at a destination place.

        The interpretation depends on the (value, place-type) pair:
        - "token": the value is one token. Always the case when the place's own
          type is a list type (there the list *is* the token — an empty list is
          a real token), and for any non-list value.
        - "batch": a list value at a scalar-typed place is a batch of tokens,
          deposited element-wise.
        - "nothing": an empty batch; nothing is deposited.

        Routing (value_and_places_types_match), deposit-time validation
        (ensure_token_type_matches_place_type), stage-2 output arbitration
        (only non-"nothing" destinations count toward ambiguity) and the
        stage-3 deposit (add_tokens_to_places) all derive their behaviour
        from this classification — change it here and nowhere else.

        Deliberately NOT a client: the pre-flight sweep over tokens already
        resting in places (ensure_resident_token_type_matches_place_type). A
        resident value is always exactly one token, never a batch, so it is
        validated whole regardless of this rule.
        """
        place_type_is_a_list_type = place.type is list or get_origin(place.type) is list
        if isinstance(value, list) and not place_type_is_a_list_type:
            return "batch" if value else "nothing"
        return "token"

    def value_can_be_deposited(value: Any, place: ListPlaceNode) -> bool:
        """Whether the value, interpreted per its disposition, type-checks at the place."""
        disposition = ExecutableGraphCheck.value_disposition_for_place(value, place)
        if disposition == "nothing":
            return True
        if disposition == "batch":
            return all(CompareTypes.between_value_and_type(item, place.type) for item in value)
        return CompareTypes.between_value_and_type(value, place.type)

    def ensure_token_type_matches_place_type(token: any, place: ListPlaceNode):
        disposition = ExecutableGraphCheck.value_disposition_for_place(token, place)
        if disposition == "batch":
            for item in token:
                if not CompareTypes.between_value_and_type(item, place.type):
                    raise TypeError(
                        f"Expected token item to be of type {place.type} in {place.name}, got {type(item)}."
                        f"\nToken item: {item}"
                    )
        elif disposition == "token":
            if not CompareTypes.between_value_and_type(token, place.type):
                raise TypeError(
                    f"Expected token to be of type {place.type} in {place.name}, got {type(token)}."
                    f"\nToken: {token}"
                )
        # "nothing" (an empty batch) is always acceptable — it deposits no tokens.

    def ensure_resident_token_type_matches_place_type(token: any, place: ListPlaceNode):
        """Validate a token *already resting* in a place — always as one whole token.

        Deliberately does NOT go through value_disposition_for_place. That rule is
        about deposits: a list arriving at a scalar place is a batch to unpack. But a
        value already in a place is by definition exactly one token, so 'batch' is a
        category error here. This mirrors ListPlaceNode.check_type_matches_tokens, the
        place's own construction validator, so the pre-flight sweep and construction
        agree; only post-construction mutation can produce a token they must reject.
        """
        if not CompareTypes.between_value_and_type(token, place.type):
            raise TypeError(
                f"Expected token to be of type {place.type} in {place.name}, got {type(token)}."
                f"\nToken: {token}"
            )

    def ensure_all_token_types_match_place_types(executable_graph: ExecutableGraph):
        for place in executable_graph.places:
            for token in place.tokens:
                ExecutableGraphCheck.ensure_resident_token_type_matches_place_type(token, place)

    def return_indices_are_a_mix_of_none_and_non_none(outgoing_edges: tuple[ReturnedEdgeFromTransition, ...]) -> bool:
        return (
            not ExecutableGraphCheck.all_return_indices_are_none(outgoing_edges)
            and not ExecutableGraphCheck.all_return_indices_are_integers(outgoing_edges)
        )

    def value_and_places_types_match(value: Any, places: Iterable[ListPlaceNode]) -> tuple[ListPlaceNode, ...]:
        """Find places where the value could be deposited.

        A place matches iff the value type-checks under its disposition there
        (see value_disposition_for_place): whole-token at list-typed places,
        element-wise batch at scalar-typed places. At run time we can not
        distinguish the intended type of an empty list's contents, so an empty
        list — an empty batch, or an empty token at a list-typed place —
        matches every place. Stage 2 arbitrates multiple matches: only
        destinations where the deposit is observable (disposition other than
        "nothing") count toward ambiguity, and genuine ambiguity requires
        allow_token_copying.
        """
        return tuple(
            place for place in places
            if ExecutableGraphCheck.value_can_be_deposited(value, place)
        )

    def assert_acyclic(executable_graph: ExecutableGraph) -> None:
        """Raise ValueError if the net's token flow contains a cycle, naming its path
        (``P1 → T1 → P2 → T2 → P1``).

        Token flow is the directed bipartite graph of ``argument_edges``
        (place → transition) and ``return_edges`` (transition → place). Read edges
        are excluded: a snapshot moves no tokens, so a cycle through one cannot
        feed itself; mutate edges are excluded on the same conservative grounds
        (they modify tokens in place but move none).

        Passing proves **no token cycles** — not termination. A transition with no
        ``ArgumentEdgeToTransition`` input (a source — possibly fed only by read
        edges, which consume nothing) can fire forever in a perfectly acyclic net.
        The static termination condition is acyclic *and* every transition
        consumes: pair this with ``assert_no_source_transitions``. Nets that
        legitimately run forever should be bounded at run time instead
        (``RunContext.error_after_n_firings``).
        """
        # Nodes are namespaced (kind, name): unique-name enforcement is per kind,
        # so a place and a transition may legally share a name.
        adjacency: dict[tuple, list[tuple]] = {}
        for edge in executable_graph.argument_edges:
            adjacency.setdefault(("place", edge.place_node_name), []).append(
                ("transition", edge.transition_node_name)
            )
        for edge in executable_graph.return_edges:
            adjacency.setdefault(("transition", edge.transition_node_name), []).append(
                ("place", edge.place_node_name)
            )

        # Iterative DFS — a long pipeline must not hit the recursion limit.
        state: dict[tuple, str] = {}  # "visiting" (on the current path) or "done"
        for start in list(adjacency):
            if start in state:
                continue
            state[start] = "visiting"
            path = [start]
            stack = [iter(adjacency.get(start, ()))]
            while stack:
                successor = next(stack[-1], None)
                if successor is None:
                    state[path.pop()] = "done"
                    stack.pop()
                elif state.get(successor) == "visiting":
                    cycle = path[path.index(successor):] + [successor]
                    raise ValueError(
                        "Token flow contains a cycle: "
                        + " → ".join(name for _, name in cycle)
                    )
                elif successor not in state:
                    state[successor] = "visiting"
                    path.append(successor)
                    stack.append(iter(adjacency.get(successor, ())))

    def assert_no_source_transitions(executable_graph: ExecutableGraph) -> None:
        """Raise ValueError if any transition has no ``ArgumentEdgeToTransition``
        input, naming the offenders. Such a *source* transition consumes nothing
        when it fires (read edges don't count — snapshots and mutates leave the
        place untouched), so it can fire forever and even an acyclic net
        containing one never quiesces. Together with ``assert_acyclic`` this is a
        static termination proof: tokens only flow forward, and every firing
        consumes at least one.

        Deliberate sources (generators, external-input transitions) are
        legitimate — don't assert this on such nets; bound them at run time with
        ``RunContext.error_after_n_firings`` or drive them via
        ``run_indefinitely``.
        """
        consuming = {edge.transition_node_name for edge in executable_graph.argument_edges}
        sources = [t.name for t in executable_graph.transitions if t.name not in consuming]
        if sources:
            raise ValueError(
                "Source transitions (no ArgumentEdgeToTransition input — firing "
                f"consumes nothing, so the net cannot quiesce): {', '.join(sources)}"
            )


# Default selector: highest `priority` wins; transitions without one score 0.0.
# `max` keeps the first maximum, so ties — including nets where nobody sets a
# priority — fall back to definition order.
def default_transition_selector(
    graph: ExecutableGraph, enabled: list[FunctionTransitionNode]
) -> Optional[FunctionTransitionNode]:
    """Default selector: highest-priority enabled transition, definition order on ties."""
    if not enabled:
        return None
    return max(enabled, key=lambda t: t.priority(graph) if t.priority is not None else 0.0)


class ExecutableGraphOperations:
    """Functions that alter the executable graph.

    ## Transitions Algorithm

    ### 1. Pick next transition to execute.
    - Iterate over transition nodes.
    - If there are no more transitions that can fire, end the algorithm.
    - Given a transition node, check if it can fire.
        - Are there sufficient input tokens?

    ### 2. Fire the transition.
    - Remove input tokens from places.
    - Call transition function with the input tokens to generate the output tokens.
    - Add output tokens (append?).
    """

    def construct_graph(
        mixed_nodes_and_edges: Iterable[
            Union[
                ListPlaceNode, FunctionTransitionNode, ArgumentEdgeToTransition,
                ReturnedEdgeFromTransition, SnapshotEdge, MutateEdge,
            ]
        ],
        allow_token_copying: bool = False,
        restore_tokens_on_failure: bool = False,
        expect_acyclic: bool = False,
    ) -> ExecutableGraph:
        places, transitions, edges_to, edges_from = [], [], [], []
        snapshot_edges, mutate_edges = [], []
        for node in mixed_nodes_and_edges:
            if isinstance(node, ListPlaceNode):
                places.append(node)
            elif isinstance(node, FunctionTransitionNode):
                transitions.append(node)
            elif isinstance(node, ArgumentEdgeToTransition):
                edges_to.append(node)
            elif isinstance(node, ReturnedEdgeFromTransition):
                edges_from.append(node)
            elif isinstance(node, SnapshotEdge):
                snapshot_edges.append(node)
            elif isinstance(node, MutateEdge):
                mutate_edges.append(node)
            else:
                raise ValueError(f"Unexpected node type: {type(node)}")
        graph = ExecutableGraph(
            places=places, transitions=transitions, argument_edges=edges_to, return_edges=edges_from,
            snapshot_edges=snapshot_edges, mutate_edges=mutate_edges, allow_token_copying=allow_token_copying,
            restore_tokens_on_failure=restore_tokens_on_failure,
        )
        # Declares "this net is a DAG" at build time; an accidental cycle then
        # fails here with its path named instead of hanging at run time.
        if expect_acyclic:
            ExecutableGraphCheck.assert_acyclic(graph)
        return graph

    def update_output_place_with_result_tokens(result: Any, place: ListPlaceNode) -> None:
        """Update the given place by appending the result token to its tokens list."""
        place.tokens.append(result)

    def stage_1_extract_argument_tokens_from_places(
        transition: FunctionTransitionNode,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        allow_token_copying: bool = False,
        # place_history_length: int = 1,
        token_history_length: int = 0,
        transition_names_to_read_edges: Optional[dict[str, tuple]] = None,
    ) -> tuple[dict[ArgumentName, any], Sequence[ListPlaceNode]]:
        """Remove input tokens from source places
        
        Return input tokens matched to function arguments and the input places without the removed tokens.

        A failed firing can be undone (best effort) with restore_argument_tokens_to_places.
        """
        # Transitions with no incoming edges (generators) have no tokens to extract
        incoming_edges: tuple[ArgumentEdgeToTransition, ...] = transition_names_to_incoming_edges.get(
            transition.name, tuple()
        )
        input_edge_names_to_tokens: dict[ArgumentName, any] = dict()
        input_places = [] # if place_history_length >= 1 else None
        for edge in incoming_edges:
            place = place_names_to_nodes[edge.place_node_name]
            place_copy = place.copy_sans_tokens()
            input_places.append(place_copy)
            argument_type = get_type_hints(transition.function).get(edge.argument)
            place_type = place.type
            # Two cases - passing a single token or passing all tokens as a list.
            argument_origin = get_origin(argument_type)
            if argument_origin is list and CompareTypes.between_annotations_where_one_maybe_in_list(
                annotation_not_in_list=place_type,
                annotation_maybe_in_list=argument_type,
            ):  # If the argument type is a list and the type inside the list matches the place type,
                # pass all tokens as a list.
                tokens = place.tokens
                place.tokens = []
                if allow_token_copying and token_history_length >= 1:
                    tokens_copy = deepcopy(tokens)
                    place_copy.tokens.extend(tokens_copy)
                input_edge_names_to_tokens[edge.argument] = tokens
            else:  # Pass in a single token (FIFO: consume the oldest token first).
                token = place.tokens.pop(0)
                # if place_history_length >= 1:
                if allow_token_copying and token_history_length >= 1:
                    token_copy = deepcopy(token)
                    place_copy.tokens.append(token_copy)
                input_edge_names_to_tokens[edge.argument] = token

        # Read edges (non-consuming). SnapshotEdge -> deep copy (the place is left untouched);
        # MutateEdge -> the live tokens (the transition may modify them in place). Presence is
        # guaranteed by the enabledness check, so place.tokens is non-empty here.
        for edge in (transition_names_to_read_edges or {}).get(transition.name, ()):
            place = place_names_to_nodes[edge.place_node_name]
            argument_type = get_type_hints(transition.function).get(edge.argument)
            if get_origin(argument_type) is list and CompareTypes.between_annotations_where_one_maybe_in_list(
                annotation_not_in_list=place.type,
                annotation_maybe_in_list=argument_type,
            ):
                value = place.tokens  # all tokens, as a list
            else:
                value = place.tokens[0]  # the single token
            input_edge_names_to_tokens[edge.argument] = (
                deepcopy(value) if isinstance(edge, SnapshotEdge) else value
            )

        return input_edge_names_to_tokens, input_places

    def restore_argument_tokens_to_places(
        transition: FunctionTransitionNode,
        input_edge_names_to_tokens: dict[ArgumentName, any],
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
    ) -> None:
        """Put the tokens consumed by stage 1 back into their source places.

        Used when restore_tokens_on_failure is enabled and a firing fails
        after extraction. Single tokens go back to the front of their place
        (they were consumed FIFO from the front) and whole-list extractions
        are prepended, so the pre-firing order is restored. This is
        conservation, not rollback: a body that mutated a token before
        failing leaves it mutated, and external side effects and in-place
        mutation via MutateEdge are not undone — which is why this is opt-in.
        """
        incoming_edges = transition_names_to_incoming_edges.get(transition.name, tuple())
        for edge in incoming_edges:
            if edge.argument not in input_edge_names_to_tokens:
                continue
            place = place_names_to_nodes[edge.place_node_name]
            argument_type = get_type_hints(transition.function).get(edge.argument)
            # Mirror the two extraction cases of stage 1.
            if get_origin(argument_type) is list and CompareTypes.between_annotations_where_one_maybe_in_list(
                annotation_not_in_list=place.type,
                annotation_maybe_in_list=argument_type,
            ):  # All tokens were extracted as a list.
                place.tokens[:0] = input_edge_names_to_tokens[edge.argument]
            else:  # A single token was popped from the front.
                place.tokens.insert(0, input_edge_names_to_tokens[edge.argument])

    def handle_failed_firing(
        transition: FunctionTransitionNode,
        exception: Exception,
        input_edge_names_to_tokens: dict[ArgumentName, any],
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        restore_tokens_on_failure: bool,
    ) -> NoReturn:
        """Raise TransitionFailedError for a firing that failed after stage 1.

        The error carries the tokens consumed from input places (read-edge
        arguments are not consumed, so they are excluded). By default the
        tokens are NOT put back: the body may have mutated them before
        failing, and silently restoring possibly-corrupt tokens to a place is
        worse than a visible loss. With restore_tokens_on_failure=True they
        are restored first (best effort).
        """
        incoming_edges = transition_names_to_incoming_edges.get(transition.name, tuple())
        consumed = {
            edge.argument: input_edge_names_to_tokens[edge.argument]
            for edge in incoming_edges
            if edge.argument in input_edge_names_to_tokens
        }
        restored = False
        if restore_tokens_on_failure:
            ExecutableGraphOperations.restore_argument_tokens_to_places(
                transition=transition,
                input_edge_names_to_tokens=input_edge_names_to_tokens,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                place_names_to_nodes=place_names_to_nodes,
            )
            restored = True
        raise TransitionFailedError(transition.name, consumed, restored) from exception

    async def stage_2_call_transition_function(
        transition: FunctionTransitionNode,
        tokens_kwargs: dict[ArgumentName, any],
        transition_names_to_outgoing_edges: dict[str, tuple[ReturnedEdgeFromTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        allow_token_copying: bool = False,
        executor=None,
    ) -> dict[ListPlaceNode, Any]:
        """Call the transition function and match output tokens to destination places.

        Return a mapping of output places to the tokens to be added to them.

        A synchronous body marked ``execution="thread"`` is run via ``executor`` (a thread pool;
        ``None`` uses the default) so it does not block the event loop. Async bodies already
        yield and run inline regardless.
        """
        if transition.kwargs is not None:
            merged_kwargs = SafeMerge.dictionaries(tokens_kwargs, transition.kwargs)
        else:
            merged_kwargs = tokens_kwargs
        if inspect.iscoroutinefunction(transition.function):
            # Async bodies already yield; "execution" does not apply.
            result = await transition.function(**merged_kwargs)
        elif transition.execution == "thread":
            # Offload a blocking / CPU-bound sync body to a thread so it doesn't freeze the
            # event loop. ``executor=None`` uses the default ThreadPoolExecutor.
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                executor, functools.partial(transition.function, **merged_kwargs)
            )
        else:
            result = transition.function(**merged_kwargs)
        output_place_names_to_tokens: dict[PlaceNodeName, Any] = dict()
        outgoing_edges: tuple[ReturnedEdgeFromTransition, ...] = transition_names_to_outgoing_edges[transition.name]
        if (
            transition.output_distribution_function is None
            and outgoing_edges
            and ExecutableGraphCheck.all_return_indices_are_integers(outgoing_edges)
        ):
            # Indexed routing: the result is a tuple and element i flows along
            # the edge with return_index == i.
            if not isinstance(result, tuple):
                raise TypeError(
                    f"Transition \"{transition.name}\" has indexed return edges but returned "
                    f"{type(result)} instead of a tuple."
                )
            for edge in outgoing_edges:
                if not 0 <= edge.return_index < len(result):
                    raise ValueError(
                        f"Return edge to place \"{edge.place_node_name}\" expects element "
                        f"{edge.return_index} of transition \"{transition.name}\"'s result, "
                        f"which has only {len(result)} elements."
                    )
                output_place_names_to_tokens[edge.place_node_name] = result[edge.return_index]
        elif transition.output_distribution_function is None:
            # Use place types to determine where tokens should go.
            potential_output_places: Iterable[ListPlaceNode] = tuple(
                place_names_to_nodes[edge.place_node_name] for edge in outgoing_edges
            )
            matching_places: Iterable[ListPlaceNode] = ExecutableGraphCheck.value_and_places_types_match(
                result, potential_output_places,
            )
            # An empty list type-matches every place but observably deposits
            # only at list-typed ones, so ambiguity is judged over the places
            # where depositing the value actually does something. For any
            # other value every match is observable and this filter is a
            # no-op.
            observable_places = tuple(
                place for place in matching_places
                if ExecutableGraphCheck.value_disposition_for_place(result, place) != "nothing"
            )
            if len(observable_places) > 1 and not allow_token_copying:
                # Multiple observable destinations but token copying is not allowed.
                raise ValueError(
                    "There are multiple matching destination place nodes but token copying is not allowed. "
                    "Expected only a single output place. To allow the same token to be copied to multiple places, "
                    "set the `allow_token_copying` parameter to True."
                )
            elif len(observable_places) > 1:
                # The token can be copied to each observable destination.
                # Note: actual copying will be handled by add_tokens_to_places
                for place in observable_places:
                    output_place_names_to_tokens[place.name] = result
            elif len(observable_places) == 1:
                # There is a single observable destination.
                output_place_names_to_tokens[observable_places[0].name] = result
            elif not matching_places:
                # No matching places found.
                raise ValueError(
                    f"No output place of transition \"{transition.name}\" matches its result "
                    f"of type {type(result)}."
                )
            # else: the value matched only as a no-op (an empty batch with
            # only scalar-typed candidates) — the firing succeeds and
            # deposits nothing.
        else:  # TODO: create and test separate functions for these two branches.
            # Use the given output distribution function to determine where the tokens should go.
            if not ExecutableGraphCheck.all_return_indices_are_none(outgoing_edges):
                raise ValueError(
                    "Expected all return indices to be None when an output distribution function is used but this is"
                    f" not the case for transition \"{transition.name}\" and outgoing_edges:\n{outgoing_edges}."
                )
            destination_place_names_to_tokens = transition.output_distribution_function(result)
            if not destination_place_names_to_tokens:
                raise ValueError(
                    "Unexpected branch: no output places found for the result of the transition."
                )
            # Token copying (and rejection of multi-place reuse when copying is
            # off) is enforced later in add_tokens_to_places, so the number of
            # destinations and the allow_token_copying flag don't change what we
            # do here: place each (place_name -> token) the distributor produced.
            for place_name, token in destination_place_names_to_tokens.items():
                destination_place = place_names_to_nodes[place_name]
                ExecutableGraphCheck.ensure_token_type_matches_place_type(token, destination_place)
                if token is not None:
                    output_place_names_to_tokens[destination_place.name] = token
        return output_place_names_to_tokens

    def add_tokens_to_places(  # Stage 3
        output_place_names_to_tokens: dict[PlaceNodeName, Any],
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        allow_token_copying: bool = False,
        check_types: bool = True,
    ) -> dict[PlaceNodeName, ListPlaceNode]:
        """Distribute the output tokens to the corresponding places.
        
        Handle token copying if required.
        Handle updating ListPlaceNode with either a single token or a list of tokens.
        Handle updating multiple place nodes with the same token if copying is allowed.
        Handle updating multiple place nodes with different tokens.
        """
        updated_places = {}
        token_usage = {}  # Maps id(token) -> list of place_names where it's used
        need_to_copy = False
        
        # First pass: track token usage
        for place_name, token in output_place_names_to_tokens.items():
            if token is None:
                continue  # Skip None tokens
            token_id = id(token)
            if token_id not in token_usage:
                token_usage[token_id] = []
            token_usage[token_id].append(place_name)
        
        # Second pass: add tokens to places
        for place_name, token in output_place_names_to_tokens.items():
            if token is None:
                continue  # Skip None tokens
            place = place_names_to_nodes[place_name]
            token_id = id(token)
            
            # Determine if we need to copy this token
            if len(token_usage[token_id]) > 1:
                if allow_token_copying:
                    need_to_copy = True
                else:
                    raise RuntimeError(
                        "Token is being added to multiple places but token copying is not allowed.\n"
                        f"Token: {token}, used in places: {token_usage[token_id]}"
                    )
            
            # Get the token to add (copy if needed and not the first usage)
            if need_to_copy and token_usage[token_id].index(place_name) > 0:
                token_or_list_to_add = deepcopy(token)
            else:
                token_or_list_to_add = token

            # Deposit per the value's disposition at this place — batch values
            # extend element-wise, single tokens (including a whole list at a
            # list-typed place) append, an empty batch deposits nothing.
            if check_types:
                ExecutableGraphCheck.ensure_token_type_matches_place_type(token_or_list_to_add, place)
            disposition = ExecutableGraphCheck.value_disposition_for_place(token_or_list_to_add, place)
            if disposition == "batch":
                place.tokens.extend(token_or_list_to_add)
            elif disposition == "token":
                place.tokens.append(token_or_list_to_add)
            updated_places[place_name] = place
        return updated_places

    async def fire_one_transition(
        executable_graph: ExecutableGraph,
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        transition_names_to_outgoing_edges: dict[str, tuple[ReturnedEdgeFromTransition, ...]],
        transition_names_to_read_edges: dict[str, tuple],
        transition_selector: Optional[Callable[[ExecutableGraph, list[FunctionTransitionNode]], Optional[FunctionTransitionNode]]] = None,
        allow_token_copying: Optional[bool] = None,
        restore_tokens_on_failure: Optional[bool] = None,
        executor=None,
        transition_history_length=1,
        place_history_length=1,
        token_history_length=0,
    ) -> Optional[TransitionName]:
        """Fire at most one enabled transition, using prebuilt adjacency maps.

        The single-firing primitive that ``execute_graph`` and the Runner's
        sequential loops compose. The caller owns the per-run setup: building the
        four maps (``MapPlaceNames`` / ``MapTransitionNames``) and validating the
        marking (``ensure_all_token_types_match_place_types``) at whatever cadence
        it wants — so driving an N-firing net pays for that setup once, not once
        per firing. ``last_fired`` is *not* reset here; resetting it per call is
        ``execute_graph``'s contract.

        Selector resolution matches ``execute_graph``: provided > graph's >
        default (highest priority, definition order on ties). ``None`` for
        ``allow_token_copying`` / ``restore_tokens_on_failure`` means use the
        graph's setting.

        Returns the fired transition's name, or None if no transition was enabled
        (or the selector declined every enabled one).
        """
        if allow_token_copying is None:
            allow_token_copying = executable_graph.allow_token_copying
        if restore_tokens_on_failure is None:
            restore_tokens_on_failure = executable_graph.restore_tokens_on_failure
        selector = (
            transition_selector
            or executable_graph.transition_selector
            or default_transition_selector
        )

        # Get all enabled transitions (sufficient tokens and a passing guard), in
        # definition order.
        enabled_transitions = []
        for transition in executable_graph.transitions:
            if ExecutableGraphCheck.transition_is_enabled(
                transition=transition,
                executable_graph=executable_graph,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                place_names_to_nodes=place_names_to_nodes,
                transition_names_to_read_edges=transition_names_to_read_edges,
            ):
                enabled_transitions.append(transition)

        # Let selector choose which transition to fire
        transition = selector(executable_graph, enabled_transitions)
        if transition is None:
            return None

        input_args_to_tokens, input_places = ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
            transition=transition,
            transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            place_names_to_nodes=place_names_to_nodes,
            allow_token_copying=allow_token_copying,
            token_history_length=token_history_length,
            transition_names_to_read_edges=transition_names_to_read_edges,
        )
        try:
            output_place_names_to_tokens = await ExecutableGraphOperations.stage_2_call_transition_function(
                transition=transition,
                tokens_kwargs=input_args_to_tokens,
                transition_names_to_outgoing_edges=transition_names_to_outgoing_edges,
                place_names_to_nodes=place_names_to_nodes,
                allow_token_copying=allow_token_copying,
                executor=executor,
            )
        except Exception as exception:
            # Raises TransitionFailedError carrying the consumed tokens;
            # restores them first when restore_tokens_on_failure is set.
            ExecutableGraphOperations.handle_failed_firing(
                transition=transition,
                exception=exception,
                input_edge_names_to_tokens=input_args_to_tokens,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                place_names_to_nodes=place_names_to_nodes,
                restore_tokens_on_failure=restore_tokens_on_failure,
            )
        updated_places_dict = ExecutableGraphOperations.add_tokens_to_places(
            output_place_names_to_tokens=output_place_names_to_tokens,
            place_names_to_nodes=place_names_to_nodes,
            allow_token_copying=allow_token_copying,
        )
        output_places: Sequence[ListPlaceNode] = list(updated_places_dict.values())

        # Authoritative monotonic counter — never trimmed. See class
        # docstring for the idempotency / replay use case.
        executable_graph.step_count += 1
        # Authoritative "what just fired" — independent of history config.
        executable_graph.last_fired = transition.name
        # Cumulative per-transition tally — monotonic, never trimmed.
        executable_graph.fired_counts[transition.name] = (
            executable_graph.fired_counts.get(transition.name, 0) + 1
        )
        # Update transition history.
        if transition_history_length == 1:
            executable_graph.transition_history = [transition]
        elif transition_history_length > 1:
            executable_graph.transition_history.append(transition)
            if len(executable_graph.transition_history) > transition_history_length:
                executable_graph.transition_history.pop(0)
        # Update place history.
        if place_history_length == 1:
            executable_graph.input_place_history = [input_places]
            executable_graph.output_place_history = [output_places]
        elif place_history_length > 1:
            executable_graph.input_place_history.append(input_places)
            executable_graph.output_place_history.append(output_places)
            if len(executable_graph.input_place_history) > place_history_length:
                executable_graph.input_place_history.pop(0)
                executable_graph.output_place_history.pop(0)
        return transition.name

    async def execute_graph(
        executable_graph: ExecutableGraph,
        stop_after_n_firings: Optional[int] = _UNSET,
        allow_token_copying: Optional[bool] = None,
        restore_tokens_on_failure: Optional[bool] = None,
        verbose=False,
        transition_history_length=1,
        place_history_length=1,
        token_history_length=0,
        transition_selector: Optional[Callable[[ExecutableGraph, list[FunctionTransitionNode]], Optional[FunctionTransitionNode]]] = None,
        executor=None,
        max_transitions: Optional[int] = _UNSET,
    ) -> tuple[ExecutableGraph, int]:
        """Execute the Petri net graph.

        Token copying allows the same token to be output to multiple places via deepcopy.
        By default this uses the graph's allow_token_copying field (which defaults to False),
        but can be overridden by passing an explicit value here.

        Args:
            executable_graph: The graph to execute
            stop_after_n_firings: Stop — without error — after this many firings
                (default 1); None means run until no transition is enabled.
                Disambiguation idiom: a return with fired < stop_after_n_firings
                proves the net quiesced; fired == stop_after_n_firings means it
                may have been cut off mid-pipeline. Resolve by calling again (0
                further firings = it was quiescence), or drive the net through
                ``Runner.run``, whose ``RunSummary.quiesced`` states it
                explicitly.
            allow_token_copying: Whether to allow copying tokens. If None, uses the graph's setting.
            restore_tokens_on_failure: Whether a failed firing puts its consumed tokens
                back (best effort) before TransitionFailedError is raised. If None, uses
                the graph's setting (default False — see the field's comment on
                ExecutableGraph for why restoring is opt-in).
            verbose: Whether to print verbose output
            transition_history_length: Length of transition history to maintain
            place_history_length: Length of place history to maintain
            token_history_length: Length of token history to maintain
            transition_selector: Optional function to select which transition to fire.
                Signature: (graph, enabled_transitions) -> transition_to_fire
                If None, uses graph.transition_selector or default behavior.
            max_transitions: Deprecated alias of ``stop_after_n_firings`` (kept for
                legacy callers; passing it warns).

        Returns:
            Tuple of (updated_graph, transitions_fired_count)
        """
        if max_transitions is not _UNSET:
            if stop_after_n_firings is not _UNSET:
                raise TypeError(
                    "pass stop_after_n_firings only — max_transitions is its deprecated alias"
                )
            warnings.warn(
                "max_transitions is deprecated: use stop_after_n_firings (same meaning — "
                "stop without error after this many firings; None = run to quiescence).",
                DeprecationWarning,
                stacklevel=2,
            )
            stop_after_n_firings = max_transitions
        elif stop_after_n_firings is _UNSET:
            stop_after_n_firings = 1

        if allow_token_copying is None:
            allow_token_copying = executable_graph.allow_token_copying

        if restore_tokens_on_failure is None:
            restore_tokens_on_failure = executable_graph.restore_tokens_on_failure

        if token_history_length > 0 and not allow_token_copying:
            raise ValueError(
                "Token history can only be recorded when token copying is allowed. Adding tokens to the history list "
                "without making a copy means that history will be altered when the tokens are modified by subsequent "
                "transitions."
            )

        transitions_fired = 0
        # Reset per call so it reflects only this invocation (None if nothing fires).
        executable_graph.last_fired = None
        ExecutableGraphCheck.ensure_all_token_types_match_place_types(executable_graph)
        place_names_to_nodes: dict[str, ListPlaceNode] = MapPlaceNames.to_list_place_nodes(executable_graph)
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]] = \
            MapTransitionNames.to_incoming_edges(executable_graph)
        transition_names_to_outgoing_edges: dict[str, tuple[ReturnedEdgeFromTransition, ...]] = \
            MapTransitionNames.to_outgoing_edges(executable_graph)
        transition_names_to_read_edges = MapTransitionNames.to_read_edges(executable_graph)

        while True:
            # stop_after_n_firings=None means run until no transition is enabled.
            if stop_after_n_firings is not None and transitions_fired >= stop_after_n_firings:
                if verbose:
                    print(f"Performed {transitions_fired} transitions, stop_after_n_firings reached.")
                return executable_graph, transitions_fired

            fired_name = await ExecutableGraphOperations.fire_one_transition(
                executable_graph=executable_graph,
                place_names_to_nodes=place_names_to_nodes,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                transition_names_to_outgoing_edges=transition_names_to_outgoing_edges,
                transition_names_to_read_edges=transition_names_to_read_edges,
                transition_selector=transition_selector,
                allow_token_copying=allow_token_copying,
                restore_tokens_on_failure=restore_tokens_on_failure,
                executor=executor,
                transition_history_length=transition_history_length,
                place_history_length=place_history_length,
                token_history_length=token_history_length,
            )
            if fired_name is None:
                if verbose:
                    print(f"Performed {transitions_fired} transitions, no more valid transitions remaining.")
                return executable_graph, transitions_fired
            transitions_fired += 1
