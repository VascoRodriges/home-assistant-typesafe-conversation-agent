"""End to end: a real Home Assistant, the real integration, a mocked System One API."""

from __future__ import annotations

import json

import pytest
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from conftest import ANSWERS
from custom_components.typesafe_conversation.const import (
    CONF_ALWAYS_CONFIRM_RISKY,
    CONF_API_KEY,
    DOMAIN,
    TYPESAFE_API_URL,
    TYPESAFE_MODELS_URL,
)


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    return None


def _recorded(name: str) -> dict:
    return json.loads((ANSWERS / f"{name}.json").read_text())["response"]


async def _setup_home(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    await hass.async_block_till_done()

    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    registry = er.async_get(hass)
    coffee = registry.async_get_or_create(
        "switch", "demo", "coffee", suggested_object_id="coffee_maker"
    )
    registry.async_update_entity(
        coffee.entity_id, area_id=kitchen.id, name="Coffee Maker"
    )
    hass.states.async_set(coffee.entity_id, "off", {"friendly_name": "Coffee Maker"})
    async_expose_entity(hass, conversation.DOMAIN, coffee.entity_id, True)


async def _add_entry(
    hass: HomeAssistant, mocker: AiohttpClientMocker, **data
) -> MockConfigEntry:
    mocker.get(TYPESAFE_MODELS_URL, json={"models": [{"name": "jev-latest"}]})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "sk-test", **data},
        subentries_data=[
            ConfigSubentryData(
                subentry_type="conversation",
                title="TypeSafe Conversation",
                data={CONF_ALWAYS_CONFIRM_RISKY: True},
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_the_agent_registers_and_advertises_control(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """CONTROL is what makes Home Assistant route the useful traffic here."""
    await _setup_home(hass)
    await _add_entry(hass, aioclient_mock)

    state = hass.states.get("conversation.typesafe_conversation")
    assert state is not None
    assert (
        state.attributes["supported_features"]
        & conversation.ConversationEntityFeature.CONTROL
    )


async def test_a_command_reaches_the_service(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """'get the coffee boiling' - the demo's canonical case, end to end."""
    await _setup_home(hass)
    await _add_entry(hass, aioclient_mock)
    aioclient_mock.post(TYPESAFE_API_URL, json=_recorded("get_the_coffee_boiling"))
    calls = async_mock_service(hass, "switch", "turn_on")

    result = await conversation.async_converse(
        hass, "get the coffee boiling", None, None,
        agent_id="conversation.typesafe_conversation",
    )

    assert len(calls) == 1
    assert calls[0].data[ATTR_ENTITY_ID] == ["switch.coffee_maker"]
    assert result.response.response_type is not None


async def test_an_empty_utterance_spends_no_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup_home(hass)
    await _add_entry(hass, aioclient_mock)
    before = len(aioclient_mock.mock_calls)

    result = await conversation.async_converse(
        hass, "   ", None, None, agent_id="conversation.typesafe_conversation"
    )
    assert len(aioclient_mock.mock_calls) == before, "no API call for empty input"
    assert result.response.error_code is not None


async def test_jev_being_down_falls_back_rather_than_failing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """An outage must degrade to the sentence matcher, never to a question."""
    await _setup_home(hass)
    await _add_entry(hass, aioclient_mock)
    aioclient_mock.post(TYPESAFE_API_URL, status=503, text="down")

    result = await conversation.async_converse(
        hass, "turn on the coffee maker", None, None,
        agent_id="conversation.typesafe_conversation",
    )
    # No exception, and a response the pipeline can speak.
    assert result.response is not None
    assert result.continue_conversation is False


async def test_a_risky_action_asks_first(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    await _setup_home(hass)
    registry = er.async_get(hass)
    lock = registry.async_get_or_create(
        "lock", "demo", "front", suggested_object_id="front_door"
    )
    registry.async_update_entity(lock.entity_id, name="Front Door")
    hass.states.async_set(lock.entity_id, "locked", {"friendly_name": "Front Door"})
    async_expose_entity(hass, conversation.DOMAIN, lock.entity_id, True)

    await _add_entry(hass, aioclient_mock)
    aioclient_mock.post(TYPESAFE_API_URL, json=_recorded("unlock_the_front_door"))
    calls = async_mock_service(hass, "lock", "unlock")

    result = await conversation.async_converse(
        hass, "unlock the front door", None, None,
        agent_id="conversation.typesafe_conversation",
    )
    assert not calls, "must not unlock before the user confirms"
    assert result.continue_conversation is True
    assert "unlock" in result.response.speech["plain"]["speech"].lower()


async def test_the_catalog_uses_home_assistants_conversation_domain(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    """Guard against submodule shadowing.

    This package has its own ``conversation.py`` platform. Forwarding the
    platform setup binds that submodule as an attribute of the package, which
    rebinds the module-global name ``conversation`` inside ``__init__.py``. If
    anything reads ``conversation.DOMAIN`` after that point it silently gets
    ``typesafe_conversation``, no entity matches the exposure check, and the agent
    sees an empty home. Hence CONVERSATION_DOMAIN.
    """
    await _setup_home(hass)
    await _add_entry(hass, aioclient_mock)

    catalog = hass.data[DOMAIN]["catalog"]
    assert catalog.assistant == "conversation"
    assert [e.entity_id for e in catalog.entities] == ["switch.coffee_maker"]
