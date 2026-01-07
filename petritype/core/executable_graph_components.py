from copy import deepcopy
from typing import _GenericAlias, _UnionGenericAlias, TypeAliasType
from types import GenericAlias
from typing import Callable, Iterable, Optional, Sequence, Type, Union, Any, get_type_hints, get_origin, get_args
from pydantic import BaseModel, model_validator
import inspect

from petritype.core.data_structures import ArgumentName, FunctionName, KwArgs, PlaceNodeName, ReturnIndex
from petritype.core.type_comparisons import CompareTypes
from petritype.helpers.structures import SafeMerge


type TransitionName = str


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
            if not (
                isinstance(type_of_value, type)  # Changed Type to type (builtin)
                or isinstance(type_of_value, _UnionGenericAlias)
                or isinstance(type_of_value, _GenericAlias)
                or isinstance(type_of_value, GenericAlias)
                or isinstance(type_of_value, TypeAliasType)
            ):
                raise ValueError(
                    f"Expected type to be a Type, _UnionGenericAlias, or _GenericAlias but got: {type_of_value} "
                    f"(type: {type(type_of_value)})"
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
    name: str
    function: Callable
    output_distribution_function: Optional[Callable[[Any], dict[PlaceNodeName, Any]]] = None
    kwargs: Optional[KwArgs] = None


class ArgumentEdgeToTransition(PositionalArgsBaseModel):
    place_node_name: PlaceNodeName
    transition_node_name: FunctionName
    argument: ArgumentName


class ReturnedEdgeFromTransition(PositionalArgsBaseModel):
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
                    # is a ListPlaceNode that holds a list of tokens.
                    annotation2=argument_type,  # This is to allow the case of passing in all the tokens
                    # at once as a list.
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
            place_type = get_type_hints(place).get('type')
            return_type = get_type_hints(transition.function).get('return')
            if place_type is not None and return_type is not None:
                if not CompareTypes.between_annotations_where_one_maybe_in_list(
                    annotation_not_in_list=place_type,  # The place type contains the inner type even if the place
                    # is a ListPlaceNode that holds a list of tokens.
                    annotation_maybe_in_list=return_type,  # All the tokens could be returned at once as a list.
                ):
                    raise TypeError(
                        f"Type mismatch for return edge from transition '{transition.name}' to place "
                        f"'{place.name}': place type '{place_type}' does not match return type "
                        f"'{return_type}'."
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
            if len(place.tokens) == 0:
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
        # Handle the case where we have ListPlaceNode being given a list of tokens of the matching inner type.
        if isinstance(place, ListPlaceNode) and isinstance(token, list):
            inner_type = place.type
            for item in token:
                if not CompareTypes.between_value_and_type(item, inner_type):
                    raise TypeError(
                        f"Expected token item to be of type {inner_type} in {place.name}, got {type(item)}."
                        f"\nToken item: {item}"
                    )
            return
        if not CompareTypes.between_value_and_type(token, place.type):
            raise TypeError(
                f"Expected token to be of type {place.type} in {place.name}, got {type(token)}."
                f"\nToken: {token}"
            )

    def ensure_all_token_types_match_place_types(executable_graph: ExecutableGraph):
        places = (x for x in executable_graph if isinstance(x, ListPlaceNode))
        for place in places:
            for token in place.tokens:
                ExecutableGraphCheck.ensure_token_type_matches_place_type(token, place)

    def return_indices_ara_a_mix_of_none_and_non_none(outgoing_edges: tuple[ReturnedEdgeFromTransition, ...]) -> bool:
        return (
            not ExecutableGraphCheck.all_return_indices_are_none(outgoing_edges)
            and not ExecutableGraphCheck.all_return_indices_are_integers(outgoing_edges)
        )

    def value_and_places_types_match(value: Any, places: Iterable[ListPlaceNode]) -> Iterable[ListPlaceNode]:
        """Find places whose types match the value.
        
        Issues with handling empty lists:
        At run time we can not distinguish the intended type of an empty list's contents.
        Theoretically the intended token type may even be actual empty lists.
        One approach is to somehow require disambiguation at graph construction time.
        However this may be inconvenient and verbose for most use cases.

        Proposed solution: Rely on multiple matches and assume the user knows what they are doing (could be confusing).
        Handles:
        - Direct type matches: value type matches place type
        - List values: Check if list elements match place type
        """
        matching_by_list_contents = []
        matching_by_direct_type = []
        # If value is a list with elements, check if elements match place types
        if isinstance(value, list) and len(value) > 0:
            # Check each element in the list against place types
            for place in places:
                # All elements must match the place type
                if all(CompareTypes.between_value_and_type(item, place.type) for item in value):
                    matching_by_list_contents.append(place)
        elif isinstance(value, list) and len(value) == 0:
            # For empty lists, we can't determine the intended type of contents.
            # So an empty list can actually match any ListPlaceNode regardless of its inner type.
            return tuple(places)
        
        # For non-list values or empty lists, use direct type matching
        for place in places:
            if CompareTypes.between_value_and_type(value, place.type):
                matching_by_direct_type.append(place)
        return tuple(matching_by_direct_type + matching_by_list_contents)


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
            Union[ListPlaceNode, FunctionTransitionNode, ArgumentEdgeToTransition, ReturnedEdgeFromTransition]
        ],
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
    ) -> tuple[dict[ArgumentName, any], Sequence[ListPlaceNode]]:
        """Remove input tokens from source places
        
        Return input tokens matched to function arguments and the input places without the removed tokens.
        """
        # TODO Do we need a way to put the tokens back in case a transition fails?
        incoming_edges: tuple[ArgumentEdgeToTransition, ...] = transition_names_to_incoming_edges[transition.name]
        input_edge_names_to_tokens: dict[ArgumentName, any] = dict()
        input_places = [] # if place_history_length >= 1 else None
        for edge in incoming_edges:
            place = place_names_to_nodes[edge.place_node_name]
            place_copy = place.copy_sans_tokens()
            input_places.append(place_copy)
            argument_type = get_type_hints(transition.function).get(edge.argument)
            place_type = place.type
            # Two cases - passing a single token or passing all tokens as a list.
            print("argument_type:", argument_type   
                  , "place_type:", place_type
                  , "place.tokens:", place.tokens
                  )
            argument_origin = get_origin(argument_type)
            print("argument_origin is list:", argument_origin is list)
            if argument_origin is list and CompareTypes.between_annotations_where_one_maybe_in_list(
                annotation_not_in_list=place_type,
                annotation_maybe_in_list=argument_type,
            ):  # If the argument type is a list and the type inside the list matches the place type,
                # pass all tokens as a list.
                tokens = place.tokens
                print("tokens to pass as list:", tokens)
                place.tokens = []
                if allow_token_copying and token_history_length >= 1:
                    tokens_copy = deepcopy(tokens)
                    place_copy.tokens.extend(tokens_copy)
                input_edge_names_to_tokens[edge.argument] = tokens
            else:  # Pass in a single token.
                token = place.tokens.pop()
                print("token to pass as single:", token)
                # if place_history_length >= 1:
                if allow_token_copying and token_history_length >= 1:
                    token_copy = deepcopy(token)
                    place_copy.tokens.append(token_copy)
                input_edge_names_to_tokens[edge.argument] = token
        return input_edge_names_to_tokens, input_places

    async def stage_2_call_transition_function(
        transition: FunctionTransitionNode,
        tokens_kwargs: dict[ArgumentName, any],
        transition_names_to_outgoing_edges: dict[str, tuple[ReturnedEdgeFromTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        allow_token_copying: bool = False,
    ) -> dict[ListPlaceNode, Any]:
        """Call the transition function and match output tokens to destination places.

        Return a mapping of output places to the tokens to be added to them.
        """
        if transition.kwargs is not None:
            merged_kwargs = SafeMerge.dictionaries(tokens_kwargs, transition.kwargs)
        else:
            merged_kwargs = tokens_kwargs
        if inspect.iscoroutinefunction(transition.function):
            result = await transition.function(**merged_kwargs)
        else:
            result = transition.function(**merged_kwargs)
        output_place_names_to_tokens: dict[PlaceNodeName, Any] = dict()
        outgoing_edges: tuple[ReturnedEdgeFromTransition, ...] = transition_names_to_outgoing_edges[transition.name]
        if transition.output_distribution_function is None:
            # Use place types to determine where tokens should go.
            potential_output_places: Iterable[ListPlaceNode] = tuple(
                place_names_to_nodes[edge.place_node_name] for edge in outgoing_edges
            )
            matching_places: Iterable[ListPlaceNode] = ExecutableGraphCheck.value_and_places_types_match(
                result, potential_output_places,
            )
            if len(matching_places) > 1 and not allow_token_copying:
                # Multiple matching places but token copying is not allowed.
                raise ValueError(
                    "There are multiple matching destination place nodes but token copying is not allowed. "
                    "Expected only a single output place. To allow the same token to be copied to multiple places, "
                    "set the `allow_token_copying` parameter to True."
                )
            elif len(matching_places) > 1 and allow_token_copying:
                # There are multiple matching places and the token can be copied to each of them.
                # Note: actual copying will be handled by add_tokens_to_places
                for place in matching_places:
                    output_place_names_to_tokens[place.name] = result
            elif len(matching_places) == 1:
                # There is a single matching place.
                matching_place = matching_places[0]
                output_place_names_to_tokens[matching_place.name] = result
            else:
                # No matching places found.
                if len(matching_places) == 0:
                    raise ValueError(
                        f"No matching places found for result of transition \"{transition.name}\". "
                        f"Expected a place of type {type(result)}."
                    )
                else:
                    raise ValueError("Unexpected branch...")
        else:  # TODO: create and test separate functions for these two branches.
            # Use the given output distribution function to determine where the tokens should go.
            if not ExecutableGraphCheck.all_return_indices_are_none(outgoing_edges):
                raise ValueError(
                    "Expected all return indices to be None when an output distribution function is used but this is"
                    f" not the case for transition \"{transition.name}\" and outgoing_edges:\n{outgoing_edges}."
                )
            destination_place_names_to_tokens = transition.output_distribution_function(result)
            if len(destination_place_names_to_tokens) > 1 and not allow_token_copying:
            #     raise ValueError(
            #         "Expected only a single output place. To allow the same token to be copied to multiple places, "
            #         "set the `allow_token_copying` parameter to True."
            #     )
                # TODO: Check that none of the tokens refer to the same piece of memory here?
                # Use the distribution function to result to put tokens in the output places.
                for place_name, token in destination_place_names_to_tokens.items():
                    destination_place = place_names_to_nodes[place_name]
                    ExecutableGraphCheck.ensure_token_type_matches_place_type(token, destination_place)
                    if token is not None:
                        output_place_names_to_tokens[destination_place.name] = token
            elif len(destination_place_names_to_tokens) == 1 and not allow_token_copying:
                place_name, token = next(iter(destination_place_names_to_tokens.items()))
                destination_place = place_names_to_nodes[place_name]
                ExecutableGraphCheck.ensure_token_type_matches_place_type(token, destination_place)
                if token is not None:
                    output_place_names_to_tokens[destination_place.name] = token
            elif len(destination_place_names_to_tokens) > 1 and allow_token_copying:
                for place_name, token in destination_place_names_to_tokens.items():
                    destination_place = place_names_to_nodes[place_name]
                    ExecutableGraphCheck.ensure_token_type_matches_place_type(token, destination_place)
                    if token is not None:
                        output_place_names_to_tokens[destination_place.name] = token
            else:
                raise ValueError(
                    "Unexpected branch: no output places found for the result of the transition."
                )
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

            # If token is a list and place type is not a list, extend with list elements. Otherwise, append the token.
            place_type_origin = get_origin(place.type)
            if (
                isinstance(token_or_list_to_add, list)
                and len(token_or_list_to_add) > 0
                and place_type_origin is not list
            ):
                if check_types:  # Check that the place type matches the type of every token in the list.
                    for single_token in token_or_list_to_add:
                        CompareTypes.between_value_and_type(single_token, place.type)
                place.tokens.extend(token_or_list_to_add)
            elif ( # If the token is an empty list and place type is a list, add the empty list as a token.
                isinstance(token_or_list_to_add, list)
                and len(token_or_list_to_add) == 0
                and (place_type_origin is list or place.type is list)
            ):
                place.tokens.append([])
            elif ( # If token is an empty list and place type is not a list, do nothing.
                isinstance(token_or_list_to_add, list)
                and len(token_or_list_to_add) == 0
                and place_type_origin is not list
            ):
                pass
            else:
                if check_types:
                    CompareTypes.between_value_and_type(token_or_list_to_add, place.type)
                place.tokens.append(token_or_list_to_add)
            updated_places[place_name] = place
        return updated_places


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
            # input_history, output_history = await ExecutableGraphOperations.old_fire_transition(
            #     transition=transition,
            #     transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            #     transition_names_to_outgoing_edges=transition_names_to_outgoing_edges,
            #     place_names_to_nodes=place_names_to_nodes,
            #     allow_token_copying=allow_token_copying,
            #     place_history_length=place_history_length,
            #     token_history_length=token_history_length,
            # )
            #
            input_args_to_tokens, input_places = ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
                transition=transition,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                place_names_to_nodes=place_names_to_nodes,
                allow_token_copying=allow_token_copying,
                # place_history_length=place_history_length,
                token_history_length=token_history_length,
            )
            output_place_names_to_tokens = await ExecutableGraphOperations.stage_2_call_transition_function(
                transition=transition,
                tokens_kwargs=input_args_to_tokens,
                transition_names_to_outgoing_edges=transition_names_to_outgoing_edges,
                place_names_to_nodes=place_names_to_nodes,
                allow_token_copying=allow_token_copying,
            )
            updated_places_dict = ExecutableGraphOperations.add_tokens_to_places(
                output_place_names_to_tokens=output_place_names_to_tokens,
                place_names_to_nodes=place_names_to_nodes,
                allow_token_copying=allow_token_copying,
            )
            output_places: Sequence[ListPlaceNode] = list(updated_places_dict.values())

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
                executable_graph.input_place_history = [input_places]
                executable_graph.output_place_history = [output_places]
            elif place_history_length > 1:
                executable_graph.input_place_history.append(input_places)
                executable_graph.output_place_history.append(output_places)
                if len(executable_graph.input_place_history) > place_history_length:
                    executable_graph.input_place_history.pop(0)
                    executable_graph.output_place_history.pop(0)
