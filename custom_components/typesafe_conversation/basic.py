"""Portable Assist-exposed light/sensor profile using the existing budgeted cascade."""

import math

from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ

from .household import HouseholdRuntime
from .household_history import HistoryQueries
from .household_policy import Catalog, PolicyError


def numeric_sensor(state):
    if state is None or not state.attributes.get("unit_of_measurement"):
        return False
    if state.state in ("unknown", "unavailable"):
        return True
    try:
        return math.isfinite(float(state.state))
    except TypeError, ValueError:
        return False


def builtin_catalog(entities, states, selected=()):
    """Only actual Assist-exposed entities supplied by the native catalog qualify."""
    targets, names, readable = {}, {}, {}
    selection = set(selected)
    for entity in entities:
        if selection and entity.entity_id not in selection:
            continue
        label = " · ".join(
            filter(None, (entity.area_name, entity.name, *entity.aliases))
        )
        if entity.domain == "sensor" and numeric_sensor(states.get(entity.entity_id)):
            readable[entity.entity_id] = label
        elif entity.domain == "light":
            key = "device_" + entity.entity_id.replace(".", "_")
            names[key] = label
            targets[key + ":main"] = [entity.entity_id]
            if entity.area_id:
                room = "area_" + entity.area_id
                names[room] = entity.area_name
                targets.setdefault(room + ":main", []).append(entity.entity_id)
    if (
        len(readable) + len({item for group in targets.values() for item in group})
        > 100
    ):
        raise PolicyError("Basic profile exceeds 100 entities; narrow its scope")
    fields = {
        "room": {"required": True, "selector": {"select": {"options": list(names)}}},
        "fixture": {"default": "main", "selector": {"select": {"options": ["main"]}}},
        "light_action": {
            "required": True,
            "selector": {"select": {"options": ["on", "off", "toggle", "adjust"]}},
        },
        "brightness_action": {
            "default": "unchanged",
            "selector": {
                "select": {"options": ["unchanged", "set", "increase", "decrease"]}
            },
        },
        "brightness_percent": {"selector": {"number": {"min": 1, "max": 100}}},
        "brightness_step_percent": {
            "default": 10,
            "selector": {"number": {"min": 1, "max": 50}},
        },
        "color_profile": {
            "default": "unchanged",
            "selector": {
                "select": {
                    "options": [
                        "unchanged",
                        "warm",
                        "neutral",
                        "cool",
                        "red",
                        "green",
                        "blue",
                        "relax",
                        "night",
                        "reading",
                        "focus",
                    ]
                }
            },
        },
    }
    scripts = (
        {
            "voice_llm_room_lights": {
                "description": (
                    "Operate an exposed light or the exposed lights of a named area. "
                    "Relative brightness uses current state, not an invented value."
                ),
                "fields": fields,
                "sequence": [
                    {
                        "variables": {
                            "lighting_targets": targets,
                            "room_names": names,
                            "fixture_names": {"main": "освещение"},
                        }
                    }
                ],
            },
            "voice_llm_all_lights_off": {
                "description": (
                    "Turn off only the exposed lights in this profile, "
                    "not every device in HA."
                ),
                "fields": {},
                "sequence": [],
            },
        }
        if targets
        else {}
    )
    return Catalog(scripts, dict.fromkeys(scripts, "light")), readable


def light_call(state, arguments):
    """Resolve supported attributes locally. No arbitrary model-generated services."""
    action = arguments["light_action"]
    if state is None or state.state in ("unknown", "unavailable"):
        raise PolicyError("Light is unavailable")
    if action == "off" or (action == "toggle" and state.state == "on"):
        return "turn_off", {}
    data = {}
    brightness = arguments.get("brightness_action", "unchanged")
    color = arguments.get("color_profile", "unchanged")
    modes = set(state.attributes.get("supported_color_modes", []))
    dimmable = bool(modes - {"onoff"})
    if brightness != "unchanged":
        if not dimmable:
            raise PolicyError("This light does not support brightness")
        if brightness == "set":
            value = arguments["brightness_percent"]
        else:
            current = (
                round((state.attributes.get("brightness") or 0) * 100 / 255)
                if state.state == "on"
                else 0
            )
            step = arguments.get("brightness_step_percent", 10)
            value = current + step if brightness == "increase" else current - step
            if (
                state.state == "off"
                and brightness == "decrease"
                and action == "adjust"
                and color == "unchanged"
            ):
                return None, {}
        data["brightness_pct"] = max(1, min(100, value))
    if color != "unchanged":
        whites = {
            "warm": 2700,
            "neutral": 4000,
            "cool": 6000,
            "reading": 4000,
            "focus": 6000,
        }
        colors = {
            "red": [255, 0, 0],
            "green": [0, 255, 0],
            "blue": [0, 0, 255],
            "relax": [255, 180, 110],
            "night": [255, 90, 20],
        }
        if color in whites and "color_temp" in modes:
            kelvin = whites[color]
            if color in ("warm", "cool") and state.attributes.get("color_temp_kelvin"):
                kelvin = state.attributes["color_temp_kelvin"] + (
                    -250 if color == "warm" else 250
                )
            data["color_temp_kelvin"] = max(
                state.attributes.get("min_color_temp_kelvin", 2000),
                min(state.attributes.get("max_color_temp_kelvin", 6500), kelvin),
            )
        elif modes & {"hs", "rgb", "rgbw", "rgbww", "xy"}:
            data["rgb_color"] = colors.get(
                color,
                {
                    "warm": [255, 185, 120],
                    "neutral": [255, 240, 220],
                    "cool": [200, 225, 255],
                    "reading": [255, 240, 220],
                    "focus": [200, 225, 255],
                }.get(color),
            )
        else:
            raise PolicyError("This light does not support the requested color")
    if (
        action == "adjust"
        and state.state == "off"
        and dimmable
        and "brightness_pct" not in data
    ):
        data["brightness_pct"] = 10
    return "turn_on", data


class BasicRuntime(HouseholdRuntime):
    def __init__(self, *args, source_catalog, **kwargs):
        super().__init__(*args, **kwargs)
        self.source_catalog = source_catalog

    def visible_entities(self, context, user):
        entities = self.source_catalog.entities
        if context and context.user_id:
            return tuple(
                e
                for e in entities
                if user and user.permissions.check_entity(e.entity_id, POLICY_READ)
            )
        return entities

    async def refresh_scope(self, context=None):
        user = (
            await self.hass.auth.async_get_user(context.user_id)
            if context and context.user_id
            else None
        )
        entities = self.visible_entities(context, user)
        self.catalog, self.config["read_entities"] = builtin_catalog(
            entities, self.hass.states, self.config.get("basic_entities", [])
        )
        self.history_router = HistoryQueries(self.config)

    async def reload_catalog(self):
        async with self.lock:
            await self.refresh_scope()

    def status(self):
        result = super().status()
        result["execution_enabled"] = self.active and self.execution_enabled
        result["profile"] = "lights_and_sensors"
        result["eligible_lights"] = len(
            {e for group in self.catalog.lights.values() for e in group}
        )
        result["eligible_sensors"] = len(self.config["read_entities"])
        return result

    async def execute(self, operations, context, preview):
        if preview or not self.active or not self.execution_enabled:
            return [], False
        user = (
            await self.hass.auth.async_get_user(context.user_id)
            if context and context.user_id
            else None
        )
        entities = self.visible_entities(context, user)
        current, _readable = builtin_catalog(
            entities,
            self.hass.states,
            self.config.get("basic_entities", []),
        )
        calls = []
        # Preflight ALL targets, permissions, capabilities and supported features.
        for operation in operations:
            name, arguments = operation["capability"], operation["arguments"]
            if name == "voice_llm_room_lights":
                targets = current.lights.get(
                    arguments["room"] + ":" + arguments.get("fixture", "main"), []
                )
            elif name == "voice_llm_all_lights_off":
                targets = sorted(
                    {e for group in current.lights.values() for e in group}
                )
                arguments = {"light_action": "off"}
            else:
                raise PolicyError("Unsupported built-in capability")
            if not targets:
                raise PolicyError("No currently exposed light target")
            planned = (
                self.catalog.lights.get(
                    arguments["room"] + ":" + arguments.get("fixture", "main"), []
                )
                if name == "voice_llm_room_lights"
                else {e for group in self.catalog.lights.values() for e in group}
            )
            if set(targets) != set(planned):
                raise PolicyError(
                    "Light exposure changed while planning; no actions started"
                )
            for entity in targets:
                if (
                    context
                    and context.user_id
                    and (
                        not user
                        or not user.permissions.check_entity(entity, POLICY_CONTROL)
                    )
                ):
                    raise PolicyError("Light control permission denied")
                service, values = light_call(self.hass.states.get(entity), arguments)
                if service and not self.hass.services.has_service("light", service):
                    raise PolicyError("Light service is unavailable")
                calls.append((name, entity, service, values))
        results = []
        for name, entity, service, values in calls:
            if not self.active or not self.execution_enabled:
                break
            try:
                if not self.source_catalog.get(entity):
                    raise PolicyError("Light is no longer exposed")
                if service:
                    await self.hass.services.async_call(
                        "light",
                        service,
                        {"entity_id": entity, **values},
                        blocking=True,
                        context=context,
                    )
                results.append(
                    {
                        "capability": name,
                        "success": True,
                        "message": "Команда освещения передана HA."
                        if service
                        else "Свет уже выключен; яркость не изменялась.",
                    }
                )
            except Exception:
                results.append(
                    {
                        "capability": name,
                        "success": False,
                        "message": (
                            "HA сообщил ошибку управления светом. "
                            "Остальные действия остановлены."
                        ),
                    }
                )
                break
        return results, bool(results)
