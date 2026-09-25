"""Replay recorded Jev answers through the router.

Every fixture here is a real response from jev-1.13.0 against
``tests/fixtures/home.json``. If a threshold in const.py changes, these tests
say exactly which utterances start behaving differently.
"""

from __future__ import annotations

import pytest

from conftest import load_response
from custom_components.typesafe_conversation.extraction import extract
from custom_components.typesafe_conversation.router import Route, route


def _route(name: str, entities_by_id, available_domains):
    response, payload = load_response(name)
    utterance = payload["utterance"]
    extraction = extract(
        utterance,
        want_media="media_player" in available_domains,
        want_color="light" in available_domains,
    )
    plan = route(
        response,
        entities_by_id=entities_by_id,
        extraction=extraction,
        speaker_area_id=payload["spoken_from_area"],
        available_domains=available_domains,
    )
    return plan, utterance


# (fixture, expected route, expected domain, expected action)
CASES = [
    ("get_the_coffee_boiling", Route.COMMAND, "switch", "turn_on"),
    ("it_s_too_bright_in_here", Route.COMMAND, "light", "dimmer"),
    ("set_the_living_room_lights_to_30", Route.COMMAND, "light", "set_brightness"),
    ("make_the_lights_a_bit_warmer", Route.COMMAND, "light", "set_color"),
    ("set_the_thermostat_to_21_degrees", Route.COMMAND, "climate", "set_temperature"),
    ("turn_the_volume_down_a_bit", Route.COMMAND, "media_player", "quieter"),
    ("unlock_the_front_door", Route.CONFIRM, "lock", "unlock"),
    ("play_some_jazz_in_the_kitchen", Route.COMMAND, "media_player", "search_and_play"),
    ("goodnight", Route.COMMAND, "script", "run"),
    ("turn_off_all_the_lights_and_lock_the_front_door", Route.COMPOUND, None, None),
    ("dim_the_bedroom_lamp_and_start_the_dishwasher", Route.COMPOUND, None, None),
    ("is_the_garage_door_open", Route.QUERY, None, None),
    ("what_s_the_temperature_in_the_bedroom", Route.QUERY, None, None),
    ("how_many_lights_are_on", Route.QUERY, None, None),
    ("what_time_is_it", Route.QUERY, None, None),
    ("is_everything_locked_up", Route.QUERY, None, None),
    ("who_won_the_world_cup_in_1998", Route.INFORMATION, None, None),
    ("never_mind", Route.CANCEL, None, None),
    ("asdfgh", Route.FALLBACK, None, None),
]


@pytest.mark.parametrize(("name", "expected", "domain", "action"), CASES)
def test_routes(name, expected, domain, action, entities_by_id, available_domains):
    plan, utterance = _route(name, entities_by_id, available_domains)
    assert plan.route is expected, f"{utterance!r}: {plan.reason} / {plan.trace}"
    if domain is not None:
        assert plan.domain == domain, utterance
    if action is not None:
        assert plan.action == action, utterance


def test_coffee_maker_resolves_to_the_exact_entity(entities_by_id, available_domains):
    plan, _ = _route("get_the_coffee_boiling", entities_by_id, available_domains)
    assert plan.target.entity is not None
    assert plan.target.entity.entity_id == "switch.coffee_maker"
    # High confidence: no need to spell the target out loud.
    assert plan.name_target_in_speech is False


def test_absolute_brightness_carries_the_value(entities_by_id, available_domains):
    plan, _ = _route(
        "set_the_living_room_lights_to_30", entities_by_id, available_domains
    )
    assert plan.value == 30.0
    assert plan.value_unit == "%"
    assert plan.target.area_id == "living_room"


def test_thermostat_value_and_unit(entities_by_id, available_domains):
    plan, _ = _route(
        "set_the_thermostat_to_21_degrees", entities_by_id, available_domains
    )
    assert plan.value == 21.0
    # "degrees" with no letter: the executor resolves the unit from the entity,
    # never from the model.
    assert plan.value_unit == "deg"
    assert plan.spec is not None and plan.spec.single_target


def test_relative_change_becomes_a_signed_step(entities_by_id, available_domains):
    plan, _ = _route("turn_the_volume_down_a_bit", entities_by_id, available_domains)
    assert plan.relative_step == -10, "'a bit' is the slight magnitude"
    plan, _ = _route("it_s_too_bright_in_here", entities_by_id, available_domains)
    assert plan.relative_step is not None and plan.relative_step < 0


def test_warmer_light_becomes_a_colour_temperature(entities_by_id, available_domains):
    plan, _ = _route("make_the_lights_a_bit_warmer", entities_by_id, available_domains)
    assert plan.color_temp_kelvin == 2700
    assert plan.text_slot is None


def test_media_search_query_is_a_verbatim_span(entities_by_id, available_domains):
    plan, _ = _route("play_some_jazz_in_the_kitchen", entities_by_id, available_domains)
    assert plan.text_slot == ("search_query", "some jazz")

    # This fixture has no media_player in the kitchen - both live in the living
    # room - so a hard `area` slot would be a guaranteed MatchFailedError.
    # search_and_play is single-target, so the area becomes a preference and
    # Home Assistant picks a real player while still favouring the kitchen.
    assert plan.target.area_id is None
    assert plan.preferred_area_id == "kitchen"
    assert plan.trace["area_resolution"] == "prefer_area"


def test_unlocking_asks_before_acting(entities_by_id, available_domains):
    """A risky action needs more than ordinary confidence to happen silently."""
    plan, _ = _route("unlock_the_front_door", entities_by_id, available_domains)
    assert plan.route is Route.CONFIRM
    assert plan.trace["risky"] > 0.9


def test_ambiguous_target_asks_which_one(entities_by_id, available_domains):
    plan, _ = _route("turn_on_the_thing_in_the_corner", entities_by_id, available_domains)
    assert plan.route in (Route.CLARIFY, Route.FALLBACK)
    if plan.route is Route.CLARIFY:
        assert len(plan.options) == 2


def test_whole_house_command_never_lacks_a_target(entities_by_id, available_domains):
    """'turn off everything' names no domain.

    An intent with no name/area/floor/domain constraint raises
    IntentHandleError, so the plan must say whole_house and let the executor
    fan out over the domains that exist.
    """
    plan, _ = _route("turn_off_everything", entities_by_id, available_domains)
    if plan.route is Route.COMMAND:
        assert plan.target.whole_house is True
        assert plan.action == "turn_off"


def test_query_kinds(entities_by_id, available_domains):
    for name, kind in [
        ("is_the_garage_door_open", "device_state"),
        ("what_s_the_temperature_in_the_bedroom", "temperature"),
        ("how_many_lights_are_on", "count"),
        ("what_time_is_it", "time_or_date"),
        ("is_everything_locked_up", "needs_prose"),
    ]:
        plan, utterance = _route(name, entities_by_id, available_domains)
        assert plan.query_kind == kind, f"{utterance!r} -> {plan.query_kind}"


def test_unread_branches_are_ignored(entities_by_id, available_domains):
    """'is everything locked up' is a query.

    action_lock comes back "lock" at 0.99 - a confident answer to a question
    nobody asked. The router must not act on it. This is the speculative
    fan-out contract: wrong answers on unread branches are free.
    """
    response, _ = load_response("is_everything_locked_up")
    assert response.choice("action_lock").choice == "lock"
    plan, _ = _route("is_everything_locked_up", entities_by_id, available_domains)
    assert plan.route is Route.QUERY
    assert plan.action is None
