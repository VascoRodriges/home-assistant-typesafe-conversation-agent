"""Pure, fail-closed policy and script catalog. No HA or network imports."""

import copy
import json
import math
from datetime import UTC, datetime


class PolicyError(ValueError):
    """An invalid plan must never be executed."""


class PlanError(PolicyError):
    """A locally invalid plan can receive one bounded semantic correction."""


def object_schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def validate(value, schema, path="plan"):  # noqa: C901 - exact recursive schema subset
    """Validate the exact subset we generate, including local semantic bounds."""
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate(value, option, path)
                return
            except PolicyError:
                pass
        raise PolicyError(f"{path}: unsupported value")
    kind = schema["type"]
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "integer": int,
        "number": (float, int),
        "null": type(None),
    }
    if not isinstance(value, types[kind]) or (
        kind in ("integer", "number") and isinstance(value, bool)
    ):
        raise PolicyError(f"{path}: incorrect type")
    if "enum" in schema and value not in schema["enum"]:
        raise PolicyError(f"{path}: outside allowed enum")
    if kind == "object":
        if set(value) != set(schema["properties"]):
            raise PolicyError(f"{path}: missing or unknown fields")
        for key, item in value.items():
            validate(item, schema["properties"][key], f"{path}.{key}")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 32):
            raise PolicyError(f"{path}: invalid array length")
        for item in value:
            validate(item, schema["items"], path)
        if len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise PolicyError(f"{path}: duplicate items")
    elif kind == "string":
        if len(value) > schema.get("maxLength", 1200):
            raise PolicyError(f"{path}: text too long")
    elif kind in ("integer", "number"):
        if not math.isfinite(value) or not schema.get(
            "minimum", -1e12
        ) <= value <= schema.get("maximum", 1e12):
            raise PolicyError(f"{path}: invalid number")


def selector_schema(field):
    selector = field.get("selector", {})
    if "select" in selector:
        options = selector["select"]["options"]
        enum = [o["value"] if isinstance(o, dict) else o for o in options]
        schema = {"type": "string", "enum": enum}
        if selector["select"].get("multiple"):
            schema = {
                "type": "array",
                "items": schema,
                "minItems": 1,
                "maxItems": len(enum),
            }
    elif "number" in selector:
        number = selector["number"]
        schema = {
            "type": "integer" if number.get("step", 1) == 1 else "number",
            "minimum": number["min"],
            "maximum": number["max"],
        }
    elif "text" in selector:
        schema = {"type": "string", "maxLength": 500}
    else:
        raise PolicyError("Unsupported YAML selector")
    schema["description"] = field.get("description", field.get("name", ""))
    return schema if field.get("required", False) else nullable(schema)


class Catalog:
    def __init__(self, scripts, allowlist):
        self.tools = {}
        self.lights = {}
        self.players = {}
        self.labels = {}
        self.defaults = {}
        for name, group in allowlist.items():
            script = scripts[name]
            self.defaults[name] = {
                key: field["default"]
                for key, field in script.get("fields", {}).items()
                if not field.get("required") and "default" in field
            }
            self.tools[name] = {
                "group": group,
                "description": script.get("description", ""),
                "arguments": object_schema(
                    {k: selector_schema(v) for k, v in script.get("fields", {}).items()}
                ),
            }
            for step in script["sequence"]:
                variables = step.get("variables", {})
                self.labels[name] = {
                    **self.labels.get(name, {}),
                    **{
                        k: v
                        for k, v in variables.items()
                        if k.endswith("_names") and isinstance(v, dict)
                    },
                }
                self.lights.update(variables.get("lighting_targets", {}))
                self.players.update(variables.get("players", {}))

    def selected(self, groups, allowed=None):
        return {
            k: v
            for k, v in self.tools.items()
            if v["group"] in groups and (allowed is None or k in allowed)
        }

    def schema(self, groups, allowed=None):
        tools = self.selected(groups, allowed)
        operations = [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["capability", "arguments"],
                "properties": {
                    "capability": {"type": "string", "enum": [name]},
                    "arguments": tool["arguments"],
                },
            }
            for name, tool in tools.items()
        ]
        # At least one option is needed even for a read-only sensor question;
        # execution permissions are checked independently against selected groups.
        if not operations:
            operations = [{"type": "null"}]
        return object_schema(
            {
                "operations": {
                    "type": "array",
                    "items": {"anyOf": operations},
                    "maxItems": 8,
                },
                "preserve": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(self.lights)}
                    if self.lights
                    else {"type": "string"},
                    "maxItems": 12 if self.lights else 0,
                    "description": "Fixtures the user explicitly leaves unchanged. Resolve aliases from the catalog. Never modify their underlying entities.",
                },
                "home_answer": nullable(
                    {
                        "type": "string",
                        "maxLength": 1000,
                        "description": "Only answer an actual home state QUESTION; otherwise null. Never report actions here.",
                    }
                ),
                "home_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 12,
                    "description": "Entity IDs supporting home_answer. Empty list when home_answer is null.",
                },
                "external_questions": {
                    "type": "array",
                    "maxItems": 2,
                    "items": object_schema(
                        {
                            "kind": {"type": "string", "enum": ["general", "web"]},
                            "question": {"type": "string", "maxLength": 500},
                        }
                    ),
                },
                "clarification": nullable({"type": "string", "maxLength": 500}),
            }
        )

    def checked_plan(self, plan, groups, snapshot, allowed=None):  # noqa: C901 - centralized policy boundary
        validate(plan, self.schema(groups, allowed))
        if plan["clarification"]:
            if plan["operations"]:
                raise PolicyError("A clarification may not include operations")
            return plan
        if any(e not in snapshot for e in plan["home_evidence"]):
            raise PolicyError("Read-only answer references an unavailable entity")
        if plan["home_answer"] and not plan["home_evidence"]:
            raise PolicyError("Home answer requires state evidence")
        protected = {e for key in plan["preserve"] for e in self.lights[key]}
        result = copy.deepcopy(plan)
        for op in result["operations"]:
            name, args = op["capability"], op["arguments"]
            if name not in self.selected(groups, allowed):
                raise PolicyError("Capability is not in selected groups")
            op["arguments"] = args = {k: v for k, v in args.items() if v is not None}
            if name == "voice_llm_room_lights":
                key = args["room"] + ":" + args.get("fixture", "main")
                targets = set(self.lights.get(key, []))
                if not targets:
                    raise PolicyError("This fixture does not exist in that room")
                if targets & protected:
                    raise PolicyError("Operation would change a protected light")
                mode = args.get("brightness_action", "unchanged")
                if mode == "set" and "brightness_percent" not in args:
                    raise PolicyError("Absolute brightness requires a value")
                if mode != "set" and "brightness_percent" in args:
                    raise PolicyError(
                        "Relative brightness cannot set an absolute value"
                    )
                if args["light_action"] == "off" and (
                    mode != "unchanged"
                    or args.get("color_profile", "unchanged") != "unchanged"
                ):
                    raise PolicyError(
                        "Switching off may not also change brightness or color"
                    )
            elif name == "voice_llm_all_lights_off" and protected:
                raise PolicyError("Global off conflicts with lights to preserve")
            elif name == "voice_llm_receiver_volume":
                if args["volume_action"] == "set" and "volume_percent" not in args:
                    raise PolicyError("Absolute volume requires a value")
                if args["volume_action"] != "set" and "volume_percent" in args:
                    raise PolicyError("Relative volume cannot set an absolute value")
            elif name in ("voice_llm_play_music", "voice_llm_music_control"):
                if args["destination"] not in self.players:
                    raise PolicyError("Unknown player destination")
        return result

    def describe(self, operation):
        """Render deterministic human semantics; no interpretation of user text."""
        name, args = operation["capability"], operation["arguments"]
        labels = self.labels.get(name, {})
        rooms = labels.get("room_names", {})
        if name == "voice_llm_room_lights":
            fixture = labels.get("fixture_names", {}).get(
                args.get("fixture", "main"), args.get("fixture", "main")
            )
            action = {
                "on": "включить",
                "off": "выключить",
                "toggle": "переключить",
                "adjust": "изменить настройки",
            }[args["light_action"]]
            text = f"{action}: {fixture} {rooms.get(args['room'], args['room'])}"
            brightness = args.get("brightness_action", "unchanged")
            if brightness in ("increase", "decrease"):
                verb = "увеличить" if brightness == "increase" else "уменьшить"
                text += f"; {verb} яркость относительно текущей на {args.get('brightness_step_percent', 10)} процентных пунктов (безопасный шаг)"
            elif brightness == "set":
                text += f"; установить яркость {args['brightness_percent']}%"
            profile = args.get("color_profile", "unchanged")
            if profile != "unchanged":
                color = {
                    "warm": "тёплый свет",
                    "neutral": "нейтральный свет",
                    "cool": "холодный свет",
                    "red": "красный",
                    "green": "зелёный",
                    "blue": "синий",
                    "relax": "спокойный оттенок",
                    "night": "ночной оттенок",
                    "reading": "для чтения",
                    "focus": "для концентрации",
                }[profile]
                text += f"; цветовой профиль: {color}"
            return text
        if name == "voice_llm_receiver_volume":
            action = args["volume_action"]
            if action == "set":
                return (
                    f"Установить громкость ресивера точно на {args['volume_percent']}%"
                )
            return f"{'Увеличить' if action == 'increase' else 'Уменьшить'} громкость ресивера относительно текущей на {args.get('step_percent', 5)} процентных пунктов (безопасный шаг)"
        if name == "voice_llm_receiver_power":
            return (
                "Включить ресивер"
                if args["power_action"] == "on"
                else "Выключить ресивер и остановить его музыку"
            )
        if name == "voice_llm_all_lights_off":
            return "Выключить освещение в пределах разрешённых целей каталога"
        if name == "voice_llm_play_music":
            return (
                f"Включить музыку: запрос «{args['query']}», на {args['destination']} через Music Assistant"
                + (
                    f"; громкость {args['volume_percent']}%"
                    if "volume_percent" in args
                    else ""
                )
            )
        if name == "voice_llm_music_control":
            verb = {
                "pause": "пауза",
                "play": "продолжить",
                "stop": "стоп",
                "next": "следующий трек",
                "previous": "предыдущий трек",
            }[args["playback_action"]]
            return f"Музыка на {args['destination']}: {verb}"
        if name == "voice_llm_vacuum_rooms":
            sequence = ", затем ".join(rooms.get(r, r) for r in args["rooms"])
            return f"Уборка пылесосом: {sequence}; запуск через {args.get('delay_minutes', 0)} минут; {args.get('repeats', 1)} проход"
        return f"{self.tools[name]['description']} Параметры: {json.dumps(args, ensure_ascii=False)}"


def probability(answer):
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        raise PolicyError("Invalid decision response")
    value = answer.get("noul")
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise PolicyError("Invalid decision probability")
    return value


class Budget:
    """UTC billing windows; reservation is persisted BEFORE a network call."""

    def __init__(self, limits, data=None, now=None):
        self.limits = limits
        self.data = (
            data if data is not None else {"days": {}, "months": {}, "blocked": False}
        )
        if not isinstance(self.data, dict) or not isinstance(
            self.data.get("blocked"), bool
        ):
            raise PolicyError("Invalid persisted budget ledger")
        for window in ("days", "months"):
            if not isinstance(self.data.get(window), dict) or any(
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for key, value in self.data[window].items()
            ):
                raise PolicyError("Invalid persisted budget ledger")
        if any(
            isinstance(limits.get(key), bool)
            or not isinstance(limits.get(key), (int, float))
            or not math.isfinite(limits[key])
            or limits[key] <= 0
            for key in ("request_usd", "daily_usd", "monthly_usd")
        ):
            raise PolicyError("Invalid budget limits")
        self.now = now or (lambda: datetime.now(UTC))

    def windows(self):
        now = self.now()
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    def reserve(self, amount, request_spend):
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount < 0
            or self.data.get("blocked")
        ):
            raise PolicyError("Budget is blocked")
        day, month = self.windows()
        if request_spend + amount > self.limits["request_usd"]:
            raise PolicyError("Request budget limit reached")
        if (
            self.data["days"].get(day, 0) + amount > self.limits["daily_usd"]
            or self.data["months"].get(month, 0) + amount > self.limits["monthly_usd"]
        ):
            raise PolicyError("Daily or monthly budget limit reached")
        self.data["days"][day] = self.data["days"].get(day, 0) + amount
        self.data["months"][month] = self.data["months"].get(month, 0) + amount
        return (day, month, amount)

    def settle(self, reservation, cost):
        day, month, amount = reservation
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (float, int))
            or not math.isfinite(cost)
            or cost < 0
        ):
            return amount  # Timeout/missing usage: keep the conservative reservation.
        for window, key in (("days", day), ("months", month)):
            self.data[window][key] = max(
                0, self.data[window].get(key, 0) + cost - amount
            )
        if cost > amount + 1e-9:
            self.data["blocked"] = (
                True  # Unexpected bill: no later requests or actuation.
            )
            raise PolicyError("Reported cost exceeds reserved ceiling")
        return cost

    def status(self):
        day, month = self.windows()
        return {
            "day_usd": round(self.data["days"].get(day, 0), 6),
            "month_usd": round(self.data["months"].get(month, 0), 6),
            "blocked": self.data.get("blocked", False),
            **self.limits,
        }
