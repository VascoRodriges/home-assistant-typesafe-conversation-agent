"""Synthetic configuration checks; never use credentials from a real home."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.typesafe_conversation.settings import (
    YAML_SCHEMA,
    normalize_settings,
    resolve_credentials,
)


def test_yaml_defaults_to_preview_and_pinned_jev():
    data = normalize_settings(YAML_SCHEMA({"api_key": "sk-or-test"}))
    assert data["provider"] == "openrouter"
    assert data["model"] == "typesafe/jev-1.13"
    assert data["execution_enabled"] is False


def test_existing_direct_entries_keep_their_provider():
    data = normalize_settings({"api_key": "sk-test"})
    assert data["provider"] == "typesafe"
    assert data["model"] == "jev-latest"


@pytest.mark.parametrize("model", ["typesafe/jev-router", "~typesafe/jev-latest"])
def test_decision_router_aliases_are_not_allowed(model):
    with pytest.raises(ValueError, match="pinned"):
        normalize_settings({"api_key": "k", "provider": "openrouter", "model": model})


@pytest.mark.parametrize("model", ["openrouter/auto", "typesafe/jev-router"])
def test_language_model_router_aliases_are_not_allowed(model):
    with pytest.raises(ValueError, match="explicit"):
        normalize_settings({"api_key": "k", "llm_model": model})


async def test_referenced_credentials_stay_out_of_persisted_settings(hass):
    entry = MockConfigEntry(domain="open_router", data={"api_key": "sk-or-synthetic"})
    entry.add_to_hass(hass)
    settings = normalize_settings(
        YAML_SCHEMA(
            {
                "openrouter_entry_id": entry.entry_id,
                "llm_backend": "openai_compatible",
                "llm_base_url": "https://openrouter.ai/api",
                "llm_model": "openai/gpt-4o",
                "llm_split_model": "openai/gpt-4o-mini",
            }
        )
    )
    resolved = resolve_credentials(hass, settings)
    assert resolved["api_key"] == "sk-or-synthetic"
    assert resolved["llm_api_key"] == "sk-or-synthetic"
    assert "api_key" not in settings and "llm_api_key" not in settings


async def test_reference_cannot_point_to_an_unrelated_integration(hass):
    entry = MockConfigEntry(domain="other", data={"api_key": "private"})
    entry.add_to_hass(hass)
    with pytest.raises(ValueError, match="does not exist"):
        resolve_credentials(hass, YAML_SCHEMA({"openrouter_entry_id": entry.entry_id}))
