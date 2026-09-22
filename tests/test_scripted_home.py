"""Routing against a script-heavy catalog.

`tests/fixtures/scripted_home.json` is a synthetic catalog built to hold the
structural properties that a light-heavy fixture does not exercise, because
each of them broke the router once:

* **eight `script` entities.** A script's action Choice offers only two options,
  `run` and `not_targeted`, and Jev derives confidence as
  ``(n * p_top - 1) / (n - 1)``. Two options at p=0.66 therefore score 0.31,
  where seven options at the same probability score 0.60. Gating the action on
  confidence made every script command fall back.
* **two climate zones.** `HassClimateSetTemperature` is `single_target=True`,
  so an unqualified "set the thermostat" has no single answer.
* **eleven entities in no area at all**, so area-based targeting cannot be
  assumed.
* **`todo` and `weather` domains**, which have no on/off semantics.
* **an area named like a floor, with no floor of that name**, which used to make
  a floor-scoped request dead-end.

The answers in `fixtures/scripted_answers/` are real jev-1.13.0 responses to
this catalog, recorded with `scripts/calibrate.py --record`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.typesafe_conversation.entities import CatalogEntity
from custom_components.typesafe_conversation.extraction import extract
from custom_components.typesafe_conversation.system_one import SystemOneResponse, _parse_answer
from custom_components.typesafe_conversation.router import Route, route

FIXTURES = Path(__file__).parent / "fixtures"
ANSWERS = FIXTURES / "scripted_answers"


def _catalog():
    home = json.loads((FIXTURES / "scripted_home.json").read_text())
    names = {a["id"]: a["name"] for a in home["areas"]}
    floors = {a["id"]: a.get("floor") for a in home["areas"]}
    entities = tuple(
        CatalogEntity(
            entity_id=e["id"],
            name=e["name"],
            aliases=tuple(e["also"].split(", ")) if e.get("also") else (),
            area_id=e.get("area"),
            area_name=names.get(e.get("area")),
            floor_name=floors.get(e.get("area")),
            domain=e["domain"],
            device_class=(e.get("attrs") or {}).get("device_class"),
            supported_features=0,
        )
        for e in home["entities"]
    )
    catalog_floors = {a["id"]: a["floor"] for a in home["areas"] if a.get("floor")}
    return entities, catalog_floors


@pytest.fixture(name="scripted")
def scripted_fixture():
    entities, catalog_floors = _catalog()
    by_id = {e.entity_id: e for e in entities}
    domains = frozenset(e.domain for e in entities)

    def _route(slug: str):
        payload = json.loads((ANSWERS / f"{slug}.json").read_text())
        body = payload["response"]
        response = SystemOneResponse(
            model=body["model"],
            answers={k: _parse_answer(k, v) for k, v in body["answers"].items()},
            input_tokens=body.get("usage", {}).get("input_tokens", 0),
            output_tokens=0,
            latency_ms=0.0,
            raw=body,
        )
        utterance = payload["utterance"]
        return route(
            response,
            entities_by_id=by_id,
            extraction=extract(
                utterance,
                want_media="media_player" in domains,
                want_color="light" in domains,
            ),
            speaker_area_id=payload["spoken_from_area"],
            available_domains=domains,
            catalog_floors=catalog_floors,
        )

    return _route


DECIDED = (
    Route.COMMAND,
    Route.QUERY,
    Route.COMPOUND,
    Route.INFORMATION,
    Route.CONFIRM,
    Route.CLARIFY,
)


def test_the_fixture_still_has_the_properties_these_tests_rely_on():
    """Guard the fixture itself.

    Every test below depends on a structural property of the catalog rather
    than on a particular device, so a well-meaning edit that smooths the
    catalog out would silently stop exercising the bug it was built for.
    """
    entities, _ = _catalog()
    domains = [e.domain for e in entities]
    assert domains.count("script") >= 6, "need a domain whose action Choice has 2 options"
    assert domains.count("climate") == 2, "need two zones for the single_target clash"
    assert sum(1 for e in entities if e.area_id is None) >= 10
    assert "todo" in domains and "weather" in domains

    home = json.loads((FIXTURES / "scripted_home.json").read_text())
    floor_like = [
        a for a in home["areas"]
        if a["name"].endswith("Level") and "floor" not in a
    ]
    assert floor_like, "need an area named like a floor but with no floor set"


@pytest.mark.parametrize("slug", sorted(p.stem for p in ANSWERS.glob("*.json")))
def test_nothing_dead_ends(slug, scripted):
    """Every recorded utterance reaches a decision, not the fallback ladder."""
    plan = scripted(slug)
    assert plan.route in DECIDED, f"{slug}: {plan.reason} / {plan.trace}"


def test_a_two_option_action_choice_is_still_trusted(scripted):
    """The reason the action gate reads probability, not confidence."""
    plan = scripted("start_the_bedtime_routine")
    assert plan.route is Route.COMMAND
    assert plan.domain == "script"
    assert plan.target.entity.entity_id == "script.routine_bedtime"

    # Show the arithmetic the gate has to survive: a two-option Choice scores
    # far lower confidence than a large one at the same probability.
    answer = plan.trace["action"]
    assert answer["choice"] == "run"
    assert answer["top"]["run"] >= 0.55


def test_list_items_come_from_a_span_not_the_model(scripted):
    plan = scripted("add_milk_to_the_shopping_list")
    assert plan.route is Route.COMMAND
    assert plan.domain == "todo"
    assert plan.action == "add_item"
    # "milk", not "add milk": the chunker strips the verb before Jev picks.
    assert plan.text_slot == ("item", "milk")
    assert plan.target.entity.entity_id == "todo.list_one"


def test_an_area_named_like_a_floor_still_resolves(scripted):
    """"Upstairs" is an area here, and no floor carries that name.

    Jev reads the request as floor-scoped. Bailing out when the floor cannot be
    resolved dead-ended a perfectly good command; falling through to area
    targeting handles it.
    """
    plan = scripted("make_it_cooler_upstairs")
    assert plan.route is Route.COMMAND
    assert plan.domain == "climate"
    assert plan.action == "cooler"
    assert plan.relative_step is not None and plan.relative_step < 0


def test_a_door_counts_as_a_cover(scripted):
    """Home Assistant models doors, gates and windows as covers.

    The action question used to describe only blinds and garage doors, so a
    plain door came back `not_targeted` and the command fell back.
    """
    plan = scripted("open_the_side_door")
    assert plan.route in (Route.COMMAND, Route.CONFIRM)
    assert plan.domain == "cover"
    assert plan.action == "open"


def test_two_zones_are_not_silently_picked_between(scripted):
    """With no zone named, either ask or say which one was chosen."""
    plan = scripted("set_the_thermostat_to_70")
    assert plan.route in (Route.COMMAND, Route.CLARIFY)
    if plan.route is Route.COMMAND:
        assert plan.name_target_in_speech, "must name a target it is unsure of"
        assert plan.value == 70.0


def test_general_knowledge_reaches_the_llm(scripted):
    assert scripted("who_won_the_world_cup_in_1998").route is Route.INFORMATION
    # "what's the weather" sits near the category threshold; either the
    # information route or the fallback ladder ends up at the LLM.
    assert scripted("what_s_the_weather").route in (
        Route.INFORMATION,
        Route.FALLBACK,
    )
