"""Wildlife: species occurrences across habitats, with keys to query them by.

The example of ``TOKEN_QUERYING_AND_FILTERING.md`` §2, and the conformance net
``wildlife`` of PROTOCOL.md §11. Places are habitats; a token is one species
occurring in one habitat, and carries its own category (``class``), so "all
birds" is a question about keys the tokens carry, never something derived from
names. Every place declares the keys ``species``, ``class`` and ``diet``, and
every transition but ``Settle`` acts through a keyed edge: in autumn the oldest
bird of the forest and of the tundra migrates to the wetland, the savanna's
egrets spread to the wetland while there are more than ``spread_threshold`` of
them, the forest's deer thin down to a floor of ten and the tundra's lemmings
crash down to a hundred. Each guard reads the token its edge would take, so a
plain run is about nineteen firings and then nothing is fireable.

Run it to see the five filters of the design document answered on the seed::

    uv run python examples/token_view_filtering/wildlife.py
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from petritype import petri_net
from petritype.core.executable_graph_components import (
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    ReturnedEdgeFromTransition,
)
from petritype.core.keys import Filter, MutateWhereEdge, Query, TakeWhereEdge

HABITATS = ("Forest", "Wetland", "Savanna", "Reef", "Tundra")
Class = Literal["Bird", "Mammal", "Reptile", "Fish"]
Diet = Literal["Herbivore", "Carnivore", "Insectivore", "Omnivore"]


class Species(BaseModel):
    """One species in one habitat. ``class`` is a Python keyword, so the field is
    ``class_`` and ``class`` on the wire (its alias); either name constructs it."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    class_: Class = Field(alias="class")
    diet: Diet
    population: int
    destination: Optional[str] = None


KEYS = {
    "species": lambda s: s.name,
    "class": lambda s: s.class_,
    "diet": lambda s: s.diet,
}


# --- the filters the keyed edges carry ------------------------------------------------------

BIRDS = Filter.where(class_="Bird")
HERBIVORES = Filter.where(diet="Herbivore")
LEMMINGS = Filter.where(species="Lemming")


# --- bodies ---------------------------------------------------------------------------------


def migrate(species: Species) -> Species:
    """The keyed edge hands over the oldest bird of the habitat and nothing else;
    the other tokens stay where they are, with their ids."""
    species.destination = "Wetland"
    return species


def settle(species: Species) -> Species:
    return species


def route_settlement(species: Species) -> dict[str, Species]:
    destination, species.destination = species.destination, None
    return {destination: species}


def spread(species: Species) -> Species:
    """The token stays where it is with half its population (rounded down); a
    copy carrying the rest is produced into the destination."""
    kept = species.population // 2
    moved = species.population - kept
    species.population = kept
    return species.model_copy(update={"population": moved})


def decline(species: Species) -> None:
    species.population //= 2


# --- guards: each reads the token its edge would take ----------------------------------------


def _oldest(graph: ExecutableGraph, place: str, filter: Filter) -> Optional[Species]:
    """The oldest token of ``place`` the filter keeps: the one a keyed edge takes."""
    node = graph.place_named(place)
    index = Query.first(graph, node, filter)
    return None if index is None else node.tokens[index]


def autumn(graph: ExecutableGraph) -> bool:
    return graph.params["season"] == "Autumn"


def savanna_birds_above_threshold(graph: ExecutableGraph) -> bool:
    bird = _oldest(graph, "Savanna", BIRDS)
    return bird is not None and bird.population > graph.params["spread_threshold"]


def forest_herbivores_above_ten(graph: ExecutableGraph) -> bool:
    herbivore = _oldest(graph, "Forest", HERBIVORES)
    return herbivore is not None and herbivore.population > 10


def lemmings_above_a_hundred(graph: ExecutableGraph) -> bool:
    lemmings = _oldest(graph, "Tundra", LEMMINGS)
    return lemmings is not None and lemmings.population > 100


# --- the net ---------------------------------------------------------------------------------


def seed() -> dict[str, list[Species]]:
    def s(name, class_, diet, population):
        return Species(name=name, class_=class_, diet=diet, population=population)
    return {
        "Forest": [
            s("Robin", "Bird", "Insectivore", 120), s("Fox", "Mammal", "Omnivore", 60),
            s("Deer", "Mammal", "Herbivore", 300), s("Woodpecker", "Bird", "Insectivore", 45),
        ],
        "Wetland": [
            s("Robin", "Bird", "Insectivore", 80), s("Heron", "Bird", "Carnivore", 40),
            s("Pike", "Fish", "Carnivore", 150), s("Otter", "Mammal", "Carnivore", 25),
        ],
        "Savanna": [
            s("Zebra", "Mammal", "Herbivore", 5000), s("Gecko", "Reptile", "Insectivore", 200),
            s("Lion", "Mammal", "Carnivore", 40), s("Egret", "Bird", "Insectivore", 1500),
        ],
        "Reef": [
            s("Clownfish", "Fish", "Omnivore", 300), s("Parrotfish", "Fish", "Herbivore", 400),
            s("Reef Shark", "Fish", "Carnivore", 20), s("Sea Turtle", "Reptile", "Herbivore", 80),
        ],
        "Tundra": [
            s("Fox", "Mammal", "Omnivore", 30), s("Arctic Hare", "Mammal", "Herbivore", 90),
            s("Lemming", "Mammal", "Herbivore", 2000), s("Snowy Owl", "Bird", "Carnivore", 15),
        ],
    }


def build_wildlife(record_events: bool = True) -> ExecutableGraph:
    tokens = seed()
    return ExecutableGraphOperations.construct_graph([
        *[ListPlaceNode(habitat, Species, tokens[habitat], keys=KEYS) for habitat in HABITATS],
        ListPlaceNode("Migrating", Species, keys=KEYS),
        # In autumn the oldest bird of the forest, and of the tundra, leaves for the wetland.
        FunctionTransitionNode("Migrate Forest Wetland", migrate, guard=autumn),
        TakeWhereEdge("Forest", "Migrate Forest Wetland", "species", where=BIRDS),
        ReturnedEdgeFromTransition("Migrate Forest Wetland", "Migrating"),
        FunctionTransitionNode("Migrate Tundra Wetland", migrate, guard=autumn),
        TakeWhereEdge("Tundra", "Migrate Tundra Wetland", "species", where=BIRDS),
        ReturnedEdgeFromTransition("Migrate Tundra Wetland", "Migrating"),
        FunctionTransitionNode("Settle", settle, output_distribution_function=route_settlement),
        ArgumentEdgeToTransition("Migrating", "Settle", "species"),
        *[ReturnedEdgeFromTransition("Settle", habitat) for habitat in HABITATS],
        # The savanna's egrets follow the herds and roost in the wetland: half of them go.
        FunctionTransitionNode("Spread Savanna Wetland", spread, guard=savanna_birds_above_threshold),
        MutateWhereEdge("Savanna", "Spread Savanna Wetland", "species", where=BIRDS),
        ReturnedEdgeFromTransition("Spread Savanna Wetland", "Wetland"),
        # The forest's deer thin, down to a floor; the tundra's lemmings crash.
        FunctionTransitionNode("Decline Forest", decline, guard=forest_herbivores_above_ten),
        MutateWhereEdge("Forest", "Decline Forest", "species", where=HERBIVORES),
        FunctionTransitionNode("Decline Tundra", decline, guard=lemmings_above_a_hundred),
        MutateWhereEdge("Tundra", "Decline Tundra", "species", where=LEMMINGS),
    ], params={"season": "Autumn", "spread_threshold": 200}, record_events=record_events)


@petri_net(name="wildlife", description="Species migrate, spread and decline across habitats")
def wildlife() -> ExecutableGraph:
    return build_wildlife()


# --- the five questions ------------------------------------------------------------------------

FILTERS = {
    "species in {Robin}": Filter.where(species="Robin"),
    "class in {Bird}": Filter.where(class_="Bird"),
    "class in {Bird, Mammal}": Filter.where(class_=["Bird", "Mammal"]),
    "class in {Bird} and diet in {Insectivore}": Filter.where(class_="Bird", diet="Insectivore"),
    "class in {Mammal} in {Forest, Tundra}": Filter.where(class_="Mammal").in_places("Forest", "Tundra"),
}


def answer(graph: ExecutableGraph, filter: Filter) -> list[tuple[str, str]]:
    return [(match.place, match.token.name) for match in Query.matches(graph, filter)]


def main() -> None:
    graph = build_wildlife()
    for question, filter in FILTERS.items():
        print(f"{question}: {answer(graph, filter)}")
    print(f"distinct class: {Query.distinct(graph, 'class')}")


if __name__ == "__main__":
    main()
