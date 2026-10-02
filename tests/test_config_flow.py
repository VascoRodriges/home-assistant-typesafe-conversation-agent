"""YAML import lifetime, validated without a real home or API key."""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.typesafe_conversation.const import DOMAIN, OPENROUTER_KEY_URL


@pytest.fixture(autouse=True)
async def _custom_integrations(hass, enable_custom_integrations):
    # Conversation's startup hook reads the exposure store. Initialize that
    # dependency before loading a flow on an already-running test instance.
    assert await async_setup_component(hass, "homeassistant", {})


async def test_yaml_import_resolves_but_does_not_persist_referenced_key(
    hass, aioclient_mock
):
    source = MockConfigEntry(domain="open_router", data={"api_key": "sk-or-fixture"})
    source.add_to_hass(hass)
    aioclient_mock.get(OPENROUTER_KEY_URL, json={"data": {"limit_remaining": 1}})
    with patch(
        "custom_components.typesafe_conversation.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={
                "name": "Synthetic Assistant",
                "openrouter_entry_id": source.entry_id,
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["execution_enabled"] is False
    assert result["data"]["openrouter_entry_id"] == source.entry_id
    assert "api_key" not in result["data"]


async def test_yaml_reimport_replaces_removed_roles_and_disables_offline(
    hass, aioclient_mock
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        source=SOURCE_IMPORT,
        unique_id="yaml:Synthetic Assistant",
        data={
            "name": "Synthetic Assistant",
            "api_key": "sk-old",
            "provider": "openrouter",
            "execution_enabled": True,
            "llm_model": "openai/gpt-4o",
            "llm_api_key": "old-llm-key",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={"name": "Synthetic Assistant", "api_key": "sk-new"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["execution_enabled"] is False
    assert entry.data["api_key"] == "sk-new"
    assert "llm_model" not in entry.data and "llm_api_key" not in entry.data
    assert not aioclient_mock.mock_calls, (
        "Local safety settings do not need cloud access"
    )


async def test_invalid_yaml_does_not_make_a_provider_request(hass, aioclient_mock):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={"api_key": "k", "model": "typesafe/jev-router"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_config"
    assert not aioclient_mock.mock_calls


async def test_user_menu_offers_quickstart_first(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["quickstart", "native"]


@pytest.mark.parametrize("reference", [False, True])
async def test_quickstart_creates_safe_portable_agent_without_yaml(
    hass, aioclient_mock, reference
):
    source = MockConfigEntry(
        domain="open_router",
        data={"api_key": "sk-or-synthetic"},
        title="Synthetic source",
    )
    if reference:
        source.add_to_hass(hass)
    aioclient_mock.get(OPENROUTER_KEY_URL, json={"data": {"limit_remaining": 1}})
    menu = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    form = await hass.config_entries.flow.async_configure(
        menu["flow_id"], {"next_step_id": "quickstart"}
    )
    assert form["type"] is FlowResultType.FORM
    values = {"name": "Synthetic Assistant", "execution_enabled": False}
    if reference:
        values["openrouter_entry_id"] = source.entry_id
    else:
        values["api_key"] = "sk-or-synthetic"
    with patch(
        "custom_components.typesafe_conversation.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(form["flow_id"], values)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    config = result["data"]
    assert config["execution_enabled"] is False
    assert config["household"]["builtin_preset"] is True
    assert config["household"]["basic_entities"] == []
    assert config["household"]["read_entities"] == {}
    assert config["household"]["script_catalog"] == "builtin:lights_and_sensors"
    assert config["household"]["status_sensor"].startswith("sensor.typesafe_")
    assert len(result["result"].subentries) == 1
    if reference:
        assert config["openrouter_entry_id"] == source.entry_id
        assert "api_key" not in config
    else:
        assert config["api_key"] == "sk-or-synthetic"


async def test_quickstart_rejects_two_credential_sources(hass, aioclient_mock):
    source = MockConfigEntry(domain="open_router", data={"api_key": "synthetic"})
    source.add_to_hass(hass)
    menu = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    form = await hass.config_entries.flow.async_configure(
        menu["flow_id"], {"next_step_id": "quickstart"}
    )
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"],
        {
            "name": "Synthetic",
            "api_key": "synthetic",
            "openrouter_entry_id": source.entry_id,
            "execution_enabled": False,
        },
    )
    assert result["errors"] == {"base": "invalid_config"}
    assert not aioclient_mock.mock_calls
