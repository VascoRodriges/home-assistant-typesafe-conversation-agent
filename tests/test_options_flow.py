"""Native options dialogs: overrides are atomic and never rewrite YAML or budgets."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.typesafe_conversation.const import DOMAIN
from custom_components.typesafe_conversation.settings import effective_settings


def settings():
    return {
        "name": "Example",
        "provider": "openrouter",
        "openrouter_entry_id": "source",
        "execution_enabled": True,
        "household": {
            "script_catalog": "scripts.yaml",
            "execution_switch": "input_boolean.execution",
            "status_sensor": "sensor.budget",
            "models": {
                "decision": "typesafe/jev-1.13",
                "planner": "google/gemini-3.1-flash-lite",
                "answer": "openai/gpt-4o",
                "web": "openai/gpt-4o-mini",
            },
            "prices": {
                model: {"input_per_million": 1, "output_per_million": 2}
                for model in (
                    "typesafe/jev-1.13",
                    "google/gemini-3.1-flash-lite",
                    "openai/gpt-4o",
                    "openai/gpt-4o-mini",
                )
            },
            "budget": {"request_usd": 0.035, "daily_usd": 0.25, "monthly_usd": 3},
            "history": {"enabled": True, "max_days": 7, "max_age_minutes": 120},
            "capabilities": {},
            "read_entities": {"sensor.room": "Room temperature"},
        },
    }


def test_effective_settings_deep_merge_and_no_mutation():
    data = settings()
    before = deepcopy(data)
    options = {"household": {"history": {"max_days": 3}}}
    entry = SimpleNamespace(data=data, options=options)
    effective = effective_settings(entry)
    assert effective["household"]["history"] == {
        "enabled": True,
        "max_days": 3,
        "max_age_minutes": 120,
    }
    effective["household"]["read_entities"].clear()
    assert data == before
    assert options == {"household": {"history": {"max_days": 3}}}


@pytest.fixture
async def entry(hass, enable_custom_integrations):
    assert await async_setup_component(hass, "homeassistant", {})
    source = MockConfigEntry(
        domain="open_router", entry_id="source", data={"api_key": "sk-or-synthetic"}
    )
    source.add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, source=SOURCE_IMPORT, unique_id="yaml:Example", data=settings()
    )
    entry.add_to_hass(hass)
    return entry


async def open_section(hass, entry, section):
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": section}
    )


@pytest.mark.parametrize(
    "section", ["general", "models", "budget", "history", "routing", "catalog"]
)
async def test_imported_agent_shows_options_forms(hass, entry, section):
    result = await open_section(hass, entry, section)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == section
    assert "api_key" not in result["data_schema"].schema


async def test_history_save_retains_identity_data_other_options_and_ledger(hass, entry):
    original = deepcopy(dict(entry.data))
    hass.config_entries.async_update_entry(entry, options={"execution_enabled": False})
    result = await open_section(hass, entry, "history")
    with patch.object(hass.config_entries, "async_reload") as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"enabled": True, "max_days": 3, "max_age_minutes": 90}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data == original
    assert entry.unique_id == "yaml:Example"
    assert entry.options["execution_enabled"] is False
    assert effective_settings(entry)["household"]["history"]["max_days"] == 3
    assert not reload.called  # This unloaded test entry has no update listener.
    assert not list(Path(hass.config.config_dir).glob(".storage/*.budget"))


async def test_bad_history_rejected_without_entry_mutation(hass, entry):
    result = await open_section(hass, entry, "history")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": True, "max_days": 100, "max_age_minutes": 120}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_config"}
    assert not entry.options


async def test_missing_model_price_rejected(hass, entry):
    result = await open_section(hass, entry, "models")
    values = {
        **settings()["household"]["models"],
        "planner": "openai/gpt-5-mini",
        "prices": settings()["household"]["prices"],
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], values
    )
    assert result["errors"] == {"base": "invalid_config"}
    assert not entry.options


async def test_yaml_reimport_preserves_explicit_ui_override(hass, entry):
    hass.config_entries.async_update_entry(entry, options={"execution_enabled": False})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=settings()
    )
    assert result["reason"] == "already_configured"
    assert entry.data["execution_enabled"] is True
    assert effective_settings(entry)["execution_enabled"] is False


async def test_restore_yaml_requires_confirmation_and_keeps_budget_settings(
    hass, entry
):
    hass.config_entries.async_update_entry(entry, options={"execution_enabled": False})
    result = await open_section(hass, entry, "restore_yaml")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": False}
    )
    assert result["type"] is FlowResultType.FORM
    assert entry.options["execution_enabled"] is False
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not entry.options
    assert effective_settings(entry)["execution_enabled"] is True
    assert (
        effective_settings(entry)["household"]["budget"]
        == settings()["household"]["budget"]
    )
