"""Portable defaults and local execution: synthetic objects, no cloud or devices."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Context, State

from custom_components.typesafe_conversation.basic import (
    BasicRuntime,
    builtin_catalog,
    light_call,
    numeric_sensor,
)
from custom_components.typesafe_conversation.household_fast import FastDecisions
from custom_components.typesafe_conversation.household_policy import PolicyError
from custom_components.typesafe_conversation.presets import basic_preset
from custom_components.typesafe_conversation.settings import (
    YAML_SCHEMA,
    normalize_settings,
)


def entity(entity_id, area="sample", name="Sample fixture"):
    return SimpleNamespace(
        entity_id=entity_id,
        domain=entity_id.split(".")[0],
        area_id=area,
        area_name="Sample room" if area else None,
        name=name,
        aliases=("sample alias",),
    )


def state(entity_id="light.sample", power="on", **attributes):
    return State(
        entity_id,
        power,
        {"supported_color_modes": ["color_temp"], "brightness": 51, **attributes},
    )


def test_preset_is_independent_portable_and_budgeted():
    first, second = basic_preset(), basic_preset()
    first["models"]["planner"] = "changed"
    assert second["models"]["planner"] == "openai/gpt-4o-mini"
    assert second["read_entities"] == {} and second["basic_entities"] == []
    assert not second["web_enabled"]
    assert set(second["models"].values()) <= second["prices"].keys()
    result = normalize_settings(
        YAML_SCHEMA(
            {"provider": "openrouter", "api_key": "synthetic", "household": second}
        )
    )
    assert result["household"]["budget"]["monthly_usd"] == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("basic_entities", ["media_player.sample"]),
        ("extra_context_entities", {"media": ["media_player.sample"]}),
        ("capabilities", {"arbitrary_script": "light"}),
        ("script_catalog", "arbitrary.yaml"),
    ],
)
def test_basic_cannot_expand_domains_or_run_external_scripts(field, value):
    config = basic_preset()
    config[field] = value
    with pytest.raises(ValueError):
        normalize_settings(
            YAML_SCHEMA({"provider": "openrouter", "api_key": "k", "household": config})
        )


def test_catalog_groups_only_supplied_exposed_lights_and_numeric_sensors():
    entities = [
        entity("light.sample"),
        entity("light.second"),
        entity("sensor.temperature"),
        entity("sensor.secret"),
        entity("switch.sample"),
        entity("media_player.sample"),
    ]
    states = {
        "sensor.temperature": State(
            "sensor.temperature", "22.5", {"unit_of_measurement": "°C"}
        ),
        "sensor.secret": State("sensor.secret", "private text"),
    }
    catalog, readable = builtin_catalog(entities, states)
    assert catalog.lights["area_sample:main"] == ["light.sample", "light.second"]
    assert readable == {
        "sensor.temperature": "Sample room · Sample fixture · sample alias"
    }
    assert set(catalog.tools) == {"voice_llm_room_lights", "voice_llm_all_lights_off"}
    restricted, readable = builtin_catalog(entities, states, ["light.sample"])
    assert restricted.lights["area_sample:main"] == ["light.sample"]
    assert readable == {}
    empty, _ = builtin_catalog(entities, states, ["light.hidden"])
    assert empty.tools == {}


def test_sensor_validation_and_catalog_size_limit():
    assert numeric_sensor(
        State("sensor.sample", "unavailable", {"unit_of_measurement": "W"})
    )
    assert not numeric_sensor(
        State("sensor.sample", "nan", {"unit_of_measurement": "W"})
    )
    assert not numeric_sensor(State("sensor.sample", "42"))
    with pytest.raises(PolicyError, match="100"):
        builtin_catalog([entity(f"light.sample_{i}") for i in range(101)], {})


@pytest.mark.parametrize(
    "power,change,expected",
    [
        ("on", "increase", 30),
        ("on", "decrease", 10),
        ("off", "increase", 10),
    ],
)
def test_relative_brightness_uses_actual_state(power, change, expected):
    service, data = light_call(
        state(power=power), {"light_action": "adjust", "brightness_action": change}
    )
    assert service == "turn_on" and data["brightness_pct"] == expected


def test_dimming_off_light_does_not_turn_it_on():
    assert light_call(
        state(power="off"), {"light_action": "adjust", "brightness_action": "decrease"}
    ) == (None, {})


def test_color_clamping_relative_kelvin_and_rgb():
    assert light_call(
        state(color_temp_kelvin=3100),
        {"light_action": "adjust", "color_profile": "warm"},
    )[1] == {"color_temp_kelvin": 2850}
    assert light_call(
        state(color_temp_kelvin=6400, max_color_temp_kelvin=6500),
        {"light_action": "adjust", "color_profile": "cool"},
    )[1] == {"color_temp_kelvin": 6500}
    assert light_call(
        state(supported_color_modes=["rgb"]),
        {"light_action": "adjust", "color_profile": "red"},
    )[1] == {"rgb_color": [255, 0, 0]}


@pytest.mark.parametrize(
    "args",
    [
        {"light_action": "adjust", "brightness_action": "increase"},
        {"light_action": "adjust", "color_profile": "warm"},
    ],
)
def test_unsupported_attributes_are_not_silently_ignored(args):
    with pytest.raises(PolicyError):
        light_call(state(supported_color_modes=["onoff"]), args)


@pytest.fixture
def runtime():
    r = object.__new__(BasicRuntime)
    r.active = r.execution_enabled = True
    r.config = basic_preset()
    r.source_catalog = SimpleNamespace(
        entities=[entity("light.sample"), entity("light.second")],
        get=Mock(return_value=True),
    )
    r.hass = SimpleNamespace(
        states={"light.sample": state(), "light.second": state("light.second")},
        auth=SimpleNamespace(
            async_get_user=AsyncMock(
                return_value=SimpleNamespace(
                    permissions=SimpleNamespace(check_entity=Mock(return_value=True))
                )
            )
        ),
        services=SimpleNamespace(
            has_service=Mock(return_value=True), async_call=AsyncMock()
        ),
    )
    r.catalog, r.config["read_entities"] = builtin_catalog(
        r.source_catalog.entities, r.hass.states
    )
    return r


def operations():
    return [
        {
            "capability": "voice_llm_room_lights",
            "arguments": {
                "room": "area_sample",
                "light_action": "adjust",
                "brightness_action": "increase",
            },
        }
    ]


async def test_basic_preview_and_disabled_permission_never_call_services(runtime):
    assert await runtime.execute(operations(), None, True) == ([], False)
    runtime.execution_enabled = False
    assert await runtime.execute(operations(), None, False) == ([], False)
    runtime.hass.services.async_call.assert_not_called()


async def test_only_standard_light_services_are_used(runtime):
    results, executed = await runtime.execute(operations(), None, False)
    assert executed and len(results) == 2 and all(r["success"] for r in results)
    for call in runtime.hass.services.async_call.call_args_list:
        assert call.args[0:2] == ("light", "turn_on")
        assert call.args[2]["brightness_pct"] == 30


async def test_preflight_second_light_feature_failure_makes_no_changes(runtime):
    runtime.hass.states["light.second"] = state(
        "light.second", supported_color_modes=["onoff"]
    )
    with pytest.raises(PolicyError):
        await runtime.execute(operations(), None, False)
    runtime.hass.services.async_call.assert_not_called()


async def test_removed_exposure_fails_before_first_action(runtime):
    runtime.source_catalog.entities.pop()
    with pytest.raises(PolicyError, match="exposure changed"):
        await runtime.execute(operations(), None, False)
    runtime.hass.services.async_call.assert_not_called()


async def test_permission_failure_preflights_entire_room(runtime):
    user = await runtime.hass.auth.async_get_user("synthetic-user")
    user.permissions.check_entity.side_effect = lambda entity_id, policy: (
        policy == "read" or entity_id != "light.second"
    )
    with pytest.raises(PolicyError, match="permission"):
        await runtime.execute(operations(), Context(user_id="synthetic-user"), False)
    runtime.hass.services.async_call.assert_not_called()


async def test_service_failure_stops_without_retries(runtime):
    runtime.hass.services.async_call.side_effect = RuntimeError("synthetic failure")
    results, _executed = await runtime.execute(operations(), None, False)
    assert len(results) == 1 and not results[0]["success"]
    assert runtime.hass.services.async_call.call_count == 1


async def test_user_read_permissions_filter_catalog_and_snapshot(runtime):
    user = await runtime.hass.auth.async_get_user("synthetic-user")
    user.permissions.check_entity.side_effect = lambda entity_id, _policy: (
        entity_id == "light.sample"
    )
    await runtime.refresh_scope(Context(user_id="synthetic-user"))
    assert runtime.catalog.lights["area_sample:main"] == ["light.sample"]


def test_empty_basic_scope_still_produces_valid_closed_set_questions():
    catalog, readable = builtin_catalog([], {})
    config = {**basic_preset(), "read_entities": readable}
    questions = FastDecisions(catalog, config, "hello").questions
    assert all(len(q["criteria"]) >= 2 for q in questions.values())
    assert "player" not in questions and "sensor" not in questions
