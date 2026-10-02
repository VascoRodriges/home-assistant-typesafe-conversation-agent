"""Portable defaults. No household IDs, room mappings, scripts or credentials."""

BASIC_CATALOG = "builtin:lights_and_sensors"
BASIC_CAPABILITIES = {
    "voice_llm_room_lights": "light",
    "voice_llm_all_lights_off": "light",
}


def basic_preset():
    """Fresh independent settings for the no-YAML onboarding profile."""
    return {
        "builtin_preset": True,
        "script_catalog": BASIC_CATALOG,
        # Built-in mode uses the explicit entry permission, not a missing helper.
        "execution_switch": "input_boolean.typesafe_basic_permission",
        "models": {
            "decision": "typesafe/jev-1.13",
            "planner": "openai/gpt-4o-mini",
            "answer": "openai/gpt-4o-mini",
            "web": "openai/gpt-4o-mini",
        },
        "prices": {
            "typesafe/jev-1.13": {"input_per_million": 0.042, "output_per_million": 0},
            "openai/gpt-4o-mini": {
                "input_per_million": 0.15,
                "output_per_million": 0.6,
            },
        },
        "budget": {"request_usd": 0.035, "daily_usd": 0.25, "monthly_usd": 3},
        "capabilities": dict(BASIC_CAPABILITIES),
        "read_entities": {},
        "basic_entities": [],
        "history": {"enabled": True, "max_days": 7, "max_age_minutes": 120},
        "web_enabled": False,
        "instructions": (
            "Use only exposed, eligible lights and numeric sensors in this catalog. "
            "Room lighting means the selected lights in that area. "
            "Individual targets are available by name and aliases. "
            "Never infer a media player, vacuum, switch, lock or custom script. "
            "For unsupported tasks ask for clarification. "
            "No delayed device actions are available in this profile."
        ),
    }
