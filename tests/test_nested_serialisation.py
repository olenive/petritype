"""Nets as tokens, phase 2 (``NETS_AS_TOKENS.md`` R5, R5a, R5b, R6): saving and
loading markings, templates, the JSON form of a nested token, and forking.

The acceptance net is the transit example; step 6 of the design's script (save
after step 3 and after step 5, load each, continue, markings equal) is pinned
here through JSON text.
"""

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.tokens_as_nets.transit import HELD_BACK, Vehicle, build_cabin, build_city, fire, run_to_quiet  # noqa: E402
from petritype.core.events import Resynced  # noqa: E402
from petritype.core.executable_graph_components import (  # noqa: E402
    ArgumentEdgeToTransition,
    ExecutableGraph,
    ExecutableGraphOperations,
    FunctionTransitionNode,
    ListPlaceNode,
    NestedNets,
    ReturnedEdgeFromTransition,
)
from petritype.core.serialisation import JsonForm, NetJson, Templates  # noqa: E402
from petritype.runner.translate import Serializable  # noqa: E402


def run(coroutine):
    return asyncio.run(coroutine)


def tokens(graph, place):
    return list(graph.place_named(place).tokens)


class Parcel(BaseModel):
    id: int
    deferrals: int = 0


def sort(parcel: Parcel) -> Parcel:
    return parcel


def build_parcels(seed=(1, 2, 3), template=None) -> ExecutableGraph:
    return ExecutableGraphOperations.construct_graph([
        ListPlaceNode("Receiving", Parcel, [Parcel(id=i) for i in seed]),
        ArgumentEdgeToTransition("Receiving", "Sort", "parcel"),
        FunctionTransitionNode("Sort", sort),
        ReturnedEdgeFromTransition("Sort", "Sorted"),
        ListPlaceNode("Sorted", Parcel),
    ], params={"limit": 3}, template=template, record_events=True)


# --- R5a: export and import a marking --------------------------------------------------------


class TestMarkingRoundTrip:

    def test_a_plain_net_round_trips_through_json_text(self):
        graph = build_parcels()
        run(ExecutableGraphOperations.execute_graph(graph, stop_after_n_firings=1))
        text = json.dumps(graph.export_marking())
        assert json.loads(text) == {
            "Receiving": [{"id": 2, "deferrals": 0}, {"id": 3, "deferrals": 0}],
            "Sorted": [{"id": 1, "deferrals": 0}],
        }
        other = build_parcels(seed=())
        other.import_marking(json.loads(text))
        assert tokens(other, "Receiving") == [Parcel(id=2), Parcel(id=3)]
        assert tokens(other, "Sorted") == [Parcel(id=1)]
        run(ExecutableGraphOperations.execute_graph(other, stop_after_n_firings=None))
        assert [p.id for p in tokens(other, "Sorted")] == [1, 2, 3]

    def test_ids_are_fresh_and_each_place_records_resynced(self):
        graph = build_parcels()
        graph.drain_events()
        graph.import_marking({"Sorted": [{"id": 9}]})
        assert graph.place_named("Receiving").token_ids == ()
        assert graph.place_named("Sorted").token_ids == (4,)
        assert graph.drain_events() == [Resynced("Receiving"), Resynced("Sorted")]

    def test_a_missing_place_loads_empty_and_an_unknown_one_is_an_error(self):
        graph = build_parcels()
        with pytest.raises(KeyError, match="no place 'Loading Bay'"):
            graph.import_marking({"Loading Bay": []})
        assert [p.id for p in tokens(graph, "Receiving")] == [1, 2, 3], "nothing changed"
        graph.import_marking({})
        assert tokens(graph, "Receiving") == [] and tokens(graph, "Sorted") == []

    def test_decoding_is_strict_and_checked_before_anything_changes(self):
        graph = build_parcels()
        with pytest.raises(ValueError, match="does not decode"):
            graph.import_marking({"Receiving": [{"id": "9"}]})
        assert [p.id for p in tokens(graph, "Receiving")] == [1, 2, 3]
        with pytest.raises(ValueError, match="must be a list"):
            graph.import_marking({"Receiving": {"id": 9}})

    def test_a_place_without_a_json_form_cannot_be_saved(self):
        graph = ExecutableGraphOperations.construct_graph([ListPlaceNode("Things", object, [object()])])
        with pytest.raises(TypeError, match="'Things'.*no JSON form"):
            graph.export_marking()

    def test_a_scalar_place_round_trips(self):
        graph = ExecutableGraphOperations.construct_graph([ListPlaceNode("Names", str, ["Ada", "Bram"])])
        marking = graph.export_marking()
        assert marking == {"Names": ["Ada", "Bram"]}
        graph.import_marking({"Names": ["Cato"]})
        assert tokens(graph, "Names") == ["Cato"]
        with pytest.raises(ValueError):
            graph.import_marking({"Names": [1]})


# --- R5b: templates --------------------------------------------------------------------------


class TestTemplates:

    def test_a_registered_template_stamps_its_name_and_rebuilds(self):
        try:
            factory = Templates.register("Parcels", build_parcels)
            assert factory().template == "Parcels"
            assert Templates.build("Parcels").template == "Parcels"
            assert "Parcels" in Templates.names()
        finally:
            Templates.forget("Parcels")
        with pytest.raises(LookupError, match="no template named 'Parcels'"):
            Templates.build("Parcels")

    def test_the_example_templates_are_registered(self):
        assert {"Cabin", "Deck", "Pod"} <= set(Templates.names())
        assert build_cabin().template == "Cabin"
        assert Templates.build("Deck").template == "Deck"

    def test_a_template_must_build_a_graph(self):
        try:
            Templates.register("Broken", lambda: 42)
            with pytest.raises(TypeError, match="built int"):
                Templates.build("Broken")
        finally:
            Templates.forget("Broken")


# --- R5: the saved form of a net and of a nested token ---------------------------------------


class TestSavedForm:

    def test_save_and_restore_a_net(self):
        cabin = build_cabin()
        NestedNets.put(cabin, "Door", ["Ada", "Bram"])
        NestedNets.set_param(cabin, "stopped", False)
        cabin.disabled = {"Stand"}
        saved = NetJson.save(cabin)
        assert saved == {
            "template": "Cabin",
            "marking": {"Door": ["Ada", "Bram"], "Seats": [], "Standing": [], "Announcements": []},
            "params": {"stopped": False, "seats": 2, "capacity": 6},
            "enabled": {"Sit": True, "Stand": False, "Go To Door": True, "Go To Door From Standing": True,
                        "Clear Announcement": True},
        }
        restored = NetJson.restore(json.loads(json.dumps(saved)))
        assert restored is not cabin and restored.net_id != cabin.net_id
        assert NetJson.save(restored) == saved
        assert restored.disabled == {"Stand"}
        run(ExecutableGraphOperations.execute_graph(restored, stop_after_n_firings=None))
        assert tokens(restored, "Seats") == ["Ada", "Bram"], "a disabled Stand is not fireable"

    def test_load_into_checks_everything_first(self):
        cabin = build_cabin()
        with pytest.raises(KeyError, match="no parameter 'speed'"):
            NetJson.load_into(cabin, {"marking": {}, "params": {"speed": 1}})
        with pytest.raises(KeyError, match="no transition 'Fly'"):
            NetJson.load_into(cabin, {"marking": {}, "enabled": {"Fly": True}})
        with pytest.raises(ValueError, match="saved net is a 'Deck'"):
            NetJson.load_into(cabin, {"template": "Deck", "marking": {}})
        with pytest.raises(KeyError, match="no key 'extra'"):
            NetJson.load_into(cabin, {"marking": {}, "extra": 1})
        assert cabin.params == {"stopped": True, "seats": 2, "capacity": 6}

    def test_restore_needs_a_template(self):
        with pytest.raises(LookupError, match="names the template"):
            NetJson.restore({"marking": {}})
        with pytest.raises(LookupError, match="no template named 'Zeppelin'"):
            NetJson.restore({"template": "Zeppelin", "marking": {}})

    def test_a_graph_dumps_as_its_saved_form_in_json_mode_only(self):
        cabin = build_cabin()
        assert json.loads(cabin.model_dump_json()) == NetJson.save(cabin)
        assert cabin.model_dump(mode="json") == NetJson.save(cabin)
        assert "places" in cabin.model_dump()

    def test_a_nested_token_has_the_json_form_of_the_design(self):
        city = build_city()
        bus = tokens(city, "Depot")[0]
        NestedNets.put(bus.cabin, "Door", ["Ada"])
        assert JsonForm.encode(bus) == {
            "name": "Bus 1", "kind": "Bus", "destination": None,
            "cabin": {
                "template": "Cabin",
                "marking": {"Door": ["Ada"], "Seats": [], "Standing": [], "Announcements": []},
                "params": {"stopped": True, "seats": 2, "capacity": 6},
                "enabled": {"Sit": True, "Stand": True, "Go To Door": True, "Go To Door From Standing": True,
                            "Clear Announcement": True},
            },
        }
        assert Serializable.place_type(Vehicle) and Serializable.place_type(ExecutableGraph)

    def test_a_nested_token_decodes_strictly_and_rebuilds_its_net(self):
        saved = {"name": "Bus 2", "kind": "Bus", "destination": "Central",
                 "cabin": {"template": "Cabin", "marking": {"Seats": ["Ada"]},
                           "params": {"stopped": False}, "enabled": {"Sit": False}}}
        bus = JsonForm.decode(Vehicle, saved)
        assert bus.name == "Bus 2" and bus.destination == "Central"
        assert bus.cabin.template == "Cabin" and tokens(bus.cabin, "Seats") == ["Ada"]
        assert bus.cabin.params == {"stopped": False, "seats": 2, "capacity": 6}
        assert bus.cabin.disabled == {"Sit"}
        with pytest.raises(ValueError, match="not a saved net"):
            JsonForm.decode(Vehicle, {**saved, "cabin": {"template": "Zeppelin", "marking": {}}})
        with pytest.raises(ValueError):
            JsonForm.decode(Vehicle, {**saved, "cabin": {"template": "Cabin", "marking": {"Seats": [1]}}})

    def test_a_graph_validates_from_its_saved_form(self):
        cabin = ExecutableGraph.model_validate({"template": "Pod", "marking": {"Door": ["Ada"]}})
        assert cabin.template == "Pod" and tokens(cabin, "Door") == ["Ada"]


# --- step 6 of the acceptance script -----------------------------------------------------------


async def to_step_three(city):
    for _ in range(3):
        await fire(city, "Board Depot")
    await run_to_quiet(city, HELD_BACK)
    await fire(city, "Depart Depot")
    await run_to_quiet(city, HELD_BACK)


async def steps_four_and_five(city):
    await fire(city, "Arrive")
    await run_to_quiet(city, HELD_BACK)
    await fire(city, "Alight Central")
    await fire(city, "Alight Central")


def saved_through_text(city) -> dict:
    return json.loads(json.dumps(NetJson.save(city)))


class TestSaveAndLoadTheCity:

    def test_step_six(self):
        straight = build_city()
        run(to_step_three(straight))
        after_three = saved_through_text(straight)
        bus_in_transit = after_three["marking"]["In Transit"][0]
        assert bus_in_transit["destination"] == "Central"
        assert bus_in_transit["cabin"]["marking"] == {
            "Door": [], "Seats": ["Ada", "Bram"], "Standing": ["Cato"], "Announcements": [],
        }
        assert bus_in_transit["cabin"]["params"]["stopped"] is False

        loaded = NetJson.load_into(build_city(), after_three)
        assert NetJson.save(loaded) == after_three
        run(steps_four_and_five(straight))
        run(steps_four_and_five(loaded))
        assert NetJson.save(loaded) == NetJson.save(straight), "inner and outer markings equal after continuing"
        assert tokens(loaded, "Platform@Central") == ["Ada", "Bram"]
        assert tokens(tokens(loaded, "Central")[0].cabin, "Door") == ["Cato"]

        after_five = saved_through_text(straight)
        loaded_again = NetJson.load_into(build_city(), after_five)
        assert NetJson.save(loaded_again) == after_five
        bus = tokens(loaded_again, "Central")[0]
        assert bus.cabin.template == "Cabin" and bus.cabin.net_id != tokens(straight, "Central")[0].cabin.net_id

    def test_the_outer_marking_alone_round_trips_too(self):
        city = build_city()
        run(to_step_three(city))
        marking = json.loads(json.dumps(city.export_marking()))
        other = build_city()
        other.import_marking(marking)
        assert other.export_marking() == marking
        assert [v.name for v in tokens(other, "In Transit")] == ["Bus 1"]


# --- R6: forking ------------------------------------------------------------------------------


class TestFork:

    def test_a_fork_is_a_new_net_with_the_same_marking(self):
        city = build_city()
        bus = tokens(city, "Depot")[0]
        NestedNets.put(bus.cabin, "Door", ["Ada"])
        fork = NestedNets.fork(bus)
        assert fork is not bus and fork.cabin is not bus.cabin
        assert fork.cabin.net_id != bus.cabin.net_id
        assert NetJson.save(fork.cabin) == NetJson.save(bus.cabin)
        run(ExecutableGraphOperations.execute_graph(fork.cabin, stop_after_n_firings=None))
        assert tokens(fork.cabin, "Seats") == ["Ada"] and tokens(bus.cabin, "Door") == ["Ada"]

    def test_every_deep_copy_of_a_graph_is_a_new_net(self):
        cabin = build_cabin()
        copy = deepcopy(cabin)
        assert copy.net_id != cabin.net_id and copy.template == cabin.template
