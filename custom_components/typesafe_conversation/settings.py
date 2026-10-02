"""YAML settings and runtime-only credential resolution for the fork."""

from __future__ import annotations

from copy import deepcopy
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
from .presets import BASIC_CAPABILITIES, BASIC_CATALOG

PRICE_SCHEMA = vol.Schema(
    {
        vol.Required("input_per_million"): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=10)
        ),
        vol.Required("output_per_million"): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=30)
        ),
    }
)
HOUSEHOLD_SCHEMA = vol.Schema(
    {
        vol.Optional("builtin_preset", default=False): cv.boolean,
        vol.Optional("basic_entities", default=[]): [cv.entity_id],
        vol.Required("script_catalog"): cv.string,
        vol.Required("execution_switch"): cv.entity_id,
        vol.Optional(
            "status_sensor", default="sensor.typesafe_conversation_budget"
        ): cv.entity_id,
        vol.Required("models"): vol.Schema(
            {
                vol.Required("decision"): vol.In([DEFAULT_OPENROUTER_MODEL]),
                vol.Required("planner"): vol.In(
                    [
                        "openai/gpt-4o-mini",
                        "google/gemini-3.1-flash-lite",
                        "openai/gpt-5-mini",
                    ]
                ),
                vol.Required("answer"): vol.In(
                    [
                        "openai/gpt-4o",
                        "openai/gpt-4o-mini",
                        "google/gemini-3.1-flash-lite",
                        "openai/gpt-5-mini",
                    ]
                ),
                vol.Required("web"): vol.In(["openai/gpt-4o-mini"]),
            }
        ),
        vol.Required("prices"): {cv.string: PRICE_SCHEMA},
        vol.Required("budget"): vol.Schema(
            {
                vol.Required("request_usd"): vol.All(
                    vol.Coerce(float), vol.Range(min=0.001, max=1)
                ),
                vol.Required("daily_usd"): vol.All(
                    vol.Coerce(float), vol.Range(min=0.001, max=10)
                ),
                vol.Required("monthly_usd"): vol.All(
                    vol.Coerce(float), vol.Range(min=0.001, max=100)
                ),
            }
        ),
        vol.Required("capabilities"): {cv.slug: vol.In(["light", "media", "vacuum"])},
        vol.Optional("read_entities", default={}): {cv.entity_id: cv.string},
        vol.Optional("history", default={}): vol.Schema(
            {
                vol.Optional("enabled", default=False): cv.boolean,
                vol.Optional("max_days", default=7): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=31)
                ),
                vol.Optional("max_age_minutes", default=120): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=1440)
                ),
            }
        ),
        vol.Optional("extra_context_entities", default={}): {
            vol.In(["light", "media", "vacuum"]): [cv.entity_id]
        },
        vol.Optional("review_threshold", default=0.8): vol.All(
            vol.Coerce(float), vol.Range(min=0.8, max=1)
        ),
        vol.Optional("fast_control_threshold", default=0.90): vol.All(
            vol.Coerce(float), vol.Range(min=0.8, max=1)
        ),
        vol.Optional("fast_read_threshold", default=0.85): vol.All(
            vol.Coerce(float), vol.Range(min=0.75, max=1)
        ),
        vol.Optional("fast_margin", default=0.15): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=1)
        ),
        vol.Optional("web_enabled", default=True): cv.boolean,
        vol.Optional("instructions", default=""): cv.string,
        vol.Optional("migrate_cascade_budget", default=False): cv.boolean,
    }
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
        vol.Optional("household"): HOUSEHOLD_SCHEMA,
    }
)


def effective_settings(entry) -> dict[str, Any]:
    """UI overrides are explicit; YAML remains the baseline, never rewritten."""
    data = deepcopy(dict(entry.data))
    for key, value in entry.options.items():
        if key == "household":
            household = data.setdefault("household", {})
            for field, setting in value.items():
                if field in ("models", "budget", "history"):
                    household.setdefault(field, {}).update(deepcopy(setting))
                else:
                    household[field] = deepcopy(setting)
        else:
            data[key] = deepcopy(value)
    return data


def normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Keep older TypeSafe entries compatible; forbid implicit model routing."""
    data = dict(settings)
    provider = data.setdefault(CONF_PROVIDER, PROVIDER_TYPESAFE)
    if provider not in (PROVIDER_TYPESAFE, PROVIDER_OPENROUTER):
        raise ValueError("Unknown decision provider")
    if "household" in data:
        if provider != PROVIDER_OPENROUTER:
            raise ValueError("Capability mode requires OpenRouter")
        try:
            data["household"] = HOUSEHOLD_SCHEMA(data["household"])
        except vol.Invalid as err:
            raise ValueError("Invalid household capability configuration") from err
        household = data["household"]
        if household["builtin_preset"] and (
            household["script_catalog"] != BASIC_CATALOG
            or household["capabilities"] != BASIC_CAPABILITIES
            or household["extra_context_entities"]
            or any(
                entity.split(".")[0] not in ("light", "sensor")
                for entity in household["basic_entities"]
            )
        ):
            raise ValueError("Built-in profile is restricted to lights and sensors")
        if (
            not household["builtin_preset"]
            and household["script_catalog"] == BASIC_CATALOG
        ):
            raise ValueError("Built-in catalog requires the built-in profile")
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
