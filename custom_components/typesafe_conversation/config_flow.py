"""Config and subentry flows for TypeSafe Conversation."""

from __future__ import annotations

from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    ANSWER_TIMEOUT,
    BACKEND_OLLAMA,
    BACKEND_OPENAI_COMPAT,
    CONF_ALWAYS_CONFIRM_RISKY,
    CONF_API_KEY,
    CONF_BYPASS_LOCAL_INTENTS,
    CONF_INLINE_ENTITY_DESCRIPTIONS,
    CONF_LLM_API_KEY,
    CONF_LLM_BACKEND,
    CONF_LLM_BASE_URL,
    CONF_LLM_MODEL,
    CONF_LLM_TIMEOUT,
    CONF_MODEL,
    DEFAULT_ALWAYS_CONFIRM_RISKY,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    DOMAIN,
    LOGGER,
    TYPESAFE_CONSOLE_URL,
)
from .system_one import SystemOneAuthError, SystemOneClient, SystemOneError

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_MODEL, default=DEFAULT_MODEL): TextSelector(),
    }
)

STEP_LLM_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_LLM_BACKEND, default=BACKEND_OLLAMA): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=BACKEND_OLLAMA, label="Ollama"),
                    SelectOptionDict(
                        value=BACKEND_OPENAI_COMPAT,
                        label="OpenAI-compatible (OpenRouter, vLLM, ...)",
                    ),
                ]
            )
        ),
        vol.Optional(CONF_LLM_BASE_URL, default=DEFAULT_OLLAMA_URL): TextSelector(),
        vol.Optional(CONF_LLM_MODEL): TextSelector(),
        vol.Optional(CONF_LLM_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_LLM_TIMEOUT, default=ANSWER_TIMEOUT): NumberSelector(
            NumberSelectorConfig(min=5, max=180, step=5, unit_of_measurement="s")
        ),
    }
)


class TypeSafeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the TypeSafe credentials, then the optional LLM."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = SystemOneClient(
                async_get_clientsession(self.hass),
                user_input[CONF_API_KEY],
                user_input.get(CONF_MODEL, DEFAULT_MODEL),
            )
            try:
                await client.async_validate()
            except SystemOneAuthError:
                errors["base"] = "invalid_auth"
            except SystemOneError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected error validating the TypeSafe key")
                errors["base"] = "unknown"
            else:
                self._data = dict(user_input)
                return await self.async_step_llm()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            # hassfest rejects a literal URL inside a translated string, so the
            # console link is supplied here instead.
            description_placeholders={"console_url": TYPESAFE_CONSOLE_URL},
        )

    async def async_step_llm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the LLM used for compound splits and prose answers.

        Optional: without it the agent still handles every command and query,
        it just cannot split compound requests or answer general questions.
        """
        if user_input is not None:
            self._data.update(
                {k: v for k, v in user_input.items() if v not in (None, "")}
            )
            return self.async_create_entry(
                title="TypeSafe Conversation",
                data=self._data,
                subentries=[
                    {
                        "subentry_type": "conversation",
                        "title": "TypeSafe Conversation",
                        "data": {},
                        "unique_id": None,
                    }
                ],
            )
        return self.async_show_form(step_id="llm", data_schema=STEP_LLM_SCHEMA)

    @override
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            client = SystemOneClient(
                async_get_clientsession(self.hass),
                user_input[CONF_API_KEY],
                entry.data.get(CONF_MODEL, DEFAULT_MODEL),
            )
            try:
                await client.async_validate()
            except SystemOneAuthError:
                errors["base"] = "invalid_auth"
            except SystemOneError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"conversation": TypeSafeSubentryFlowHandler}


class TypeSafeSubentryFlowHandler(ConfigSubentryFlow):
    """One conversation agent, with its own tuning."""

    @property
    def _is_new(self) -> bool:
        return self.source == "user"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self.async_step_set_options(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self.async_step_set_options(user_input)

    async def async_step_set_options(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is not None:
            title = user_input.pop("name", "TypeSafe Conversation")
            if self._is_new:
                return self.async_create_entry(title=title, data=user_input)
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), data=user_input
            )

        current = {} if self._is_new else dict(self._get_reconfigure_subentry().data)
        schema = vol.Schema(
            {
                vol.Required(
                    "name",
                    default=(
                        "TypeSafe Conversation"
                        if self._is_new
                        else self._get_reconfigure_subentry().title
                    ),
                ): TextSelector(),
                vol.Optional(
                    CONF_ALWAYS_CONFIRM_RISKY,
                    default=current.get(
                        CONF_ALWAYS_CONFIRM_RISKY, DEFAULT_ALWAYS_CONFIRM_RISKY
                    ),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_INLINE_ENTITY_DESCRIPTIONS,
                    default=current.get(CONF_INLINE_ENTITY_DESCRIPTIONS, False),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_BYPASS_LOCAL_INTENTS,
                    default=current.get(CONF_BYPASS_LOCAL_INTENTS, False),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="set_options", data_schema=schema)
