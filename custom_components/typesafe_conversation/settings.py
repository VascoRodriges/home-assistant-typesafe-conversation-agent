"""YAML settings and runtime-only credential resolution for the fork."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import (
    BACKEND_OLLAMA,
    BACKEND_OPENAI_COMPAT,
    CONF_ALWAYS_CONFIRM_RISKY,
    CONF_API_KEY,
    CONF_BYPASS_LOCAL_INTENTS,
    CONF_EXECUTION_ENABLED,
    CONF_INLINE_ENTITY_DESCRIPTIONS,
    CONF_LLM_API_KEY,
    CONF_LLM_BACKEND,
    CONF_LLM_BASE_URL,
    CONF_LLM_MODEL,
    CONF_LLM_SPLIT_MODEL,
    CONF_LLM_TIMEOUT,
    CONF_LOCAL_FALLBACK_ENABLED,
    CONF_MODEL,
    CONF_OPENROUTER_ENTRY_ID,
    CONF_PROVIDER,
    DEFAULT_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    PROVIDER_OPENROUTER,
    PROVIDER_TYPESAFE,
)

YAML_SCHEMA = vol.Schema(
    {
        vol.Optional("name", default="TypeSafe Conversation"): cv.string,
        vol.Optional(CONF_PROVIDER, default=PROVIDER_OPENROUTER): vol.In(
            (PROVIDER_TYPESAFE, PROVIDER_OPENROUTER)
        ),
        vol.Optional(CONF_MODEL): cv.string,
        vol.Optional(CONF_API_KEY): cv.string,
        vol.Optional(CONF_OPENROUTER_ENTRY_ID): cv.string,
        vol.Optional(CONF_EXECUTION_ENABLED, default=False): cv.boolean,
        vol.Optional(CONF_LOCAL_FALLBACK_ENABLED, default=False): cv.boolean,
        vol.Optional(CONF_ALWAYS_CONFIRM_RISKY, default=True): cv.boolean,
        vol.Optional(CONF_BYPASS_LOCAL_INTENTS, default=False): cv.boolean,
        vol.Optional(CONF_INLINE_ENTITY_DESCRIPTIONS, default=False): cv.boolean,
        vol.Optional(CONF_LLM_BACKEND): vol.In((BACKEND_OLLAMA, BACKEND_OPENAI_COMPAT)),
        vol.Optional(CONF_LLM_BASE_URL): cv.url,
        vol.Optional(CONF_LLM_MODEL): cv.string,
        vol.Optional(CONF_LLM_SPLIT_MODEL): cv.string,
        vol.Optional(CONF_LLM_API_KEY): cv.string,
        vol.Optional(CONF_LLM_TIMEOUT): vol.All(vol.Coerce(float), vol.Range(min=5)),
    }
)


def normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Keep older TypeSafe entries compatible; forbid implicit model routing."""
    data = dict(settings)
    provider = data.setdefault(CONF_PROVIDER, PROVIDER_TYPESAFE)
    if provider not in (PROVIDER_TYPESAFE, PROVIDER_OPENROUTER):
        raise ValueError("Unknown decision provider")
    data.setdefault(CONF_LOCAL_FALLBACK_ENABLED, provider == PROVIDER_TYPESAFE)
    data.setdefault(
        CONF_MODEL,
        DEFAULT_OPENROUTER_MODEL if provider == PROVIDER_OPENROUTER else DEFAULT_MODEL,
    )
    if provider == PROVIDER_OPENROUTER and data[CONF_MODEL] != DEFAULT_OPENROUTER_MODEL:
        raise ValueError("OpenRouter decisions require the pinned typesafe/jev-1.13")
    for key in (CONF_LLM_MODEL, CONF_LLM_SPLIT_MODEL):
        if key in data:
            model = data[key].strip()
            # Keep the selected pool under local control, never a provider router.
            if not model or "router" in model.lower() or "latest" in model.lower():
                raise ValueError("Select an explicit language model, not a router")
            data[key] = model
    reference = data.get(CONF_OPENROUTER_ENTRY_ID)
    if any(data.get(key) for key in (CONF_LLM_MODEL, CONF_LLM_SPLIT_MODEL)) and not all(
        data.get(key) for key in (CONF_LLM_MODEL, CONF_LLM_BASE_URL, CONF_LLM_BACKEND)
    ):
        raise ValueError(
            "Language model roles require backend, base URL and answer model"
        )
    if reference and (provider != PROVIDER_OPENROUTER or data.get(CONF_API_KEY)):
        raise ValueError("Choose either an OpenRouter entry reference or an API key")
    if not reference and not data.get(CONF_API_KEY):
        raise ValueError("An API key or OpenRouter entry reference is required")
    return data


def resolve_credentials(
    hass: HomeAssistant, settings: dict[str, Any]
) -> dict[str, Any]:
    """Resolve a HA OpenRouter key in memory, never copy it into the new entry."""
    data = normalize_settings(settings)
    if reference := data.get(CONF_OPENROUTER_ENTRY_ID):
        entry = hass.config_entries.async_get_entry(reference)
        if entry is None or entry.domain != "open_router":
            raise ValueError("The referenced OpenRouter integration does not exist")
        key = entry.data.get(CONF_API_KEY)
        if not isinstance(key, str) or not key:
            raise ValueError("The referenced integration has no usable API key")
        data[CONF_API_KEY] = key
    if (
        data[CONF_PROVIDER] == PROVIDER_OPENROUTER
        and data.get(CONF_LLM_BASE_URL, "").rstrip("/") == "https://openrouter.ai/api"
    ):
        data.setdefault(CONF_LLM_API_KEY, data[CONF_API_KEY])
    return data
