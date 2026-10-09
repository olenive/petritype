"""Token keys, filters and queries (``TOKEN_QUERYING_AND_FILTERING.md``, Python
track phase 1: R1, R2). The wildlife example is the acceptance net; section 6
of the design document gives the expected results pinned here."""

import asyncio
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.wildlife import FILTERS, Species, answer, build_wildlife  # noqa: E402
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
        robin = forest.tokens[0]
        before = Keys.of(forest, robin)
        run(fire(graph, "Decline Forest"))
        assert robin.population == 60 and Keys.of(forest, robin) == before
        robin.name = "Redbreast"
        assert Keys.of(forest, robin)["species"] == "Redbreast"

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
        robin, fox = forest.tokens
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
        assert NodeLabel.default_place(forest, summarise).splitlines() == ["Forest", "(Species)", "Robin", "Fox"]
        assert NodeLabel.default_place(forest, summarise, Filter.where(class_="Bird"), graph.params).splitlines() == [
            "Forest", "(Species) 1 / 2", "Robin", "(1 hidden by filter)",
        ]
        assert NodeLabel.default_place(forest, summarise, Filter(), graph.params).splitlines() == [
            "Forest", "(Species) 2 / 2", "Robin", "Fox",
        ]
        node_attrs, _ = RustworkxToGraphviz.activation_coloured_attr_functions(
            graph, summarise, filter=Filter.where(class_="Bird"),
        )
        assert node_attrs(graph.place_named("Savanna"))["label"].splitlines()[-1] == "(2 hidden by filter)"


# --- R2: queries ----------------------------------------------------------------------------------


EXPECTED_ON_THE_SEED = {
    "species in {Robin}": [("Forest", "Robin"), ("Wetland", "Robin")],
    "class in {Bird}": [("Forest", "Robin"), ("Wetland", "Robin"), ("Wetland", "Heron")],
    "class in {Bird, Mammal}": [
        ("Forest", "Robin"), ("Forest", "Fox"), ("Wetland", "Robin"), ("Wetland", "Heron"),
        ("Savanna", "Zebra"), ("Tundra", "Fox"), ("Tundra", "Arctic Hare"),
    ],
    "class in {Bird} and diet in {Insectivore}": [("Forest", "Robin"), ("Wetland", "Robin")],
    "class in {Mammal} in {Forest, Tundra}": [("Forest", "Fox"), ("Tundra", "Fox"), ("Tundra", "Arctic Hare")],
}


class TestQueries:

    def test_the_five_filters_on_the_seed(self):
        graph = build_wildlife()
        for question, filter in FILTERS.items():
            assert answer(graph, filter) == EXPECTED_ON_THE_SEED[question], question

    def test_a_match_carries_place_id_keys_and_token(self):
        graph = build_wildlife()
        (match,) = Query.matches(graph, Filter.where(species="Heron"))
        assert match == Match("Wetland", 4, {}, None)
        assert match.keys == {"species": "Heron", "class": "Bird", "diet": "Carnivore"}
        assert match.token.population == 40

    def test_after_one_migration_and_settling(self):
        graph = build_wildlife()
        NetParams.set(graph, "season", "Autumn")
        graph.drain_events()
        run(fire(graph, "Migrate Forest Wetland"))
        run(fire(graph, "Settle"))
        assert names(graph, "Forest") == ["Fox"] and names(graph, "Wetland") == ["Robin", "Heron", "Robin"]
        assert answer(graph, Filter.where(class_="Bird")) == [("Wetland", "Robin"), ("Wetland", "Heron"), ("Wetland", "Robin")]
        assert graph.place_named("Wetland").tokens[2].destination is None
        fired = [e for e in graph.drain_events() if isinstance(e, Fired)]
        # The keyed edge moves the robin alone: one token consumed and one produced per firing,
        # the fox untouched (section 6: one Removed, one Added, one Removed, one Added).
        assert [p for p, _ in fired[0].consumed] == ["Forest"]
        assert [p for p, _ in fired[0].produced] == ["Migrating"]
        assert [p for p, _ in fired[1].consumed] == ["Migrating"]
        assert [p for p, _ in fired[1].produced] == ["Wetland"]
        assert graph.place_named("Forest").token_ids == (2,), "the fox kept its id"

    def test_the_whole_run_to_quiet_also_settles(self):
        graph = build_wildlife()
        NetParams.set(graph, "season", "Autumn")
        run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=2))
        assert names(graph, "Wetland") == ["Robin", "Heron", "Robin"]

    def test_after_a_spread(self):
        graph = build_wildlife()
        run(fire(graph, "Spread Savanna Reef"))
        assert answer(graph, Filter.where(species="Zebra")) == [("Savanna", "Zebra"), ("Reef", "Zebra")]
        savanna, reef = (graph.place_named(p).tokens for p in ("Savanna", "Reef"))
        assert savanna[0].population == 2500 and reef[1].population == 2500
        for _ in range(2):
            run(fire(graph, "Spread Savanna Reef"))
        assert savanna[0].population == 625, "below the threshold, Spread stops"
        with pytest.raises(AssertionError, match="not fireable"):
            run(fire(graph, "Spread Savanna Reef"))

    def test_distinct_values_in_order_of_first_appearance(self):
        graph = build_wildlife()
        assert Query.distinct(graph, "class") == ["Bird", "Mammal", "Reptile", "Fish"]
        assert Query.distinct(graph, "diet", places=["Tundra"]) == ["Omnivore", "Herbivore"]
        assert Query.distinct(graph, "colour") == []

    def test_count_and_first(self):
        graph = build_wildlife()
        forest = graph.place_named("Forest")
        birds = Filter.where(class_="Bird")
        assert Query.count(graph, forest, birds) == 1 and Query.first(graph, forest, birds) == 0
        mammals = Filter.where(class_="Mammal")
        assert Query.first(graph, forest, mammals) == 1
        assert Query.first(graph, forest, Filter.where(class_="Fish")) is None

    def test_the_species_json_form_spells_class(self):
        robin = build_wildlife().place_named("Forest").tokens[0]
        assert robin.model_dump(mode="json", by_alias=True) == {
            "name": "Robin", "class": "Bird", "diet": "Insectivore", "population": 120, "destination": None,
        }
        assert Species.model_validate_json('{"name": "Owl", "class": "Bird", "diet": "Carnivore", "population": 3}').class_ == "Bird"
