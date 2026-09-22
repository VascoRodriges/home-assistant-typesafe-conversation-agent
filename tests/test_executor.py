"""Execution, against real Home Assistant intent handlers.

The central claim of this integration's targeting is that an ``entity_id`` can
be passed in the ``name`` slot and will resolve exactly. These tests hold it to
that, including the case it exists to solve: two entities with the same name.
"""

from __future__ import annotations

import pytest
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er, intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.typesafe_conversation.actions import spec_for
from custom_components.typesafe_conversation.entities import EntityCatalog
from custom_components.typesafe_conversation.executor import async_execute, build_slots
from custom_components.typesafe_conversation.router import Plan, Route, Target


async def _setup(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assert await async_setup_component(hass, "intent", {})
    await hass.async_block_till_done()


def _input(hass: HomeAssistant) -> conversation.ConversationInput:
    from homeassistant.core import Context

    return conversation.ConversationInput(
        text="test",
        context=Context(),
        conversation_id=None,
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="conversation.typesafe",
    )


def _expose(hass: HomeAssistant, entity_id: str) -> None:
    async_expose_entity(hass, conversation.DOMAIN, entity_id, True)


async def test_entity_id_in_the_name_slot_resolves_exactly(hass: HomeAssistant):
    """Two lights share a name; only the one we targeted may be switched."""
    await _setup(hass)
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    bedroom = areas.async_create("Bedroom")
    registry = er.async_get(hass)

    a = registry.async_get_or_create("light", "demo", "a")
    b = registry.async_get_or_create("light", "demo", "b")
    registry.async_update_entity(a.entity_id, area_id=kitchen.id)
    registry.async_update_entity(b.entity_id, area_id=bedroom.id)
    # Deliberately identical friendly names.
    hass.states.async_set(a.entity_id, "off", {"friendly_name": "Ceiling Light"})
    hass.states.async_set(b.entity_id, "off", {"friendly_name": "Ceiling Light"})
    _expose(hass, a.entity_id)
    _expose(hass, b.entity_id)

    catalog = EntityCatalog(hass, conversation.DOMAIN)
    target_entity = next(e for e in catalog.entities if e.entity_id == b.entity_id)

    calls = async_mock_service(hass, "light", "turn_on")
    plan = Plan(
        Route.COMMAND,
        domain="light",
        action="turn_on",
        spec=spec_for("light", "turn_on"),
        target=Target(entity=target_entity, area_id=target_entity.area_id,
                      domain="light"),
    )
    await async_execute(hass, plan, _input(hass), catalog)

    assert len(calls) == 1
    assert calls[0].data[ATTR_ENTITY_ID] == [b.entity_id], (
        "the duplicate name must not win"
    )


async def test_friendly_text_rides_along_for_the_response(hass: HomeAssistant):
    """The id targets; the text is what the user hears."""
    await _setup(hass)
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("switch", "demo", "coffee")
    hass.states.async_set(entry.entity_id, "off", {"friendly_name": "Coffee Maker"})
    _expose(hass, entry.entity_id)

    catalog = EntityCatalog(hass, conversation.DOMAIN)
    entity = catalog.entities[0]
    slots = build_slots(Target(entity=entity, domain="switch"))
    assert slots["name"]["value"] == entry.entity_id
    assert slots["name"]["text"] == "Coffee Maker"


async def test_brightness_percentage_reaches_the_service(hass: HomeAssistant):
    await _setup(hass)
    assert await async_setup_component(hass, "light", {})
    # Domain intents live on the integration's intent platform; the intent
    # component loads them lazily, so nudge it before asserting on them.
    from homeassistant.components.light import intent as light_intent

    await light_intent.async_setup_intents(hass)
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("light", "demo", "lamp")
    hass.states.async_set(entry.entity_id, "on", {"friendly_name": "Lamp"})
    _expose(hass, entry.entity_id)

    catalog = EntityCatalog(hass, conversation.DOMAIN)
    calls = async_mock_service(hass, "light", "turn_on")
    plan = Plan(
        Route.COMMAND,
        domain="light",
        action="set_brightness",
        spec=spec_for("light", "set_brightness"),
        target=Target(entity=catalog.entities[0], domain="light"),
        value=30.0,
        value_unit="%",
    )
    await async_execute(hass, plan, _input(hass), catalog)
    assert calls[0].data["brightness_pct"] == 30


async def test_whole_house_always_carries_a_domain(hass: HomeAssistant):
    """An intent with no constraint at all raises IntentHandleError.

    'turn off everything' names no domain, so the executor fans out over the
    domains the home actually has rather than sending an empty slot set.
    """
    await _setup(hass)
    registry = er.async_get(hass)
    light = registry.async_get_or_create("light", "demo", "l")
    switch = registry.async_get_or_create("switch", "demo", "s")
    hass.states.async_set(light.entity_id, "on", {"friendly_name": "L"})
    hass.states.async_set(switch.entity_id, "on", {"friendly_name": "S"})
    _expose(hass, light.entity_id)
    _expose(hass, switch.entity_id)

    catalog = EntityCatalog(hass, conversation.DOMAIN)
    light_calls = async_mock_service(hass, "light", "turn_off")
    switch_calls = async_mock_service(hass, "switch", "turn_off")

    plan = Plan(
        Route.COMMAND,
        action="turn_off",
        target=Target(whole_house=True),
    )
    response = await async_execute(hass, plan, _input(hass), catalog)
    assert len(light_calls) == 1
    assert len(switch_calls) == 1
    assert response.speech


async def test_empty_constraints_would_have_raised(hass: HomeAssistant):
    """Guard the assumption the whole-house fan-out is built on."""
    await _setup(hass)
    with pytest.raises(intent.IntentHandleError):
        await intent.async_handle(hass, "test", "HassTurnOff", {})
