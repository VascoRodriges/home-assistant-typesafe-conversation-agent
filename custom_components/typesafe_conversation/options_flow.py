"""Native HA settings UI; explicit overrides without rewriting YAML or ledgers."""

from copy import deepcopy
from pathlib import Path

import voluptuous as vol
import yaml
from homeassistant.config_entries import OptionsFlow
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    ObjectSelector,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from .household_policy import Catalog, PolicyError
from .settings import YAML_SCHEMA, effective_settings, normalize_settings

MODELS = {
    "decision": ["typesafe/jev-1.13"],
    "planner": [
        "openai/gpt-4o-mini",
        "google/gemini-3.1-flash-lite",
        "openai/gpt-5-mini",
    ],
    "answer": [
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "google/gemini-3.1-flash-lite",
        "openai/gpt-5-mini",
    ],
    "web": ["openai/gpt-4o-mini"],
}


def number(low, high, step):
    return NumberSelector(
        NumberSelectorConfig(min=low, max=high, step=step, mode="box")
    )


class TypeSafeOptionsFlow(OptionsFlow):
    """Each section commits atomically, reloads the same agent and keeps its budget."""

    async def async_step_init(self, user_input=None):
        sections = ["general"]
        if "household" in self.config_entry.data:
            sections += ["models", "budget", "history", "routing", "catalog"]
        if self.config_entry.options:
            sections.append("restore_yaml")
        return self.async_show_menu(step_id="init", menu_options=sections)

    def _current(self):
        return effective_settings(self.config_entry)

    async def _save(self, section, user_input):
        options = deepcopy(dict(self.config_entry.options))
        if section == "general":
            options.update(user_input)
        else:
            household = options.setdefault("household", {})
            if section in ("models", "budget", "history"):
                if section == "models":
                    household["models"] = {key: user_input[key] for key in MODELS}
                    household["prices"] = user_input["prices"]
                else:
                    household[section] = user_input
            else:
                household.update(user_input)
        # Validate the COMPLETE prospective settings, not just this UI section.
        # Temporarily merging avoids changing a live entry before validation.
        proxy = type(
            "Settings", (), {"data": self.config_entry.data, "options": options}
        )()
        settings = normalize_settings(YAML_SCHEMA(effective_settings(proxy)))
        if household := settings.get("household"):
            if not set(household["models"].values()) <= household["prices"].keys():
                raise ValueError("Every selected model needs a price ceiling")
            if not household["execution_switch"].startswith("input_boolean."):
                raise ValueError("Execution gate must be an input_boolean")
            if not household["status_sensor"].startswith("sensor."):
                raise ValueError("Status entity must be a sensor")
            if section == "catalog":

                def validate_catalog():
                    path = Path(self.hass.config.path(household["script_catalog"]))
                    if not path.resolve().is_relative_to(
                        Path(self.hass.config.config_dir).resolve()
                    ):
                        raise ValueError("Catalog must remain inside HA config")
                    with path.open(encoding="utf-8-sig") as source:
                        Catalog(
                            yaml.safe_load(source)["script"], household["capabilities"]
                        )

                await self.hass.async_add_executor_job(validate_catalog)
        return self.async_create_entry(title="", data=options)

    async def _form(self, section, fields, user_input):
        errors = {}
        if user_input is not None:
            try:
                return await self._save(section, vol.Schema(fields)(user_input))
            except (
                ValueError,
                vol.Invalid,
                TypeError,
                KeyError,
                OSError,
                PolicyError,
                yaml.YAMLError,
            ):
                errors["base"] = "invalid_config"
        return self.async_show_form(
            step_id=section, data_schema=vol.Schema(fields), errors=errors
        )

    async def async_step_general(self, user_input=None):
        current = self._current()
        return await self._form(
            "general",
            {
                vol.Required(key, default=current.get(key, default)): BooleanSelector()
                for key, default in {
                    "execution_enabled": False,
                    "always_confirm_risky": True,
                    "local_fallback_enabled": False,
                    "bypass_local_intents": False,
                    "inline_entity_descriptions": False,
                }.items()
            },
            user_input,
        )

    async def async_step_models(self, user_input=None):
        current = self._current()["household"]
        fields = {
            vol.Required(role, default=current["models"][role]): SelectSelector(
                SelectSelectorConfig(options=models)
            )
            for role, models in MODELS.items()
        }
        fields[vol.Required("prices", default=current["prices"])] = ObjectSelector()
        return await self._form("models", fields, user_input)

    async def async_step_budget(self, user_input=None):
        current = self._current()["household"]["budget"]
        return await self._form(
            "budget",
            {
                vol.Required(key, default=current[key]): number(0.001, maximum, 0.001)
                for key, maximum in {
                    "request_usd": 1,
                    "daily_usd": 10,
                    "monthly_usd": 100,
                }.items()
            },
            user_input,
        )

    async def async_step_history(self, user_input=None):
        current = self._current()["household"].get("history", {})
        return await self._form(
            "history",
            {
                vol.Required(
                    "enabled", default=current.get("enabled", False)
                ): BooleanSelector(),
                vol.Required("max_days", default=current.get("max_days", 7)): number(
                    1, 31, 1
                ),
                vol.Required(
                    "max_age_minutes", default=current.get("max_age_minutes", 120)
                ): number(1, 1440, 1),
            },
            user_input,
        )

    async def async_step_routing(self, user_input=None):
        current = self._current()["household"]
        fields = {
            vol.Required(key, default=current.get(key, default)): number(low, 1, 0.01)
            for key, default, low in (
                ("review_threshold", 0.8, 0.8),
                ("fast_control_threshold", 0.9, 0.8),
                ("fast_read_threshold", 0.85, 0.75),
                ("fast_margin", 0.15, 0.1),
            )
        }
        fields[
            vol.Required("web_enabled", default=current.get("web_enabled", True))
        ] = BooleanSelector()
        fields[
            vol.Required("instructions", default=current.get("instructions", ""))
        ] = TextSelector(TextSelectorConfig(multiline=True))
        return await self._form("routing", fields, user_input)

    async def async_step_catalog(self, user_input=None):
        current = self._current()["household"]
        fields = {
            vol.Required(key, default=current[key]): TextSelector()
            for key in ("script_catalog", "execution_switch", "status_sensor")
        }
        fields.update(
            {
                vol.Required(key, default=current.get(key, {})): ObjectSelector()
                for key in ("capabilities", "read_entities", "extra_context_entities")
            }
        )
        return await self._form("catalog", fields, user_input)

    async def async_step_restore_yaml(self, user_input=None):
        if user_input is not None and user_input.get("confirm"):
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="restore_yaml",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): BooleanSelector()}
            ),
        )
