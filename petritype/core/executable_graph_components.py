from copy import deepcopy
from dataclasses import dataclass, field
from typing import TypeAliasType
from types import UnionType
from typing import Callable, Iterable, Literal, NoReturn, Optional, Sequence, Type, Union, Any, get_type_hints, get_origin, get_args
from pydantic import BaseModel, PrivateAttr, model_serializer, model_validator
import asyncio
import functools
import inspect
import itertools
import time
import warnings

from petritype.core.data_structures import ArgumentName, FunctionName, KwArgs, PlaceNodeName, ReturnIndex
from petritype.core.events import (
    Added, Event, Failed, Fired, Mutated, Nested, ParamChanged, PlaceTokenId, Removed, Resynced,
)
from petritype.core.type_comparisons import CompareTypes
from petritype.helpers.structures import SafeMerge


type TransitionName = str

# Distinguishes "argument not passed" from an explicit None (which means unlimited)
# while `max_transitions` remains a deprecated alias of `stop_after_n_firings`.
_UNSET: Any = object()

# The protocol method a token type implements to hold inner nets (``NestedNets``).
NESTED_NETS_METHOD = "__petritype_nets__"

# Every graph built in this process gets the next id: the stable identity of an
# inner net across the moves of the token that holds it (``ExecutableGraph.net_id``).
_net_ids = itertools.count(1)


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

    Restoring is conservation of the consumed tokens, not a rollback of the
    firing: what the body did before failing stays done. In particular a body
    that reached inside a nested token (``NestedNets``) and put tokens into, or
    took them out of, an inner net leaves the inner net as it changed it; only
    the outer tokens go back. A body that reaches inside should do so last, once
    it can no longer fail.
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
    """A place: a typed, FIFO list of tokens.

    ``nested`` declares that its tokens hold inner nets (``NestedNets``): every
    token must be an ``ExecutableGraph`` or implement ``__petritype_nets__``, which
    is checked when tokens are seeded, deposited or injected. The engine then runs
    the inner nets as part of running the outer one, unless the place is also
    ``passive``, in which case they fire only when an outer body steps them.
    """
    name: PlaceNodeName
    type: Any  # Temporarily accept any value  
    tokens: list[Any] = []
    nested: bool = False
    passive: bool = False
    # TODO: Add validation to check that type matches tokens
    # Stable ids of the tokens, parallel to ``tokens`` (same length, same order). Assigned
    # by the graph that holds the place (``TokenIds``); empty until then. Private so that
    # ``tokens`` keeps its type and every existing reader keeps working.
    _token_ids: list[int] = PrivateAttr(default_factory=list)

    @property
    def token_ids(self) -> tuple[int, ...]:
        """Read-only view of the ids of ``tokens``, index for index.

        Only meaningful once the place belongs to an ``ExecutableGraph``. If code
        assigned ``tokens`` directly the two can disagree until the graph's next
        engine entry point resyncs them (``TokenIds.resync``)."""
        return tuple(self._token_ids)

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
        if self.passive and not self.nested:
            raise ValueError(f"Place {self.name!r} is passive but not nested; passive applies to nested places.")
        for token in self.tokens:
            if not CompareTypes.between_value_and_type(token, self.type):
                raise TypeError(
                    f"Expected token to be of type {self.type} in {self.name}, got {type(token)}."
                    f"\nToken: {token}"
                )
            if self.nested:
                NestedNets.ensure_nested_token(token, self)
        return self

    def copy_sans_tokens(self) -> "ListPlaceNode":
        return ListPlaceNode(self.name, self.type, nested=self.nested, passive=self.passive)


# An alias to ListPlaceNode just called PlaceNode.


class FunctionTransitionNode(PositionalArgsBaseModel):
    """A transition node that executes a function when fired.

    Attributes:
        name: Unique identifier for the transition
        function: The function to execute when this transition fires
        output_distribution_function: Optional function to distribute outputs to places
        kwargs: Optional keyword arguments to pass to the function
        guard: Optional (ExecutableGraph) -> bool firing condition, enforced by the
            engine in every execution mode: a transition whose guard returns False is
            not fireable, regardless of available tokens or selector. The guard runs on
            every fireable-discovery sweep, so keep it a cheap, side-effect-free
            predicate over the marking (None costs nothing — the check is skipped).
        priority: Optional (ExecutableGraph) -> float selection hint. The default
            selector fires the highest-priority fireable transition; transitions
            without one score 0.0, and ties go to the first-defined transition (or
            the last-defined, under the graph's ``tie_break="last_declared"``). Only
            called on fireable transitions at selection time.
        activation_function: Deprecated — use ``guard`` (engine-enforced firing condition) or
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
                "firing condition or `priority` for selection ranking. The engine ignores "
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
    snapshot** of the place's tokens, so reading cannot disturb the place. Fireability requires
    the place to be non-empty (presence), but firing does not consume the tokens."""
    place_node_name: PlaceNodeName
    transition_node_name: FunctionName
    argument: ArgumentName


class MutateEdge(PositionalArgsBaseModel):
    """A non-consuming read/write edge (place -> transition). The transition receives the
    **live** tokens and may modify them in place; they stay in the place (not consumed) but may
    be changed. Fireability requires the place to be non-empty (presence)."""
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
            Signature: (graph, fireable_transitions) -> transition_to_fire
            If None, the default selector fires the highest-priority fireable
            transition, breaking ties by ``tie_break``. The selector receives the
            full graph context and list of fireable transitions (in definition
            order), allowing for sophisticated selection strategies (priority-based,
            random, etc.). It is called positionally, so existing selectors whose
            second parameter is named ``enabled`` keep working. Petri net texts say
            "enabled" for what this library calls "fireable"; "enabled" here is the
            on/off switch set by the runtime's ``Enable`` / ``Disable`` commands.
        tie_break: Which fireable transition the default selector fires when several
            share the highest priority (including nets that set no priorities).
            ``"first_declared"`` (default) prefers the first-defined one;
            ``"last_declared"`` prefers the last-defined one. With FIFO places the
            two give different schedules for a pipeline: first-declared processes
            it in batches (every token through stage one before any reaches stage
            two), while last-declared lets each token flow through before the next
            enters. Neither is more correct, so it is a setting, and the default
            keeps the original behaviour. Only the default selector reads it; a
            custom ``transition_selector`` is unaffected.
        next_token_id: The id the next token to enter a place will get. Every token
            gets a stable integer id when it enters a place (seed tokens at
            construction, in place order, from 1; then deposits, ``Extend`` and
            ``SetTokens``). Ids are never reused: a token keeps its id while it rests
            in a place, a consumed token's id is retired, and a token restored after a
            failed firing comes back with its old id. Read them with
            ``place.token_ids``; find one with ``TokenIds.locate``.
        record_events: When True the engine and the runtime append event records
            (``petritype.core.events``) to the graph, read with ``drain_events()``.
            Off by default, so a program that never drains does not grow a list
            for ever. Id bookkeeping happens either way. Inner nets (``NestedNets``)
            follow the outer graph's setting, and their events are drained into
            the outer log wrapped as ``Nested`` records.
        params: Net-level parameters by name, declared at construction (through
            ``construct_graph(params=...)``) and read by guards, priorities and
            selectors through the graph. A body receives a net parameter as a
            keyword argument when it declares a parameter of that name that no
            edge and no ``kwargs`` entry supplies. Set them with ``NetParams.set``,
            which records ``ParamChanged``; the runtime's ``SetNetParam`` command
            and the protocol's ``set_param`` without a transition go through it.
            The inner nets of nested tokens use them for what the outer net
            reaches in to change (a stopped flag, a capacity).
        net_id: This graph's own id, minted from a counter shared by every graph
            built in the process. It is what names an inner net stably across the
            moves of the token that holds it (``Nested.nested_id``): an outer token
            that is taken and put back gets a new token id, the net inside it keeps
            its ``net_id``. A copy of a graph shares its id until re-minted.
        template: The name of the template (``Templates``) that built this graph,
            if one did; what a saved nested token records so the net can be rebuilt.
        disabled: The graph's own transition switches: a transition named here is
            not fireable. Loaded from and saved as the ``enabled`` map of the net's
            JSON form, and the switch an inner net has (an inner net runs without a
            ``RunContext``). The runtime's ``Enable``/``Disable`` commands are the
            runner's switch on the net it drives, kept on ``RunContext.disabled``.

    JSON form: a graph dumps (in JSON mode) as, and validates from, its saved form
    ``{template, marking, params, enabled}`` (``petritype.core.serialisation``),
    so a pydantic token type with an ``ExecutableGraph`` field has the JSON form
    of a nested token (PROTOCOL.md §12.5) with no further work; validating one
    needs the template registered. A deep copy of a graph is a new net with its
    own ``net_id``.
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
    # Names of transitions whose bodies are currently executing: they consumed their inputs but
    # have not yet deposited their outputs. Both modes maintain it. Concurrent mode holds every
    # airborne transition; sequential firing (``fire_one_transition``) holds the one running
    # transition while its body executes and empties the set when the body returns or raises.
    # Writers always assign a fresh set, so a reader on another thread sees a whole value.
    # The renderer lights these "in flight" transitions distinctly.
    in_flight: set[str] = set()
    transition_history: Sequence[FunctionTransitionNode] = []
    input_place_history: Sequence[ListPlaceNode] = []
    output_place_history: Sequence[ListPlaceNode] = []
    token_history: Sequence[Any] = []
    transition_selector: Optional[Callable] = None
    # Tie-break for the default selector among equal-priority fireable transitions:
    # "first_declared" (default) fires the first-defined, "last_declared" the
    # last-defined. FIFO pipelines run in batches under the former and token by
    # token under the latter. Custom selectors ignore it.
    tie_break: Literal["first_declared", "last_declared"] = "first_declared"
    allow_token_copying: bool = False
    # When a firing fails after its input tokens were consumed, put them back
    # (best effort) before raising. Off by default: the body may have mutated
    # the tokens before failing, and silently restoring possibly-corrupt tokens
    # to a place is worse than a visible loss. Enable only when tokens are
    # immutable or bodies do not mutate them before they can fail. Either way
    # the raised TransitionFailedError carries the consumed tokens. Restoring
    # is not a rollback: what a body did inside a nested token's inner nets
    # (NestedNets.put / take / set_param) before failing stays done.
    restore_tokens_on_failure: bool = False
    # Monotonic token-id counter; see the class docstring. Never reset except by
    # constructing a new graph.
    next_token_id: int = 1
    record_events: bool = False
    params: dict[str, Any] = {}
    net_id: int = 0
    template: Optional[str] = None
    disabled: set[str] = set()
    # Event records waiting to be drained; never dumped with the model.
    _events: list[Event] = PrivateAttr(default_factory=list)

    @model_validator(mode="wrap")
    @classmethod
    def _from_saved_form(cls, value, handler):
        """A saved form (``{template, marking, ...}``) validates to the net it
        describes, rebuilt from its template; anything else validates as the
        graph's own fields."""
        from petritype.core.serialisation import NetJson  # the module imports this one
        if NetJson.looks_saved(value):
            try:
                return NetJson.restore(value)
            except (LookupError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"not a saved net: {error}") from error
        return handler(value)

    @model_serializer(mode="wrap")
    def _as_saved_form(self, handler, info):
        """In JSON mode (``model_dump_json``, ``model_dump(mode="json")``, a
        pydantic token holding a graph) the saved form; in Python mode the fields,
        as before."""
        if info.mode == "json":
            from petritype.core.serialisation import NetJson
            return NetJson.save(self)
        return handler(self)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """The schema of the saved form: the one JSON shape a graph has (its
        fields hold callables, which have none)."""
        return {
            "title": cls.__name__,
            "description": "A net in its saved form: the template that rebuilds it, its marking by "
                           "place, its parameters and its transition switches (PROTOCOL.md §12.5).",
            "type": "object",
            "properties": {
                "template": {"type": "string"},
                "marking": {"type": "object", "additionalProperties": {"type": "array"}},
                "params": {"type": "object"},
                "enabled": {"type": "object", "additionalProperties": {"type": "boolean"}},
            },
            "required": ["marking"],
        }

    def __deepcopy__(self, memo=None):
        """A deep copy is a new net: it gets its own ``net_id``, so a forked nested
        token is told apart from the original."""
        copy = super().__deepcopy__(memo)
        copy.net_id = next(_net_ids)
        return copy

    def export_marking(self) -> dict[str, list]:
        """``{place: [tokens as JSON]}``, oldest first (``NetJson.export_marking``)."""
        from petritype.core.serialisation import NetJson
        return NetJson.export_marking(self)

    def import_marking(self, marking: dict[str, list]) -> None:
        """Replace the marking from ``{place: [tokens as JSON]}``; a place not
        listed loads empty, an unknown place is an error, ids are not preserved
        (``NetJson.import_marking``)."""
        from petritype.core.serialisation import NetJson
        NetJson.import_marking(self, marking)

    def model_post_init(self, context: Any, /) -> None:
        # Seed tokens get their ids here, in place order. Runs once per construction
        # (not on model_copy), after pydantic has initialised the private attributes.
        self.net_id = next(_net_ids)
        TokenIds.seed(self)
        NestedNets.sync_recording(self)

    def drain_events(self) -> list[Event]:
        """Return the recorded events, oldest first, and clear them. Empty unless
        ``record_events`` was on while they happened. The events of inner nets
        (``NestedNets``) that have not yet reached this log are gathered first,
        wrapped as ``Nested``, so the result is complete; they come after whatever
        this graph recorded before they were gathered."""
        NestedNets.drain(self)
        events, self._events = self._events, []
        return events

    def place_named(self, name: str) -> Optional[ListPlaceNode]:
        place_names_to_nodes = {place.name: place for place in self.places}  # TODO: do we need to check every time?
        if len(set(place_names_to_nodes.keys())) != len(place_names_to_nodes.keys()):
            raise ValueError("Duplicate place names found!")
        return place_names_to_nodes.get(name)

    def transition_named(self, name: str) -> Optional[FunctionTransitionNode]:
        for transition in self.transitions:
            if transition.name == name:
                return transition
        return None

    def __petritype_nets__(self) -> dict[str, "ExecutableGraph"]:
        """A graph used as a token holds itself as its one inner net, unnamed."""
        return {"": self}

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


@dataclass
class FiringTokenIds:
    """Which token ids one firing touched. Passive state, filled in as the firing
    goes: stage 1 records what it consumed (per argument, in the order taken) and
    what it handed over through mutate edges; stage 3 records what it deposited.
    ``restore_argument_tokens_to_places`` reads ``consumed`` to put the same ids back.
    """

    consumed: dict[ArgumentName, tuple[PlaceNodeName, list[int]]] = field(default_factory=dict)
    mutated: list[PlaceTokenId] = field(default_factory=list)
    produced: list[PlaceTokenId] = field(default_factory=list)


class TokenIds:
    """Functions that keep each place's token ids in step with its tokens.

    Every token in a place has a stable integer id (``ListPlaceNode.token_ids``,
    parallel to ``tokens``) drawn from ``ExecutableGraph.next_token_id``. The engine
    moves ids together with tokens; code that assigns or resizes ``place.tokens``
    directly bypasses them, which ``resync`` detects and repairs at each engine
    entry point.
    """

    def allocate(executable_graph: ExecutableGraph, count: int) -> list[int]:
        """Take ``count`` fresh ids from the graph's counter."""
        start = executable_graph.next_token_id
        executable_graph.next_token_id = start + count
        return list(range(start, start + count))

    def seed(executable_graph: ExecutableGraph) -> None:
        """Give every token already in a place a fresh id, place by place in
        declaration order. Called once, when the graph is constructed."""
        for place in executable_graph.places:
            place._token_ids = TokenIds.allocate(executable_graph, len(place.tokens))

    def resync(executable_graph: ExecutableGraph) -> tuple[PlaceNodeName, ...]:
        """Re-id every place whose token list and id list differ in length, and
        record a ``Resynced`` event for each. Returns the names of the places re-ided.

        A length check is all it can do: code that swaps a token for another in
        place, keeping the length, goes unseen and the old id names the new token.
        Called at every engine entry point (each firing, each command applied, each
        concurrent-loop sweep, each ``locate``); cheap, one ``len`` pair per place.
        """
        resynced = []
        for place in executable_graph.places:
            if len(place._token_ids) != len(place.tokens):
                place._token_ids = TokenIds.allocate(executable_graph, len(place.tokens))
                GraphEvents.record(executable_graph, Resynced(place.name))
                resynced.append(place.name)
        return tuple(resynced)

    def append(executable_graph: ExecutableGraph, place: ListPlaceNode, tokens: list) -> list[int]:
        """Append ``tokens`` to ``place`` with fresh ids; return the ids."""
        ids = TokenIds.allocate(executable_graph, len(tokens))
        place.tokens.extend(tokens)
        place._token_ids.extend(ids)
        return ids

    def locate(executable_graph: ExecutableGraph, token_id: int) -> Optional[tuple[PlaceNodeName, Any]]:
        """``(place_name, token)`` for the live token with this id, or None if no
        place holds it (consumed, removed, or never issued). Resyncs first, so a
        place whose tokens were assigned directly answers with its fresh ids."""
        TokenIds.resync(executable_graph)
        for place in executable_graph.places:
            for index, candidate in enumerate(place._token_ids):
                if candidate == token_id:
                    return place.name, place.tokens[index]
        return None


class GraphEvents:
    """Functions that append event records (``petritype.core.events``) to a graph.
    Every append goes through ``record``, which does nothing unless
    ``executable_graph.record_events`` is on."""

    def record(executable_graph: ExecutableGraph, event: Event) -> None:
        if executable_graph.record_events:
            executable_graph._events.append(event)

    def consumed_pairs(firing_ids: FiringTokenIds) -> tuple[PlaceTokenId, ...]:
        """The consumed ids as ``(place, id)`` pairs, in the order they were taken."""
        return tuple(
            (place_name, token_id)
            for place_name, ids in firing_ids.consumed.values()
            for token_id in ids
        )

    def record_mutations(
        executable_graph: ExecutableGraph,
        firing_ids: FiringTokenIds,
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
    ) -> None:
        """One ``Mutated`` per token handed over through a mutate edge that is still
        in its place (in concurrent mode a peer may have consumed it meanwhile)."""
        for place_name, token_id in firing_ids.mutated:
            if token_id in place_names_to_nodes[place_name]._token_ids:
                GraphEvents.record(executable_graph, Mutated(place_name, token_id))

    def record_fired(
        executable_graph: ExecutableGraph,
        transition_name: TransitionName,
        firing_ids: FiringTokenIds,
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        duration_ms: float,
    ) -> None:
        """A firing completed: the inner events its body caused (``NestedNets``),
        its ``Mutated`` records, then its ``Fired``."""
        if not executable_graph.record_events:
            return
        NestedNets.drain(executable_graph)
        GraphEvents.record_mutations(executable_graph, firing_ids, place_names_to_nodes)
        GraphEvents.record(executable_graph, Fired(
            transition=transition_name,
            consumed=GraphEvents.consumed_pairs(firing_ids),
            produced=tuple(firing_ids.produced),
            duration_ms=duration_ms,
        ))

    def record_failed(
        executable_graph: ExecutableGraph,
        transition_name: TransitionName,
        firing_ids: FiringTokenIds,
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        restored: bool,
        exception: BaseException,
    ) -> None:
        """A firing failed: the inner events its body caused, its ``Mutated``
        records, ``Added`` for anything a failed deposit had already placed, then
        its ``Failed``."""
        if not executable_graph.record_events:
            return
        NestedNets.drain(executable_graph)
        GraphEvents.record_mutations(executable_graph, firing_ids, place_names_to_nodes)
        for place_name, token_id in firing_ids.produced:
            GraphEvents.record(executable_graph, Added(place_name, token_id))
        GraphEvents.record(executable_graph, Failed(
            transition=transition_name,
            consumed=GraphEvents.consumed_pairs(firing_ids),
            restored=restored,
            error=f"{type(exception).__name__}: {exception}",
        ))


class NetParams:
    """Functions over a graph's net-level parameters (``ExecutableGraph.params``)."""

    def set(executable_graph: ExecutableGraph, name: str, value: Any) -> None:
        """Set a declared parameter and record ``ParamChanged(None, name)``. A name
        the net did not declare at construction is a ``KeyError``: parameters are
        part of what a net is, not a free store."""
        if name not in executable_graph.params:
            raise KeyError(f"the net declares no parameter {name!r}")
        executable_graph.params[name] = value
        GraphEvents.record(executable_graph, ParamChanged(None, name))


class NestedNets:
    """Functions over tokens that hold Petri nets of their own (nets as tokens).

    A token of a place declared ``nested=True`` must be an ``ExecutableGraph`` (its
    one inner net is unnamed, ``""``) or implement ``__petritype_nets__(self) ->
    dict[str, ExecutableGraph]`` naming the nets it holds. The outer net then:

    - runs the inner nets as part of running itself (``fire_one_inner``, which
      ``fire_one_transition`` falls back to when the outer net has nothing to fire:
      place order, then token order, then net order, descending before moving on),
      unless the place is ``passive``;
    - gathers their events into its own log as ``Nested`` records (``drain``), with
      the path to the net they happened in and the net's stable ``net_id``;
    - lets an outer body reach inside the tokens it holds (``put``, ``take``,
      ``set_param``), which is the only coupling: no place is shared, no inner
      transition sees the outer net.

    Paths are tuples of segments, three per level: the place, the holding token's
    id as ``#<id>``, the net's name within the token.
    """

    def of(token: Any) -> dict[str, ExecutableGraph]:
        """The inner nets of ``token`` by name, or ``TypeError`` if it holds none."""
        method = getattr(token, NESTED_NETS_METHOD, None)
        if method is None:
            raise TypeError(
                f"{type(token).__name__} holds no nets: a token of a nested place must be an "
                f"ExecutableGraph or implement {NESTED_NETS_METHOD}()"
            )
        nets = method()
        if not isinstance(nets, dict) or not all(
            isinstance(name, str) and isinstance(net, ExecutableGraph) for name, net in nets.items()
        ):
            raise TypeError(
                f"{type(token).__name__}.{NESTED_NETS_METHOD}() must return a dict of str to ExecutableGraph"
            )
        return nets

    def ensure_nested_token(token: Any, place: ListPlaceNode) -> None:
        """Raise ``TypeError`` naming the place if ``token`` cannot sit in the nested ``place``."""
        try:
            NestedNets.of(token)
        except TypeError as error:
            raise TypeError(f"Place {place.name!r} is nested but its token cannot hold nets: {error}") from error

    def any_nested(executable_graph: ExecutableGraph) -> bool:
        return any(place.nested for place in executable_graph.places)

    def walk(executable_graph: ExecutableGraph, passive: bool = True):
        """Yield ``(path, token, name, inner)`` for every inner net one level down,
        in place order, token order, net order; ``passive=False`` skips passive
        places. The path is as the marking stands now."""
        for place in executable_graph.places:
            if not place.nested or (place.passive and not passive):
                continue
            ids = place._token_ids
            for index, token in enumerate(list(place.tokens)):
                token_id = f"#{ids[index]}" if index < len(ids) else "#?"
                for name, inner in NestedNets.of(token).items():
                    yield (place.name, token_id, name), token, name, inner

    def sync_recording(executable_graph: ExecutableGraph) -> None:
        """Make every inner net record events exactly when the outer one does."""
        if not NestedNets.any_nested(executable_graph):
            return
        for _path, _token, _name, inner in NestedNets.walk(executable_graph):
            if inner.record_events != executable_graph.record_events:
                inner.record_events = executable_graph.record_events
                NestedNets.sync_recording(inner)

    def drain(executable_graph: ExecutableGraph) -> None:
        """Gather the undrained events of every inner net into this graph's log,
        each wrapped as ``Nested`` with the path to the net it happened in. An
        inner net's own ``Nested`` records (from nets inside it) are re-based onto
        the longer path, so there is one wrapper whatever the depth. Called by
        ``drain_events`` and before a firing is recorded, so inner events land in
        the outer log in the order they happened."""
        if not NestedNets.any_nested(executable_graph):
            return
        for path, _token, _name, inner in NestedNets.walk(executable_graph):
            inner.record_events = executable_graph.record_events
            for event in inner.drain_events():
                if isinstance(event, Nested):
                    event = Nested(path + event.path, event.nested_id, event.event)
                else:
                    event = Nested(path, inner.net_id, event)
                GraphEvents.record(executable_graph, event)

    def resolve(executable_graph: ExecutableGraph, path: Sequence[str]) -> ExecutableGraph:
        """The inner net at ``path`` (segments as ``walk`` yields them), or
        ``LookupError`` naming the segment that failed."""
        graph = executable_graph
        segments = list(path)
        if len(segments) % 3 != 0:
            raise LookupError(f"a nested path has three segments per level, not {len(segments)}")
        for level in range(0, len(segments), 3):
            place_name, token_segment, name = segments[level:level + 3]
            place = graph.place_named(place_name)
            if place is None or not place.nested:
                raise LookupError(f"no nested place {place_name!r}")
            if not token_segment.startswith("#"):
                raise LookupError(f"a token segment is spelt #<id>, not {token_segment!r}")
            TokenIds.resync(graph)
            try:
                index = list(place._token_ids).index(int(token_segment[1:]))
            except ValueError:
                raise LookupError(f"no token {token_segment} in {place_name!r}") from None
            nets = NestedNets.of(place.tokens[index])
            if name not in nets:
                raise LookupError(f"token {token_segment} in {place_name!r} holds no net named {name!r}")
            graph = nets[name]
        return graph

    def find(executable_graph: ExecutableGraph, net_id: int) -> Optional[tuple[tuple[str, ...], ExecutableGraph]]:
        """``(path, inner)`` for the inner net with this ``net_id``, at any depth, or
        ``None``; the path is as the marking stands now."""
        for path, _token, _name, inner in NestedNets.walk(executable_graph):
            if inner.net_id == net_id:
                return path, inner
            found = NestedNets.find(inner, net_id)
            if found is not None:
                return path + found[0], found[1]
        return None

    def any_fireable(executable_graph: ExecutableGraph) -> bool:
        """Whether any transition of any non-passive inner net, at any depth, can fire now."""
        for _path, _token, _name, inner in NestedNets.walk(executable_graph, passive=False):
            place_nodes, incoming, _outgoing, read = ExecutableGraphOperations.maps(inner)
            if any(
                ExecutableGraphCheck.transition_is_fireable(t, inner, incoming, place_nodes, read)
                for t in inner.transitions
            ) or NestedNets.any_fireable(inner):
                return True
        return False

    async def fire_one_inner(
        executable_graph: ExecutableGraph,
        executor=None,
        allow_token_copying: Optional[bool] = None,
        restore_tokens_on_failure: Optional[bool] = None,
    ) -> Optional[str]:
        """Fire at most one transition of one inner net: the first that fires, in
        place order, token order, net order, each net by its own selector and
        descending into its own inner nets before the next token is tried. Passive
        places are skipped. Returns the firing's path and transition joined by
        ``/`` (``"Depot/#17/cabin/Sit"``), which the outer graph counts as a firing
        of its own under that name, or ``None``. The inner net's events are drained
        into the outer log at once."""
        for path, _token, _name, inner in NestedNets.walk(executable_graph, passive=False):
            inner.record_events = executable_graph.record_events
            place_nodes, incoming, outgoing, read = ExecutableGraphOperations.maps(inner)
            fired = await ExecutableGraphOperations.fire_one_transition(
                executable_graph=inner,
                place_names_to_nodes=place_nodes,
                transition_names_to_incoming_edges=incoming,
                transition_names_to_outgoing_edges=outgoing,
                transition_names_to_read_edges=read,
                allow_token_copying=allow_token_copying,
                restore_tokens_on_failure=restore_tokens_on_failure,
                executor=executor,
            )
            if fired is not None:
                NestedNets.drain(executable_graph)
                return "/".join((*path, fired))
        return None

    # --- reaching inside, for outer bodies ---

    def put(inner: ExecutableGraph, place_name: str, tokens: list) -> list[int]:
        """Append ``tokens`` to a place of an inner net, type-checked, with fresh
        ids, recording ``Added`` for each; returns the ids."""
        place = inner.place_named(place_name)
        if place is None:
            raise KeyError(f"the inner net has no place {place_name!r}")
        TokenIds.resync(inner)
        for token in tokens:
            ExecutableGraphCheck.ensure_resident_token_type_matches_place_type(token, place)
        ids = TokenIds.append(inner, place, list(tokens))
        for token_id in ids:
            GraphEvents.record(inner, Added(place.name, token_id))
        return ids

    def take(inner: ExecutableGraph, place_name: str, count: int = 1) -> list:
        """Remove and return the ``count`` oldest tokens of a place of an inner net,
        recording ``Removed`` for each. Fewer than ``count`` present is an error
        (nothing is taken)."""
        place = inner.place_named(place_name)
        if place is None:
            raise KeyError(f"the inner net has no place {place_name!r}")
        TokenIds.resync(inner)
        if len(place.tokens) < count:
            raise ValueError(f"{place_name!r} holds {len(place.tokens)} tokens, not {count}")
        taken, ids = place.tokens[:count], place._token_ids[:count]
        del place.tokens[:count]
        del place._token_ids[:count]
        for token_id in ids:
            GraphEvents.record(inner, Removed(place.name, token_id))
        return taken

    def set_param(inner: ExecutableGraph, name: str, value: Any) -> None:
        """``NetParams.set`` on an inner net."""
        NetParams.set(inner, name, value)

    def holds_nets(token: Any) -> bool:
        """Whether ``token`` is a net or implements the nested-nets protocol."""
        return callable(getattr(token, NESTED_NETS_METHOD, None))

    def summary(token: Any, maximum_length: int = 100) -> str:
        """A one-line summary of a nested token for a label: its own fields, with
        each inner net reduced to its name and token count (``Vehicle(name='Bus
        1', kind='Bus', destination=None) ⊂ cabin: 3``), so a renderer can mark
        a token that contains a net without printing the net. A token that is a
        net itself reads ``net: 3``. Cut at ``maximum_length`` with ``...``."""
        nets = NestedNets.of(token)
        counts = ", ".join(
            f"{name or 'net'}: {sum(len(place.tokens) for place in inner.places)}" for name, inner in nets.items()
        )
        if isinstance(token, ExecutableGraph):
            text = counts
        else:
            if isinstance(token, BaseModel):
                fields = ", ".join(
                    f"{name}={value!r}" for name, value in token
                    if not isinstance(value, ExecutableGraph)
                )
                own = f"{type(token).__name__}({fields})"
            else:
                own = type(token).__name__
            text = f"{own} \u2282 {counts}"
        if len(text) > maximum_length:
            text = text[:maximum_length] + "..."
        return text

    def fork(token: Any) -> Any:
        """A copy of a nested token whose inner nets are new nets: a deep copy,
        which gives every graph in it its own ``net_id`` (same markings,
        parameters and switches; nothing shared with the original). The clone of
        a nested token the design asks for, done by copying rather than by
        rebuilding from the template and importing the marking."""
        return deepcopy(token)


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

    def transition_is_fireable(
        transition: FunctionTransitionNode,
        executable_graph: ExecutableGraph,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        transition_names_to_read_edges: Optional[dict[str, tuple]] = None,
    ) -> bool:
        """Can fire now: not switched off on the graph (``disabled``), sufficient
        tokens, and the transition's guard, if any.

        Every fireable-discovery sweep (sequential execution and both concurrent runner
        loops) must go through this, so a guard means the same thing in every execution
        mode. Guards run on every sweep — they should be cheap, side-effect-free
        predicates over the marking.
        """
        if transition.name in executable_graph.disabled:
            return False
        if not ExecutableGraphCheck.sufficient_tokens_are_available(
            transition=transition,
            transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            place_names_to_nodes=place_names_to_nodes,
            transition_names_to_read_edges=transition_names_to_read_edges,
        ):
            return False
        return transition.guard is None or bool(transition.guard(executable_graph))

    def transition_is_enabled(
        transition: FunctionTransitionNode,
        executable_graph: ExecutableGraph,
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        transition_names_to_read_edges: Optional[dict[str, tuple]] = None,
    ) -> bool:
        """Deprecated alias of ``transition_is_fireable``; warns and delegates.

        Renamed so that "enabled" means only the on/off switch (``Enable`` / ``Disable``).
        """
        warnings.warn(
            "ExecutableGraphCheck.transition_is_enabled is deprecated: use "
            "ExecutableGraphCheck.transition_is_fireable instead (\"enabled\" now means "
            "only the Enable/Disable switch).",
            DeprecationWarning,
            stacklevel=2,
        )
        return ExecutableGraphCheck.transition_is_fireable(
            transition=transition,
            executable_graph=executable_graph,
            transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            place_names_to_nodes=place_names_to_nodes,
            transition_names_to_read_edges=transition_names_to_read_edges,
        )

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
                if place.nested:
                    NestedNets.ensure_nested_token(item, place)
        elif disposition == "token":
            if not CompareTypes.between_value_and_type(token, place.type):
                raise TypeError(
                    f"Expected token to be of type {place.type} in {place.name}, got {type(token)}."
                    f"\nToken: {token}"
                )
            if place.nested:
                NestedNets.ensure_nested_token(token, place)
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
        if place.nested:
            NestedNets.ensure_nested_token(token, place)

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
# priority — go to the first-defined transition; for `tie_break="last_declared"`
# we scan the fireable list reversed, so the last-defined one is that first maximum.
def default_transition_selector(
    graph: ExecutableGraph, fireable: list[FunctionTransitionNode]
) -> Optional[FunctionTransitionNode]:
    """Default selector: highest-priority fireable transition, ties broken by ``graph.tie_break``."""
    if not fireable:
        return None
    candidates = reversed(fireable) if graph.tie_break == "last_declared" else fireable
    return max(candidates, key=lambda t: t.priority(graph) if t.priority is not None else 0.0)


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
        tie_break: Literal["first_declared", "last_declared"] = "first_declared",
        record_events: bool = False,
        params: Optional[dict[str, Any]] = None,
        template: Optional[str] = None,
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
            restore_tokens_on_failure=restore_tokens_on_failure, tie_break=tie_break,
            record_events=record_events, params=dict(params or {}), template=template,
        )
        # Declares "this net is a DAG" at build time; an accidental cycle then
        # fails here with its path named instead of hanging at run time.
        if expect_acyclic:
            ExecutableGraphCheck.assert_acyclic(graph)
        return graph

    def maps(executable_graph: ExecutableGraph) -> tuple[dict, dict, dict, dict]:
        """The four adjacency maps ``fire_one_transition`` fires on:
        ``(place_nodes, incoming, outgoing, read)``. No validation."""
        return (
            MapPlaceNames.to_list_place_nodes(executable_graph),
            MapTransitionNames.to_incoming_edges(executable_graph),
            MapTransitionNames.to_outgoing_edges(executable_graph),
            MapTransitionNames.to_read_edges(executable_graph),
        )

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
        firing_ids: Optional[FiringTokenIds] = None,
    ) -> tuple[dict[ArgumentName, any], Sequence[ListPlaceNode]]:
        """Remove input tokens from source places
        
        Return input tokens matched to function arguments and the input places without the removed tokens.

        Token ids leave with their tokens. If ``firing_ids`` is given, the ids taken
        (per argument, in order) and the ids handed over through mutate edges are
        recorded on it; snapshot edges touch no ids.

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
                taken_ids, place._token_ids = place._token_ids, []
                if allow_token_copying and token_history_length >= 1:
                    tokens_copy = deepcopy(tokens)
                    place_copy.tokens.extend(tokens_copy)
                input_edge_names_to_tokens[edge.argument] = tokens
            else:  # Pass in a single token (FIFO: consume the oldest token first).
                token = place.tokens.pop(0)
                taken_ids = place._token_ids[:1]
                del place._token_ids[:1]
                # if place_history_length >= 1:
                if allow_token_copying and token_history_length >= 1:
                    token_copy = deepcopy(token)
                    place_copy.tokens.append(token_copy)
                input_edge_names_to_tokens[edge.argument] = token
            if firing_ids is not None:
                firing_ids.consumed[edge.argument] = (place.name, taken_ids)

        # Read edges (non-consuming). SnapshotEdge -> deep copy (the place is left untouched);
        # MutateEdge -> the live tokens (the transition may modify them in place). Presence is
        # guaranteed by the fireability check, so place.tokens is non-empty here.
        for edge in (transition_names_to_read_edges or {}).get(transition.name, ()):
            place = place_names_to_nodes[edge.place_node_name]
            argument_type = get_type_hints(transition.function).get(edge.argument)
            if get_origin(argument_type) is list and CompareTypes.between_annotations_where_one_maybe_in_list(
                annotation_not_in_list=place.type,
                annotation_maybe_in_list=argument_type,
            ):
                value = place.tokens  # all tokens, as a list
                read_ids = list(place._token_ids)
            else:
                value = place.tokens[0]  # the single token
                read_ids = place._token_ids[:1]
            if firing_ids is not None and isinstance(edge, MutateEdge):
                firing_ids.mutated.extend((place.name, token_id) for token_id in read_ids)
            input_edge_names_to_tokens[edge.argument] = (
                deepcopy(value) if isinstance(edge, SnapshotEdge) else value
            )

        return input_edge_names_to_tokens, input_places

    def restore_argument_tokens_to_places(
        transition: FunctionTransitionNode,
        input_edge_names_to_tokens: dict[ArgumentName, any],
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        firing_ids: Optional[FiringTokenIds] = None,
    ) -> None:
        """Put the tokens consumed by stage 1 back into their source places.

        With ``firing_ids`` (as filled by stage 1) the tokens get their old ids back,
        at the front, in the order taken. Without it the ids are not restored and the
        graph's next ``TokenIds.resync`` re-ids those places.

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
            if firing_ids is not None and edge.argument in firing_ids.consumed:
                place._token_ids[:0] = firing_ids.consumed[edge.argument][1]

    def handle_failed_firing(
        transition: FunctionTransitionNode,
        exception: Exception,
        input_edge_names_to_tokens: dict[ArgumentName, any],
        transition_names_to_incoming_edges: dict[str, tuple[ArgumentEdgeToTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        restore_tokens_on_failure: bool,
        executable_graph: Optional[ExecutableGraph] = None,
        firing_ids: Optional[FiringTokenIds] = None,
    ) -> NoReturn:
        """Raise TransitionFailedError for a firing that failed after stage 1.

        The error carries the tokens consumed from input places (read-edge
        arguments are not consumed, so they are excluded). By default the
        tokens are NOT put back: the body may have mutated them before
        failing, and silently restoring possibly-corrupt tokens to a place is
        worse than a visible loss. With restore_tokens_on_failure=True they
        are restored first (best effort).

        Given the graph and the firing's ids, the restored tokens get their old ids
        back and the firing's ``Mutated`` and ``Failed`` events are recorded.
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
                firing_ids=firing_ids,
            )
            restored = True
        if executable_graph is not None and firing_ids is not None:
            GraphEvents.record_failed(
                executable_graph, transition.name, firing_ids, place_names_to_nodes, restored, exception,
            )
        raise TransitionFailedError(transition.name, consumed, restored) from exception

    async def stage_2_call_transition_function(
        transition: FunctionTransitionNode,
        tokens_kwargs: dict[ArgumentName, any],
        transition_names_to_outgoing_edges: dict[str, tuple[ReturnedEdgeFromTransition, ...]],
        place_names_to_nodes: dict[str, ListPlaceNode],
        allow_token_copying: bool = False,
        executor=None,
        net_params: Optional[dict[str, Any]] = None,
    ) -> dict[ListPlaceNode, Any]:
        """Call the transition function and match output tokens to destination places.

        Return a mapping of output places to the tokens to be added to them.

        A synchronous body marked ``execution="thread"`` is run via ``executor`` (a thread pool;
        ``None`` uses the default) so it does not block the event loop. Async bodies already
        yield and run inline regardless.

        ``net_params`` (the graph's ``params``) are passed as keyword arguments to a
        body that declares a parameter of the same name that neither an edge nor
        the transition's own ``kwargs`` supplies. A transition with no output edge
        is a sink: its body must return ``None`` and nothing is deposited.
        """
        if transition.kwargs is not None:
            merged_kwargs = SafeMerge.dictionaries(tokens_kwargs, transition.kwargs)
        else:
            merged_kwargs = dict(tokens_kwargs)
        if net_params:
            wanted = ExecutableGraphOperations._net_params_wanted(transition.function, net_params)
            for name in wanted:
                if name not in merged_kwargs:
                    merged_kwargs[name] = net_params[name]
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
        outgoing_edges: tuple[ReturnedEdgeFromTransition, ...] = transition_names_to_outgoing_edges.get(
            transition.name, ()
        )
        if not outgoing_edges and transition.output_distribution_function is None:
            # A sink: the body consumes (or reads) and produces nothing.
            if result is not None:
                raise ValueError(
                    f"Transition \"{transition.name}\" has no output edge but returned {type(result).__name__}; "
                    f"a transition without output edges must return None."
                )
        elif (
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

    def _net_params_wanted(function: Callable, net_params: dict[str, Any]) -> tuple[str, ...]:
        """The net parameters ``function`` declares a parameter for (by name)."""
        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            return ()
        return tuple(name for name in net_params if name in parameters)

    def add_tokens_to_places(  # Stage 3
        output_place_names_to_tokens: dict[PlaceNodeName, Any],
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        allow_token_copying: bool = False,
        check_types: bool = True,
        executable_graph: Optional[ExecutableGraph] = None,
        firing_ids: Optional[FiringTokenIds] = None,
    ) -> dict[PlaceNodeName, ListPlaceNode]:
        """Distribute the output tokens to the corresponding places.

        Every deposited token is a new token with a fresh id from
        ``executable_graph`` (a copy made under token copying included), recorded on
        ``firing_ids.produced`` if given. Without a graph no ids are assigned and the
        graph's next ``TokenIds.resync`` re-ids the places that changed.
        
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
                deposited = list(token_or_list_to_add)
            elif disposition == "token":
                deposited = [token_or_list_to_add]
            else:
                deposited = []
            if executable_graph is None:
                place.tokens.extend(deposited)
            else:
                ids = TokenIds.append(executable_graph, place, deposited)
                if firing_ids is not None:
                    firing_ids.produced.extend((place.name, token_id) for token_id in ids)
            updated_places[place_name] = place
        return updated_places

    def deposit_and_record(
        executable_graph: ExecutableGraph,
        transition: FunctionTransitionNode,
        output_place_names_to_tokens: dict[PlaceNodeName, Any],
        place_names_to_nodes: dict[PlaceNodeName, ListPlaceNode],
        firing_ids: FiringTokenIds,
        duration_ms: float,
        allow_token_copying: bool = False,
    ) -> dict[PlaceNodeName, ListPlaceNode]:
        """Stage 3 for a firing whose body returned, then its events: ``Mutated``s and
        ``Fired`` on success. A deposit that raises (a token that does not fit its
        place) ends the firing as ``Failed`` (consumed tokens not restored; anything
        already deposited reported as ``Added``) and the error propagates unchanged.
        Shared by sequential firing and the concurrent runner's completion paths, so
        both report every firing exactly once."""
        try:
            updated_places = ExecutableGraphOperations.add_tokens_to_places(
                output_place_names_to_tokens=output_place_names_to_tokens,
                place_names_to_nodes=place_names_to_nodes,
                allow_token_copying=allow_token_copying,
                executable_graph=executable_graph,
                firing_ids=firing_ids,
            )
        except Exception as exception:
            GraphEvents.record_failed(
                executable_graph, transition.name, firing_ids, place_names_to_nodes,
                restored=False, exception=exception,
            )
            raise
        GraphEvents.record_fired(
            executable_graph, transition.name, firing_ids, place_names_to_nodes, duration_ms,
        )
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
        descend: bool = True,
    ) -> Optional[TransitionName]:
        """Fire at most one fireable transition, using prebuilt adjacency maps.

        The single-firing primitive that ``execute_graph`` and the Runner's
        sequential loops compose. The caller owns the per-run setup: building the
        four maps (``MapPlaceNames`` / ``MapTransitionNames``) and validating the
        marking (``ensure_all_token_types_match_place_types``) at whatever cadence
        it wants — so driving an N-firing net pays for that setup once, not once
        per firing. ``last_fired`` is *not* reset here; resetting it per call is
        ``execute_graph``'s contract.

        Selector resolution matches ``execute_graph``: provided > graph's >
        default (highest priority, ties broken by the graph's ``tie_break``). ``None`` for
        ``allow_token_copying`` / ``restore_tokens_on_failure`` means use the
        graph's setting.

        Returns the fired transition's name, or None if no transition was fireable
        (or the selector declined every fireable one).

        Token ids move with the tokens (``TokenIds``); places whose tokens were
        assigned directly are resynced first. With ``record_events`` on, a firing
        records its ``Mutated`` events then one ``Fired`` (or ``Failed``).

        Nested tokens (``NestedNets``): the undrained events of every inner net are
        gathered into this graph's log first, so a firing that consumes a token
        loses none of them. When no outer transition fires and ``descend`` is on,
        one inner net is given the turn (``NestedNets.fire_one_inner``, place
        order then token order then net order, passive places skipped); an inner
        firing counts as a firing of this graph under its path,
        ``"Depot/#17/cabin/Sit"``, which is what ``last_fired``, ``fired_counts``
        and the return value then say.
        """
        TokenIds.resync(executable_graph)
        NestedNets.drain(executable_graph)
        if allow_token_copying is None:
            allow_token_copying = executable_graph.allow_token_copying
        if restore_tokens_on_failure is None:
            restore_tokens_on_failure = executable_graph.restore_tokens_on_failure
        selector = (
            transition_selector
            or executable_graph.transition_selector
            or default_transition_selector
        )

        # Get all fireable transitions (sufficient tokens and a passing guard), in
        # definition order.
        fireable_transitions = []
        for transition in executable_graph.transitions:
            if ExecutableGraphCheck.transition_is_fireable(
                transition=transition,
                executable_graph=executable_graph,
                transition_names_to_incoming_edges=transition_names_to_incoming_edges,
                place_names_to_nodes=place_names_to_nodes,
                transition_names_to_read_edges=transition_names_to_read_edges,
            ):
                fireable_transitions.append(transition)

        # Let selector choose which transition to fire
        transition = selector(executable_graph, fireable_transitions)
        if transition is None:
            if not descend or not NestedNets.any_nested(executable_graph):
                return None
            fired_inside = await NestedNets.fire_one_inner(
                executable_graph, executor=executor,
                allow_token_copying=allow_token_copying, restore_tokens_on_failure=restore_tokens_on_failure,
            )
            if fired_inside is None:
                return None
            executable_graph.step_count += 1
            executable_graph.last_fired = fired_inside
            executable_graph.fired_counts[fired_inside] = executable_graph.fired_counts.get(fired_inside, 0) + 1
            return fired_inside

        firing_ids = FiringTokenIds()
        input_args_to_tokens, input_places = ExecutableGraphOperations.stage_1_extract_argument_tokens_from_places(
            transition=transition,
            transition_names_to_incoming_edges=transition_names_to_incoming_edges,
            place_names_to_nodes=place_names_to_nodes,
            allow_token_copying=allow_token_copying,
            token_history_length=token_history_length,
            transition_names_to_read_edges=transition_names_to_read_edges,
            firing_ids=firing_ids,
        )
        # Mark the chosen transition as running while its body executes, as the
        # concurrent runner does, so an observer on another thread (a heartbeat
        # reading the graph during a long synchronous body) can name it. Each
        # write assigns a fresh set: replacing the reference is atomic, whereas
        # mutating the set in place could be seen half-done.
        executable_graph.in_flight = {transition.name}
        started = time.perf_counter()
        try:
            output_place_names_to_tokens = await ExecutableGraphOperations.stage_2_call_transition_function(
                transition=transition,
                tokens_kwargs=input_args_to_tokens,
                transition_names_to_outgoing_edges=transition_names_to_outgoing_edges,
                place_names_to_nodes=place_names_to_nodes,
                allow_token_copying=allow_token_copying,
                executor=executor,
                net_params=executable_graph.params,
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
                executable_graph=executable_graph,
                firing_ids=firing_ids,
            )
        finally:
            executable_graph.in_flight = set()
        duration_ms = (time.perf_counter() - started) * 1000.0
        updated_places_dict = ExecutableGraphOperations.deposit_and_record(
            executable_graph=executable_graph,
            transition=transition,
            output_place_names_to_tokens=output_place_names_to_tokens,
            place_names_to_nodes=place_names_to_nodes,
            firing_ids=firing_ids,
            duration_ms=duration_ms,
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
                (default 1); None means run until no transition is fireable.
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
                Signature: (graph, fireable_transitions) -> transition_to_fire
                (called positionally). If None, uses graph.transition_selector or default behavior.
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
            # stop_after_n_firings=None means run until no transition is fireable.
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
