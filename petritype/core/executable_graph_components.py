from copy import deepcopy
from typing import _GenericAlias, _UnionGenericAlias, TypeAliasType
from types import GenericAlias
from typing import Callable, Iterable, Optional, Sequence, Type, Union, Any
from pydantic import BaseModel, model_validator
import inspect
from typeguard import TypeCheckError, check_type

from petritype.core.data_structures import ArgumentName, FunctionName, KwArgs, PlaceNodeName, ReturnIndex
from petritype.core.type_comparisons import CompareTypes
from petritype.helpers.structures import SafeMerge


type TransitionName = str


class ListPlaceNode(BaseModel):
    name: PlaceNodeName
    type: Any  # Temporarily accept any value
    values: list[Any] = []
    # TODO: Add validation to check that type matches values

    @model_validator(mode="before")
    def check_type(cls, values):
        type_of_value = values.get('type', None)
        if type_of_value is not None:
            if not (
                isinstance(type_of_value, Type)
                or isinstance(type_of_value, _UnionGenericAlias)
                or isinstance(type_of_value, _GenericAlias)
                or isinstance(type_of_value, GenericAlias)
                or isinstance(type_of_value, TypeAliasType)
            ):
                raise ValueError(
                    f"Expected type to be a Type, _UnionGenericAlias, or _GenericAlias but got: {type_of_value}"
                )
        if type_of_value is None:
            raise NotImplementedError(
                "Unclear at the time of writing what it means for a place node to have no type."
            )
        return values

    def copy_sans_values(self) -> "ListPlaceNode":
        return ListPlaceNode(name=self.name, type=self.type)


class FunctionTransitionNode(BaseModel):
    name: str
    function: Callable
    kwargs: Optional[KwArgs] = None
    output_distribution_function: Optional[Callable[[Any], dict[PlaceNodeName, Any]]] = None


class ArgumentEdgeToTransition(BaseModel):
    place_node_name: PlaceNodeName
    transition_node_name: FunctionName
    argument: ArgumentName


class ReturnedEdgeFromTransition(BaseModel):
    transition_node_name: FunctionName
    place_node_name: PlaceNodeName
    return_index: Optional[ReturnIndex] = None


class ExecutableGraph(BaseModel):
    places: Sequence[ListPlaceNode]
    transitions: Sequence[FunctionTransitionNode]
    argument_edges: Sequence[ArgumentEdgeToTransition]
    return_edges: Sequence[ReturnedEdgeFromTransition]
    transition_history: Sequence[FunctionTransitionNode] = []
    input_place_history: Sequence[ListPlaceNode] = []
    output_place_history: Sequence[ListPlaceNode] = []
    token_history: Sequence[Any] = []


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


class ExecutableGraphCheck:
    """Functions that do not alter the executable graph."""

    def sufficient_tokens_are_available(
        transition: FunctionTransitionNode,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
    ) -> bool:
        incoming_edges: tuple[ArgumentEdgeToTransition, ...] = transition_names_to_incoming_edges[transition.name]
        for edge in incoming_edges:
            place = place_names_to_nodes[edge.place_node_name]
            if len(place.values) == 0:
                return False
        return True

    def next_transition(
        executable_graph: ExecutableGraph,  # Needed so that we know the transition order.
        place_names_to_nodes: dict[str, ListPlaceNode],
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        fire_transitions_last_to_first: bool = True,
    ) -> Optional[FunctionTransitionNode]:
        if fire_transitions_last_to_first:
            transitions = tuple(x for x in reversed(executable_graph.transitions))
        else:
            transitions = tuple(x for x in executable_graph.transitions)
        for transition in transitions:
            if not ExecutableGraphCheck.sufficient_tokens_are_available(
                transition=transition,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                place_names_to_nodes=place_names_to_nodes,
            ):
                continue
            return transition
        return None

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

    def ensure_token_type_matches_place_type(token: any, place: ListPlaceNode):
        try:
            check_type(token, place.type)  # Note: isinstance() argument 2 cannot be a parameterized generic.
        except TypeCheckError as e:
            raise TypeError(
                f"Expected token to be of type {place.type} in {place.name}, got {type(token)}.\nTypeGuard error: {e}"
                f"\nToken: {token}"
            )

    def ensure_all_token_types_match_place_types(executable_graph: ExecutableGraph):
        places = (x for x in executable_graph if isinstance(x, ListPlaceNode))
        for place in places:
            for token in place.values:
                ExecutableGraphCheck.ensure_token_type_matches_place_type(token, place)

    def return_indices_ara_a_mix_of_none_and_non_none(outgoing_edges: tuple[ReturnedEdgeFromTransition, ...]) -> bool:
        return (
            not ExecutableGraphCheck.all_return_indices_are_none(outgoing_edges)
            and not ExecutableGraphCheck.all_return_indices_are_integers(outgoing_edges)
        )

    def value_and_places_types_match(value: Any, places: Iterable[ListPlaceNode]) -> Iterable[ListPlaceNode]:
        bools = (CompareTypes.between_value_and_type(value, place.type) for place in places)
        return tuple(place for place, match in zip(places, bools) if match)


class ExecutableGraphOperations:
    """Functions that alter the executable graph.

    ## Transitions Algorithm

    ### 1. Pick next transition to execute.
    - Iterate over transition nodes.
    - If there are no more transitions that can fire, end the algorithm.
    - Given a transition node, check if it can fire.
        - Are there sufficient input tokens?

    ### 2. Fire the transition.
    - Remove input tokens from places (pop?).
    - Call transition function with the input tokens to generate the output tokens.
    - Add output tokens (append?).
    """

    def construct_graph(
        mixed_nodes_and_edges: Iterable[
            Union[ListPlaceNode, FunctionTransitionNode, ArgumentEdgeToTransition, ReturnedEdgeFromTransition]
        ]
    ) -> ExecutableGraph:
        places, transitions, edges_to, edges_from = [], [], [], []
        for node in mixed_nodes_and_edges:
            if isinstance(node, ListPlaceNode):
                places.append(node)
            elif isinstance(node, FunctionTransitionNode):
                transitions.append(node)
            elif isinstance(node, ArgumentEdgeToTransition):
                edges_to.append(node)
            elif isinstance(node, ReturnedEdgeFromTransition):
                edges_from.append(node)
            else:
                raise ValueError(f"Unexpected node type: {type(node)}")
        return ExecutableGraph(places=places, transitions=transitions, argument_edges=edges_to, return_edges=edges_from)

    def extract_argument_values_from_places(
        transition: FunctionTransitionNode,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        allow_token_copying: bool = False,
        place_history_length: int = 1,
        token_history_length: int = 0,
    ) -> tuple[dict[ArgumentName, any], Optional[Sequence[ListPlaceNode]]]:
        # TODO Do we need a way to put the tokens back in case a transition fails?
        incoming_edges: tuple[ArgumentEdgeToTransition, ...] = transition_names_to_incoming_edges[transition.name]
        out = dict()
        input_places = [] if place_history_length >= 1 else None
        for edge in incoming_edges:
            place = place_names_to_nodes[edge.place_node_name]
            token = place.values.pop()
            if place_history_length >= 1:
                place_copy = place.copy_sans_values()
                input_places.append(place_copy)
                if allow_token_copying and token_history_length >= 1:
                    token_copy = deepcopy(token)
                    place_copy.values.append(token_copy)
            out[edge.argument] = token
        return out, input_places

    async def fire_transition(
        transition: FunctionTransitionNode,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        transition_names_to_outgoing_edges: dict[str, tuple[ReturnedEdgeFromTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        allow_token_copying: bool = False,
        place_history_length: int = 1,
        token_history_length: int = 0,
    ) -> tuple[Optional[Sequence[ListPlaceNode]], Optional[Sequence[ListPlaceNode]]]:
        """TODO: test how this handles missing in/out edges and places.
        TODO: check for kwargs and transition kwargs clashes as part of graph validation before execution.
        """

        if token_history_length >= 1 and not allow_token_copying:
            raise ValueError(
                "Token history can only be recorded when token copying is allowed. Adding tokens to the history list "
                "without making a copy means that history will be altered when the tokens are modified by subsequent "
                "transitions."
            )

        # Pop the input tokens out of the input places.
        outgoing_edges: tuple[ReturnedEdgeFromTransition, ...] = transition_names_to_outgoing_edges[transition.name]
        tokens_kwargs, history_input_places = ExecutableGraphOperations.extract_argument_values_from_places(
            transition=transition,
            transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            place_names_to_nodes=place_names_to_nodes,
            allow_token_copying=allow_token_copying,
            place_history_length=place_history_length,
            token_history_length=token_history_length,
        )

        # Use the information from incoming edges together with tokens & kwargs to execute the transition function.
        if transition.kwargs is not None:
            merged_kwargs = SafeMerge.dictionaries(tokens_kwargs, transition.kwargs)
        else:
            merged_kwargs = tokens_kwargs
        if inspect.iscoroutinefunction(transition.function):
            result = await transition.function(**merged_kwargs)
        else:
            result = transition.function(**merged_kwargs)

        # Distribute the result to the outgoing places.
        history_output_places = [] if place_history_length >= 1 else None
        if transition.output_distribution_function is None:  # Use place types to determine where tokens should go.
            potential_output_places: Iterable[ListPlaceNode] = tuple(
                place_names_to_nodes[edge.place_node_name] for edge in outgoing_edges
            )
            matching_places: Iterable[ListPlaceNode] = ExecutableGraphCheck.value_and_places_types_match(
                result, potential_output_places,
            )
            if len(matching_places) > 1 and not allow_token_copying:
                raise ValueError(
                    "Expected only a single output place. To allow the same token to be copied to multiple places, "
                    "set the `allow_token_copying` parameter to True."
                )
            elif len(matching_places) > 1 and allow_token_copying:
                for place in matching_places:
                    output_token = deepcopy(result)
                    place.values.append(output_token)
                    if place_history_length >= 1:
                        history_place = place.copy_sans_values()
                        history_output_places.append(history_place)
                        if token_history_length >= 1:
                            history_place.values.append(deepcopy(output_token))
            elif len(matching_places) == 1:
                matching_place = next(iter(matching_places))
                matching_place.values.append(result)
                if place_history_length >= 1:
                    history_place = matching_place.copy_sans_values()
                    history_output_places.append(history_place)
                    if token_history_length >= 1 and allow_token_copying:
                        history_place.values.append(deepcopy(result))
            else:
                raise ValueError("Unexpected branch...")
        else:  # Use the given output distribution function to determine where the tokens should go.
            # TODO: determine if we actually want this to be an option.
            if not ExecutableGraphCheck.all_return_indices_are_none(outgoing_edges):
                raise ValueError(
                    "Expected all return indices to be None when an output distribution function is used but this is"
                    f" not the case for transition \"{transition.name}\" and outgoing_edges:\n{outgoing_edges}."
                )
            destination_place_names_to_tokens = transition.output_distribution_function(result)
            if len(destination_place_names_to_tokens) > 1 and not allow_token_copying:
                raise ValueError(
                    "Expected only a single output place. To allow the same token to be copied to multiple places, "
                    "set the `allow_token_copying` parameter to True."
                )
            elif len(destination_place_names_to_tokens) == 1 and not allow_token_copying:
                place_name, token = next(iter(destination_place_names_to_tokens.items()))
                destination_place = place_names_to_nodes[place_name]
                ExecutableGraphCheck.ensure_token_type_matches_place_type(token, destination_place)
                if token is not None:
                    destination_place.values.append(token)
                    if place_history_length >= 1:
                        history_place = destination_place.copy_sans_values()
                        history_output_places.append(history_place)
                        # Token history is not recorded when token copying is not allowed.
            elif len(destination_place_names_to_tokens) > 1 and allow_token_copying:
                for place_name, token in destination_place_names_to_tokens.items():
                    destination_place = place_names_to_nodes[place_name]
                    ExecutableGraphCheck.ensure_token_type_matches_place_type(token, destination_place)
                    if token is not None:
                        destination_place.values.append(token)
                        if place_history_length >= 1:
                            history_place = destination_place.copy_sans_values()
                            history_output_places.append(history_place)
                            if token_history_length >= 1:
                                history_place.values.append(deepcopy(token))
        return history_input_places, history_output_places

    async def execute_graph(
        executable_graph: ExecutableGraph,
        max_transitions: Optional[int] = 1,
        allow_token_copying=False,
        verbose=False,
        transition_history_length=1,
        place_history_length=1,
        token_history_length=0,
    ) -> tuple[ExecutableGraph, int]:
        """
        By default allow_token_copying is set to false because some object may behave in unexpected ways when copied,
        even when using deepcopy.
        """

        if token_history_length > 0 and not allow_token_copying:
            raise ValueError(
                "Token history can only be recorded when token copying is allowed. Adding tokens to the history list "
                "without making a copy means that history will be altered when the tokens are modified by subsequent "
                "transitions."
            )

        transitions_fired = 0
        ExecutableGraphCheck.ensure_all_token_types_match_place_types(executable_graph)
        place_names_to_nodes: dict[str, ListPlaceNode] = MapPlaceNames.to_list_place_nodes(executable_graph)
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]] = \
            MapTransitionNames.to_incoming_edges(executable_graph)
        transition_names_to_outgoing_edges: dict[str, tuple[ReturnedEdgeFromTransition, ...]] = \
            MapTransitionNames.to_outgoing_edges(executable_graph)
        while True:
            if transitions_fired >= max_transitions:
                if verbose:
                    print(f"Performed {transitions_fired} transitions, maximum transitions count reached.")
                return executable_graph, transitions_fired
            transition = ExecutableGraphCheck.next_transition(
                executable_graph=executable_graph,
                place_names_to_nodes=place_names_to_nodes,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges
            )
            if transition is None:
                print(f"Performed {transitions_fired} transitions, no more valid transitions remaining.")
                return executable_graph, transitions_fired
            input_history, output_history = await ExecutableGraphOperations.fire_transition(
                transition=transition,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                transition_names_to_outgoing_edges=transition_names_to_outgoing_edges,
                place_names_to_nodes=place_names_to_nodes,
                allow_token_copying=allow_token_copying,
                place_history_length=place_history_length,
                token_history_length=token_history_length,
            )
            transitions_fired += 1
            # Update transition history.
            if transition_history_length == 1:
                executable_graph.transition_history = [transition]
            elif transition_history_length > 1:
                executable_graph.transition_history.append(transition)
                if len(executable_graph.transition_history) > transition_history_length:
                    executable_graph.transition_history.pop(0)
            # Update place history.
            if place_history_length == 1:
                executable_graph.input_place_history = [input_history]
                executable_graph.output_place_history = [output_history]
            elif place_history_length > 1:
                executable_graph.input_place_history.append(input_history)
                executable_graph.output_place_history.append(output_history)
                if len(executable_graph.input_place_history) > place_history_length:
                    executable_graph.input_place_history.pop(0)
                    executable_graph.output_place_history.pop(0)
