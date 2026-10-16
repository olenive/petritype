"""Token keys, filters and queries (``TOKEN_QUERYING_AND_FILTERING.md``, Python
track phase 1: R1, R2). The wildlife example is the acceptance net; section 6
of the design document gives the expected results pinned here."""

import asyncio
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.token_view_filtering.wildlife import FILTERS, Species, answer, build_wildlife  # noqa: E402
from petritype.core.events import Fired  # noqa: E402
from petritype.core.executable_graph_components import (  # noqa: E402
    ExecutableGraphOperations,
    ListPlaceNode,
    NetParams,
)
from petritype.core.keys import Clause, Filter, Keys, Match, Query, is_key_value, same_value  # noqa: E402


def run(coroutine):
    return asyncio.run(coroutine)


async def fire(graph, name):
    def pinned(_graph, fireable):
        return next((t for t in fireable if t.name == name), None)
    fired = await ExecutableGraphOperations.fire_one_transition(
        graph, *ExecutableGraphOperations.maps(graph), transition_selector=pinned, descend=False,
    )
    assert fired == name, f"{name} was not fireable"


def names(graph, place):
    return [s.name for s in graph.place_named(place).tokens]


# --- R1: keys -------------------------------------------------------------------------------------


class TestKeys:

    def test_every_habitat_token_reports_its_three_keys(self):
        graph = build_wildlife()
        for place in graph.places:
            assert place.key_names == ("species", "class", "diet")
            for token in place.tokens:
                assert Keys.of(place, token) == {"species": token.name, "class": token.class_, "diet": token.diet}

    def test_a_decline_leaves_the_keys_while_a_rename_changes_species(self):
        graph = build_wildlife()
        forest = graph.place_named("Forest")
        deer = forest.tokens[2]
        before = Keys.of(forest, deer)
        run(fire(graph, "Decline Forest"))
        assert deer.population == 150 and Keys.of(forest, deer) == before
        deer.name = "Roe Deer"
        assert Keys.of(forest, deer)["species"] == "Roe Deer"

    def test_keys_by_attribute_name(self):
        place = ListPlaceNode("P", Species, [Species(name="Gecko", class_="Reptile", diet="Insectivore", population=1)],
                              keys=("name", "diet"))
        assert place.key_names == ("name", "diet")
        assert Keys.of(place, place.tokens[0]) == {"name": "Gecko", "diet": "Insectivore"}
        assert ListPlaceNode("Q", int).key_names == ()

    def test_a_key_must_be_a_string_an_integer_or_a_boolean(self):
        place = ListPlaceNode("P", float, [1.5], keys={"value": lambda x: x})
        with pytest.raises(TypeError, match="key 'value'.*is float"):
            Keys.of(place, 1.5)
        with pytest.raises(TypeError, match="sequence of attribute names"):
            ListPlaceNode("P", int, keys="name")
        assert is_key_value(True) and is_key_value(0) and is_key_value("x")
        assert not is_key_value(1.0) and not is_key_value(None) and not is_key_value([1])

    def test_one_is_not_true(self):
        assert same_value(1, 1) and not same_value(1, True) and not same_value("1", 1)


# --- filters --------------------------------------------------------------------------------------


class TestFilters:

    def test_where_builds_clauses_and_drops_the_keyword_underscore(self):
        f = Filter.where(class_="Bird", diet=["Insectivore", "Omnivore"])
        assert f.clauses == (Clause("class", ("Bird",)), Clause("diet", ("Insectivore", "Omnivore")))
        assert f.in_places("Forest").places == ("Forest",) and f.with_prefix("For").prefix == "For"

    def test_a_clause_has_values_or_a_param(self):
        with pytest.raises(ValueError):
            Clause("class")
        with pytest.raises(ValueError):
            Clause("class", ("Bird",), param="season")
        with pytest.raises(TypeError):
            Clause("size", (1.5,))

    def test_matching_rules(self):
        graph = build_wildlife()
        forest = graph.place_named("Forest")
        robin, fox, deer, woodpecker = forest.tokens
        assert Filter.where(class_="Bird").matches(graph, forest, robin)
        assert not Filter.where(class_="Bird").matches(graph, forest, fox)
        assert Filter.where(class_=["Bird", "Mammal"], diet="Omnivore").matches(graph, forest, fox)
        assert not Filter.where(class_="Bird").in_places("Tundra").matches(graph, forest, robin)
        assert Filter.where(class_="Bird").with_prefix("For").matches(graph, forest, robin)
        assert not Filter.where(colour="red").matches(graph, forest, robin), "an unknown key matches nothing"
        assert Filter().matches(graph, forest, fox), "no clauses keeps everything"
        plain = ListPlaceNode("Plain", int, [1])
        assert not Filter.where(class_="Bird").matches(graph, plain, 1), "a place without keys matches no clause"
        assert Filter().matches(graph, plain, 1)

    def test_a_param_clause_reads_the_net_parameter_as_it_stands(self):
        graph = build_wildlife()
        forest = graph.place_named("Forest")
        robin = forest.tokens[0]
        seasonal = Filter((Clause("class", param="wanted"),))
        graph.params["wanted"] = "Mammal"
        assert not seasonal.matches(graph, forest, robin)
        graph.params["wanted"] = "Bird"
        assert seasonal.matches(graph, forest, robin)
        del graph.params["wanted"]
        assert not seasonal.matches(graph, forest, robin), "a missing parameter accepts nothing"


# --- the visualiser: a place label that honours a filter ------------------------------------------


class TestFilteredLabel:

    def test_the_label_lists_kept_tokens_and_counts_the_hidden(self):
        pytest.importorskip("rustworkx")
        from petritype.plotting.rustworkx_to_graphviz import NodeLabel, RustworkxToGraphviz
        graph = build_wildlife()
        forest = graph.place_named("Forest")
        summarise = lambda s: s.name
        assert NodeLabel.default_place(forest, summarise).splitlines() == [
            "Forest", "(Species)", "Robin", "Fox", "Deer", "Woodpecker",
        ]
        assert NodeLabel.default_place(forest, summarise, Filter.where(class_="Bird"), graph.params).splitlines() == [
            "Forest", "(Species) 2 / 4", "Robin", "Woodpecker", "(2 hidden by filter)",
        ]
        assert NodeLabel.default_place(forest, summarise, Filter(), graph.params).splitlines() == [
            "Forest", "(Species) 4 / 4", "Robin", "Fox", "Deer", "Woodpecker",
        ]
        node_attrs, _ = RustworkxToGraphviz.activation_coloured_attr_functions(
            graph, summarise, filter=Filter.where(class_="Bird"),
        )
        assert node_attrs(graph.place_named("Savanna"))["label"].splitlines()[-2:] == ["Egret", "(3 hidden by filter)"]


# --- R2: queries ----------------------------------------------------------------------------------


EXPECTED_ON_THE_SEED = {
    "species in {Robin}": [("Forest", "Robin"), ("Wetland", "Robin")],
    "class in {Bird}": [
        ("Forest", "Robin"), ("Forest", "Woodpecker"), ("Wetland", "Robin"), ("Wetland", "Heron"),
        ("Savanna", "Egret"), ("Tundra", "Snowy Owl"),
    ],
    "class in {Bird, Mammal}": [
        ("Forest", "Robin"), ("Forest", "Fox"), ("Forest", "Deer"), ("Forest", "Woodpecker"),
        ("Wetland", "Robin"), ("Wetland", "Heron"), ("Wetland", "Otter"),
        ("Savanna", "Zebra"), ("Savanna", "Lion"), ("Savanna", "Egret"),
        ("Tundra", "Fox"), ("Tundra", "Arctic Hare"), ("Tundra", "Lemming"), ("Tundra", "Snowy Owl"),
    ],
    "class in {Bird} and diet in {Insectivore}": [
        ("Forest", "Robin"), ("Forest", "Woodpecker"), ("Wetland", "Robin"), ("Savanna", "Egret"),
    ],
    "class in {Mammal} in {Forest, Tundra}": [
        ("Forest", "Fox"), ("Forest", "Deer"), ("Tundra", "Fox"), ("Tundra", "Arctic Hare"), ("Tundra", "Lemming"),
    ],
}

# A plain run to quiet (the default selector, autumn): three migrations and three settlings,
# three spreads of the egret, five declines of the deer and five of the lemmings.
PLAIN_RUN = [
    "Migrate Forest Wetland", "Migrate Forest Wetland", "Migrate Tundra Wetland",
    "Settle", "Settle", "Settle",
    *["Spread Savanna Wetland"] * 3, *["Decline Forest"] * 5, *["Decline Tundra"] * 5,
]


def populations(graph, place):
    return [(s.name, s.population) for s in graph.place_named(place).tokens]


class TestQueries:

    def test_the_five_filters_on_the_seed(self):
        graph = build_wildlife()
        for question, filter in FILTERS.items():
            assert answer(graph, filter) == EXPECTED_ON_THE_SEED[question], question

    def test_a_match_carries_place_id_keys_and_token(self):
        graph = build_wildlife()
        (match,) = Query.matches(graph, Filter.where(species="Heron"))
        assert match == Match("Wetland", 6, {}, None)
        assert match.keys == {"species": "Heron", "class": "Bird", "diet": "Carnivore"}
        assert match.token.population == 40

    def test_after_one_migration_and_settling(self):
        graph = build_wildlife()
        assert graph.params == {"season": "Autumn", "spread_threshold": 200}
        graph.drain_events()
        run(fire(graph, "Migrate Forest Wetland"))
        run(fire(graph, "Settle"))
        assert names(graph, "Forest") == ["Fox", "Deer", "Woodpecker"]
        assert names(graph, "Wetland") == ["Robin", "Heron", "Pike", "Otter", "Robin"]
        assert answer(graph, Filter.where(class_="Bird")) == [
            ("Forest", "Woodpecker"), ("Wetland", "Robin"), ("Wetland", "Heron"), ("Wetland", "Robin"),
            ("Savanna", "Egret"), ("Tundra", "Snowy Owl"),
        ]
        assert graph.place_named("Wetland").tokens[4].destination is None
        fired = [e for e in graph.drain_events() if isinstance(e, Fired)]
        # The keyed edge moves the robin alone: one token consumed and one produced per firing,
        # the others untouched (section 6: one Removed, one Added, one Removed, one Added).
        assert [p for p, _ in fired[0].consumed] == ["Forest"]
        assert [p for p, _ in fired[0].produced] == ["Migrating"]
        assert [p for p, _ in fired[1].consumed] == ["Migrating"]
        assert [p for p, _ in fired[1].produced] == ["Wetland"]
        assert graph.place_named("Forest").token_ids == (2, 3, 4), "the fox, the deer and the woodpecker kept their ids"

    def test_the_whole_run_to_quiet(self):
        graph = build_wildlife()
        graph.drain_events()
        _, fired = run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=None))
        assert [e.transition for e in graph.drain_events() if isinstance(e, Fired)] == PLAIN_RUN
        assert fired == 19
        assert populations(graph, "Forest") == [("Fox", 60), ("Deer", 9)]
        assert populations(graph, "Wetland") == [
            ("Robin", 80), ("Heron", 40), ("Pike", 150), ("Otter", 25),
            ("Robin", 120), ("Woodpecker", 45), ("Snowy Owl", 15),
            ("Egret", 750), ("Egret", 375), ("Egret", 188),
        ]
        assert populations(graph, "Savanna") == [("Zebra", 5000), ("Gecko", 200), ("Lion", 40), ("Egret", 187)]
        assert populations(graph, "Reef") == [("Clownfish", 300), ("Parrotfish", 400), ("Reef Shark", 20), ("Sea Turtle", 80)]
        assert populations(graph, "Tundra") == [("Fox", 30), ("Arctic Hare", 90), ("Lemming", 62)]
        assert names(graph, "Migrating") == []
        assert all(s.population > 0 for place in graph.places for s in place.tokens), "no population reaches zero"
        assert all(s.destination is None for place in graph.places for s in place.tokens)

    def test_in_summer_nothing_migrates(self):
        graph = build_wildlife()
        NetParams.set(graph, "season", "Summer")
        _, fired = run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=None))
        assert fired == 13, "three spreads and ten declines"
        assert names(graph, "Forest") == ["Robin", "Fox", "Deer", "Woodpecker"] and names(graph, "Migrating") == []
        assert names(graph, "Tundra")[-1] == "Snowy Owl"

    def test_after_a_spread(self):
        graph = build_wildlife()
        savanna, wetland = (graph.place_named(p) for p in ("Savanna", "Wetland"))
        ids = savanna.token_ids
        run(fire(graph, "Spread Savanna Wetland"))
        assert answer(graph, Filter.where(species="Egret")) == [("Wetland", "Egret"), ("Savanna", "Egret")]
        egret = savanna.tokens[3]
        assert egret.population == 750 and wetland.tokens[4].population == 750
        assert savanna.token_ids == ids, "the egret keeps its id and its place"
        assert populations(graph, "Savanna")[0] == ("Zebra", 5000), "the keyed edge leaves the zebra alone"
        for _ in range(2):
            run(fire(graph, "Spread Savanna Wetland"))
        assert egret.population == 187 and [s.population for s in wetland.tokens[4:]] == [750, 375, 188]
        # The egret is below the threshold now; the zebra in front of it does not count.
        with pytest.raises(AssertionError, match="not fireable"):
            run(fire(graph, "Spread Savanna Wetland"))

    def test_the_declines_stop_at_their_floors(self):
        graph = build_wildlife()
        for _ in range(5):
            run(fire(graph, "Decline Forest"))
        assert populations(graph, "Forest")[2] == ("Deer", 9)
        with pytest.raises(AssertionError, match="not fireable"):
            run(fire(graph, "Decline Forest"))
        for _ in range(5):
            run(fire(graph, "Decline Tundra"))
        assert populations(graph, "Tundra")[2] == ("Lemming", 62)
        with pytest.raises(AssertionError, match="not fireable"):
            run(fire(graph, "Decline Tundra"))

    def test_a_guard_reads_the_token_its_edge_would_take(self):
        graph = build_wildlife()
        forest = graph.place_named("Forest")
        forest.tokens.insert(0, Species(name="Hare", class_="Mammal", diet="Herbivore", population=8))
        # The oldest herbivore is the hare, at its floor; the deer behind it does not count.
        with pytest.raises(AssertionError, match="not fireable"):
            run(fire(graph, "Decline Forest"))
        forest.tokens[0].population = 40
        run(fire(graph, "Decline Forest"))
        assert populations(graph, "Forest")[0] == ("Hare", 20) and populations(graph, "Forest")[3] == ("Deer", 300)

    def test_distinct_values_in_order_of_first_appearance(self):
        graph = build_wildlife()
        assert Query.distinct(graph, "class") == ["Bird", "Mammal", "Fish", "Reptile"]
        assert Query.distinct(graph, "diet", places=["Tundra"]) == ["Omnivore", "Herbivore", "Carnivore"]
        assert Query.distinct(graph, "colour") == []

    def test_count_and_first(self):
        graph = build_wildlife()
        forest = graph.place_named("Forest")
        birds = Filter.where(class_="Bird")
        assert Query.count(graph, forest, birds) == 2 and Query.first(graph, forest, birds) == 0
        mammals = Filter.where(class_="Mammal")
        assert Query.first(graph, forest, mammals) == 1
        assert Query.first(graph, forest, Filter.where(diet="Herbivore")) == 2
        assert Query.first(graph, forest, Filter.where(class_="Fish")) is None

    def test_the_species_json_form_spells_class(self):
        robin = build_wildlife().place_named("Forest").tokens[0]
        assert robin.model_dump(mode="json", by_alias=True) == {
            "name": "Robin", "class": "Bird", "diet": "Insectivore", "population": 120, "destination": None,
        }
        assert Species.model_validate_json('{"name": "Owl", "class": "Bird", "diet": "Carnivore", "population": 3}').class_ == "Bird"
