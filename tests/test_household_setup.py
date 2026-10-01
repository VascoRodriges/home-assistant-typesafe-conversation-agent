"""Load the real adapter/platform through a synthetic YAML config entry."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.typesafe_conversation.const import DOMAIN, OPENROUTER_KEY_URL
from tests.test_household import synthetic_catalog


@pytest.fixture(autouse=True)
async def _dependencies(hass, enable_custom_integrations):
    assert await async_setup_component(hass, "homeassistant", {})


async def test_yaml_import_loads_capability_runtime_and_status(hass, aioclient_mock):
    scripts = synthetic_catalog(raw=True)
    target = Path(hass.config.path("synthetic-scripts.yaml"))
    target.parent.mkdir(parents=True, exist_ok=True)
    await hass.async_add_executor_job(
        target.write_text, yaml.safe_dump({"script": scripts}), "utf-8"
    )
    source = MockConfigEntry(domain="open_router", data={"api_key": "sk-synthetic"})
    source.add_to_hass(hass)
    aioclient_mock.get(OPENROUTER_KEY_URL, json={"data": {"limit_remaining": 1}})
    models = {
        "decision": "typesafe/jev-1.13",
        "planner": "openai/gpt-4o-mini",
        "answer": "openai/gpt-4o",
        "web": "openai/gpt-4o-mini",
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            "name": "Synthetic Capability Agent",
            "openrouter_entry_id": source.entry_id,
            "household": {
                "script_catalog": "synthetic-scripts.yaml",
                "execution_switch": "input_boolean.synthetic_execution",
                "models": models,
                "prices": {
                    model: {"input_per_million": 0.5, "output_per_million": 2}
                    for model in models.values()
                },
                "budget": {"request_usd": 0.035, "daily_usd": 0.25, "monthly_usd": 3},
                "capabilities": {
                    name: "light" if "lights" in name else "media" for name in scripts
                },
            },
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.runtime_data.household is not None
    assert not entry.runtime_data.household.execution_enabled
    assert entry.runtime_data.llm is None  # No unbudgeted warmup or alternate backend.
    status = await hass.services.async_call(
        DOMAIN, "status", {}, blocking=True, return_response=True
    )
    assert status["budget"]["month_usd"] == 0
    assert not status["execution_enabled"]
    assert hass.states.get("sensor.typesafe_conversation_budget") is not None
    assert hass.states.get("conversation.synthetic_capability_agent") is not None
    assert aioclient_mock.call_count == 2  # Import and runtime key validation only.
    assert "api_key" not in entry.data
    old_runtime = entry.runtime_data.household
    old_runtime.budget.reserve(0.002, 0)
    await old_runtime.store.async_save(old_runtime.budget.data)
    changed = {**entry.data, "execution_enabled": True}
    with patch(
        "custom_components.typesafe_conversation.async_integration_yaml_config",
        new=AsyncMock(return_value={DOMAIN: changed}),
    ):
        await hass.services.async_call(DOMAIN, "reload_yaml", {}, blocking=True)
        await hass.async_block_till_done()
    assert not old_runtime.active
    assert entry.runtime_data.household is not old_runtime
    assert entry.runtime_data.household.execution_enabled
    assert entry.runtime_data.household.budget.status()["month_usd"] == 0.002
    assert hass.services.has_service(DOMAIN, "reload_yaml")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service(DOMAIN, "preview")
    assert not hass.services.has_service(DOMAIN, "reload_yaml")
