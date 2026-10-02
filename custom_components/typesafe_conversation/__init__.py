"""The TypeSafe Conversation integration.

Puts a TypeSafe System One model in front of the LLM as the decision layer:
device commands and state queries resolve from typed answers in code, and only genuinely
generative work - splitting compound requests, answering general questions -
reaches an LLM.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.service import async_register_admin_service

from .const import (
    CONF_API_KEY,
    CONF_MODEL,
    CONF_PROVIDER,
    CONVERSATION_DOMAIN,
    DOMAIN,
    PROVIDER_TYPESAFE,
    TRACE_HISTORY,
    WARMUP_INTERVAL_SECONDS,
)
from .entities import EntityCatalog
from .household import CANDIDATES, HouseholdRuntime
from .household_policy import PolicyError
from .llm_backend import LLMBackend, create_backend
from .settings import YAML_SCHEMA, effective_settings, resolve_credentials
from .system_one import SystemOneAuthError, SystemOneClient, SystemOneError

PLATFORMS = [Platform.CONVERSATION]
CONFIG_SCHEMA = vol.Schema({vol.Optional(DOMAIN): YAML_SCHEMA}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Import one YAML-managed entry without storing referenced credentials."""
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "import"}, data=dict(config[DOMAIN])
            )
        )
    return True


@dataclass
class TypeSafeRuntimeData:
    """Everything one config entry needs at runtime."""

    client: SystemOneClient
    catalog: EntityCatalog
    llm: LLMBackend | None
    model: str
    household: HouseholdRuntime | None = None
    questions_cache: tuple[int, bool, dict[str, Any]] | None = None
    """Shared across turns: the question set is a pure function of the catalog."""

    traces: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=TRACE_HISTORY)
    )
    """Recent request traces, newest last.

    Home Assistant defines conversation traces but nothing reads them back -
    there is no websocket command and no UI - so they are write-only. Keeping
    our own ring buffer is what makes the diagnostics download useful."""


type TypeSafeConfigEntry = ConfigEntry[TypeSafeRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: TypeSafeConfigEntry) -> bool:
    """Set up TypeSafe Conversation from a config entry."""
    session = async_get_clientsession(hass)
    try:
        settings = resolve_credentials(hass, effective_settings(entry))
    except ValueError as err:
        raise ConfigEntryNotReady(str(err)) from err
    client = SystemOneClient(
        session,
        settings[CONF_API_KEY],
        settings[CONF_MODEL],
        provider=settings.get(CONF_PROVIDER, PROVIDER_TYPESAFE),
    )

    try:
        await client.async_validate()
    except SystemOneAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except SystemOneError as err:
        raise ConfigEntryNotReady(f"Could not reach TypeSafe: {err}") from err

    store = hass.data.setdefault(DOMAIN, {})
    catalog: EntityCatalog | None = store.get("catalog")
    if catalog is None:
        # One catalog for the whole instance: it describes Home Assistant, not
        # any particular config entry.
        catalog = EntityCatalog(hass, CONVERSATION_DOMAIN)
        catalog.async_start()
        store["catalog"] = catalog

    household = None
    if "household" in settings:
        household = HouseholdRuntime(
            hass,
            {
                **settings["household"],
                "openrouter_entry_id": settings["openrouter_entry_id"],
            },
            entry.entry_id,
            bool(settings.get("execution_enabled", False)),
        )
        try:
            await household.initialize()
        except (OSError, ValueError) as err:
            raise ConfigEntryNotReady(
                "Unable to initialize YAML catalog or budget"
            ) from err
    # Capability-mode model calls ALL go through the persisted budget gate.
    llm = None if household else create_backend(session, settings)
    entry.runtime_data = TypeSafeRuntimeData(
        client=client,
        catalog=catalog,
        llm=llm,
        model=settings[CONF_MODEL],
        household=household,
    )

    if household:
        _register_household_services(hass)

    if llm is not None:
        # A cold model load is seconds long and would be blamed on us, so pay
        # for it in the background and keep paying every 20 minutes.
        entry.async_create_background_task(
            hass, llm.async_warm_up(), "typesafe_llm_warmup", eager_start=False
        )

        async def _async_warm_up(_now: datetime) -> None:
            """Keep the model resident.

            This must be a coroutine function. async_track_time_interval
            classifies its action as a HassJob, and a plain sync callable is
            run in an executor thread - from which hass.async_create_task is
            not safe to call.
            """
            await llm.async_warm_up()

        entry.async_on_unload(
            async_track_time_interval(
                hass,
                _async_warm_up,
                timedelta(seconds=WARMUP_INTERVAL_SECONDS),
                name="typesafe_llm_warmup",
            )
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TypeSafeConfigEntry) -> bool:
    """Unload a config entry."""
    household = entry.runtime_data.household
    if household:
        # Stop queued turns and further actions, then drain the persisted budget
        # writer before a replacement runtime loads the same ledger.
        household.active = False
        async with household.lock:
            pass
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded and household:
        household.active = True
    if unloaded and entry.runtime_data.household:
        hass.states.async_remove(entry.runtime_data.household.config["status_sensor"])
        if not any(
            other.entry_id != entry.entry_id
            and getattr(other, "runtime_data", None)
            and other.runtime_data.household
            for other in hass.config_entries.async_entries(DOMAIN)
        ):
            for service in ("preview", "status", "reload_catalog", "reload_yaml"):
                hass.services.async_remove(DOMAIN, service)
    if unloaded and not [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]:
        # Last entry gone: stop listening for registry changes.
        store = hass.data.get(DOMAIN, {})
        if (catalog := store.pop("catalog", None)) is not None:
            catalog.async_stop()
    return unloaded


def _register_household_services(hass: HomeAssistant) -> None:
    """Services select exactly one entry; preview can never actuate."""

    def runtime(call):
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if getattr(entry, "runtime_data", None)
            and entry.runtime_data.household
            and (
                not call.data.get("entry_id") or entry.entry_id == call.data["entry_id"]
            )
        ]
        if len(entries) != 1:
            raise HomeAssistantError("Select one loaded TypeSafe capability entry")
        return entries[0].runtime_data.household

    async def preview(call):
        candidate = call.data.get("candidate_model")
        return await runtime(call).process(
            call.data["text"],
            context=call.context,
            preview=True,
            model_overrides={"planner": candidate} if candidate else None,
        )

    async def status(call):
        return runtime(call).status()

    async def reload_catalog(call):
        try:
            await runtime(call).reload_catalog()
        except (OSError, PolicyError) as err:
            raise HomeAssistantError(
                "Unable to reload YAML capability catalog"
            ) from err

    async def reload_yaml(_call):
        config = await async_integration_yaml_config(
            hass, DOMAIN, raise_on_failure=True
        )
        if DOMAIN not in config:
            raise HomeAssistantError("TypeSafe YAML configuration is missing")
        data = dict(config[DOMAIN])
        if not any(
            entry.unique_id == "yaml:" + data["name"]
            for entry in hass.config_entries.async_entries(DOMAIN)
        ):
            raise HomeAssistantError("Reload must keep the existing YAML agent name")
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "import"}, data=data
        )
        if result.get("reason") != "already_configured":
            raise HomeAssistantError("YAML import could not update the existing agent")

    selector = {vol.Optional("entry_id"): cv.string}
    hass.services.async_register(
        DOMAIN,
        "preview",
        preview,
        schema=vol.Schema(
            {
                **selector,
                vol.Required("text"): vol.All(cv.string, vol.Length(min=1, max=1200)),
                vol.Optional("candidate_model"): vol.In(CANDIDATES),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "status",
        status,
        schema=vol.Schema(selector),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "reload_catalog", reload_catalog, schema=vol.Schema(selector)
    )
    async_register_admin_service(hass, DOMAIN, "reload_yaml", reload_yaml)


async def _async_update_listener(
    hass: HomeAssistant, entry: TypeSafeConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


__all__ = ["TypeSafeConfigEntry", "TypeSafeRuntimeData"]
