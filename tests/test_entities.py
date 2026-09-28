"""The catalog, against a real Home Assistant instance."""

from __future__ import annotations

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.typesafe_conversation.entities import EntityCatalog


async def _setup(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    await hass.async_block_till_done()


async def test_only_exposed_entities_are_in_the_catalog(hass: HomeAssistant):
    await _setup(hass)
    registry = er.async_get(hass)
    shown = registry.async_get_or_create("light", "demo", "shown")
    hidden = registry.async_get_or_create("light", "demo", "hidden")
    hass.states.async_set(shown.entity_id, "on", {"friendly_name": "Shown Light"})
    hass.states.async_set(hidden.entity_id, "off", {"friendly_name": "Hidden Light"})
    async_expose_entity(hass, conversation.DOMAIN, shown.entity_id, True)
    async_expose_entity(hass, conversation.DOMAIN, hidden.entity_id, False)

    catalog = EntityCatalog(hass, conversation.DOMAIN)
    ids = {e.entity_id for e in catalog.entities}
    assert shown.entity_id in ids
    assert hidden.entity_id not in ids


async def test_entity_takes_its_area_and_state_is_read_fresh(hass: HomeAssistant):
    await _setup(hass)
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("switch", "demo", "coffee")
    registry.async_update_entity(entry.entity_id, area_id=kitchen.id)
    hass.states.async_set(entry.entity_id, "off", {"friendly_name": "Coffee Maker"})
    async_expose_entity(hass, conversation.DOMAIN, entry.entity_id, True)

    catalog = EntityCatalog(hass, conversation.DOMAIN)
    (found,) = [e for e in catalog.entities if e.entity_id == entry.entity_id]
    assert found.area_id == kitchen.id
    assert found.area_name == "Kitchen"
    assert catalog.snapshot()["entities"][0]["state"] == "off"

    # Structure is cached, but state must not be.
    generation = catalog.generation
    hass.states.async_set(entry.entity_id, "on", {"friendly_name": "Coffee Maker"})
    assert catalog.snapshot()["entities"][0]["state"] == "on"
    assert catalog.generation == generation, "a state change must not rebuild"


async def test_registry_change_invalidates_the_catalog(hass: HomeAssistant):
    await _setup(hass)
    registry = er.async_get(hass)
    first = registry.async_get_or_create("light", "demo", "one")
    hass.states.async_set(first.entity_id, "on", {"friendly_name": "One"})
    async_expose_entity(hass, conversation.DOMAIN, first.entity_id, True)

    catalog = EntityCatalog(hass, conversation.DOMAIN)
    catalog.async_start()
    assert len(catalog.entities) == 1
    generation = catalog.generation

    second = registry.async_get_or_create("light", "demo", "two")
    hass.states.async_set(second.entity_id, "on", {"friendly_name": "Two"})
    async_expose_entity(hass, conversation.DOMAIN, second.entity_id, True)
    await hass.async_block_till_done()

    assert len(catalog.entities) == 2
    assert catalog.generation > generation
    catalog.async_stop()


async def test_attributes_are_pruned_per_domain(hass: HomeAssistant):
    await _setup(hass)
    registry = er.async_get(hass)
    light = registry.async_get_or_create("light", "demo", "lamp")
    hass.states.async_set(
        light.entity_id,
        "on",
        {
            "friendly_name": "Lamp",
            "brightness": 128,
            "volume_level": 0.5,  # belongs to media_player, must be dropped
            "supported_features": 63,
        },
    )
    async_expose_entity(hass, conversation.DOMAIN, light.entity_id, True)

    catalog = EntityCatalog(hass, conversation.DOMAIN)
    attrs = catalog.snapshot()["entities"][0]["attrs"]
    assert attrs == {"brightness": 128}
