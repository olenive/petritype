"""Wildlife: species occurrences across habitats, with keys to query them by.

The example of ``TOKEN_QUERYING_AND_FILTERING.md`` §2, and the conformance net
``wildlife`` of PROTOCOL.md §11. Places are habitats; a token is one species
occurring in one habitat, and carries its own category (``class``), so "all
birds" is a question about keys the tokens carry, never something derived from
names. Every place declares the keys ``species``, ``class`` and ``diet``.

Run it to see the five filters of the design document answered on the seed::

    uv run python examples/wildlife.py
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
    MutateEdge,
    ReturnedEdgeFromTransition,
)
from petritype.core.keys import Filter, Query

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


# --- bodies ---------------------------------------------------------------------------------


def migrate(species: list[Species]) -> tuple[list[Species], list[Species]]:
    """Until keyed edges: take every token of the habitat, send the oldest bird
    on its way, put the others back (slot 0 stays, slot 1 migrates)."""
    for index, candidate in enumerate(species):
        if candidate.class_ == "Bird":
            candidate.destination = "Wetland"
            return species[:index] + species[index + 1:], [candidate]
    return species, []


def settle(species: Species) -> Species:
    return species


def route_settlement(species: Species) -> dict[str, Species]:
    destination, species.destination = species.destination, None
    return {destination: species}


def spread(species: Species) -> Species:
    """The token stays with half its population (rounded down); a copy with the
    other half appears in the destination."""
    kept = species.population // 2
    moved = species.population - kept
    species.population = kept
    return species.model_copy(update={"population": moved})


def decline(species: Species) -> None:
    species.population //= 2


# --- guards ---------------------------------------------------------------------------------


def _front(graph: ExecutableGraph, place: str) -> Species:
    return graph.place_named(place).tokens[0]


def autumn_and_a_bird_in_forest(graph: ExecutableGraph) -> bool:
    return graph.params["season"] == "Autumn" and any(s.class_ == "Bird" for s in graph.place_named("Forest").tokens)


def savanna_front_above_threshold(graph: ExecutableGraph) -> bool:
    return _front(graph, "Savanna").population > graph.params["spread_threshold"]


def forest_front_alive(graph: ExecutableGraph) -> bool:
    return _front(graph, "Forest").population > 0


# --- the net ---------------------------------------------------------------------------------


def seed() -> dict[str, list[Species]]:
    def s(name, class_, diet, population):
        return Species(name=name, class_=class_, diet=diet, population=population)
    return {
        "Forest": [s("Robin", "Bird", "Insectivore", 120), s("Fox", "Mammal", "Omnivore", 60)],
        "Wetland": [s("Robin", "Bird", "Insectivore", 80), s("Heron", "Bird", "Carnivore", 40)],
        "Savanna": [s("Zebra", "Mammal", "Herbivore", 5000), s("Gecko", "Reptile", "Insectivore", 200)],
        "Reef": [s("Clownfish", "Fish", "Omnivore", 300)],
        "Tundra": [s("Fox", "Mammal", "Omnivore", 30), s("Arctic Hare", "Mammal", "Herbivore", 90)],
    }


def build_wildlife(record_events: bool = True) -> ExecutableGraph:
    tokens = seed()
    return ExecutableGraphOperations.construct_graph([
        *[ListPlaceNode(habitat, Species, tokens[habitat], keys=KEYS) for habitat in HABITATS],
        ListPlaceNode("Migrating", Species, keys=KEYS),
        FunctionTransitionNode("Migrate Forest Wetland", migrate, guard=autumn_and_a_bird_in_forest),
        ArgumentEdgeToTransition("Forest", "Migrate Forest Wetland", "species"),
        ReturnedEdgeFromTransition("Migrate Forest Wetland", "Forest", 0),
        ReturnedEdgeFromTransition("Migrate Forest Wetland", "Migrating", 1),
        FunctionTransitionNode("Settle", settle, output_distribution_function=route_settlement),
        ArgumentEdgeToTransition("Migrating", "Settle", "species"),
        *[ReturnedEdgeFromTransition("Settle", habitat) for habitat in HABITATS],
        FunctionTransitionNode("Spread Savanna Reef", spread, guard=savanna_front_above_threshold),
        MutateEdge("Savanna", "Spread Savanna Reef", "species"),
        ReturnedEdgeFromTransition("Spread Savanna Reef", "Reef"),
        FunctionTransitionNode("Decline Forest", decline, guard=forest_front_alive),
        MutateEdge("Forest", "Decline Forest", "species"),
    ], params={"season": "Summer", "spread_threshold": 1000}, record_events=record_events)


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
