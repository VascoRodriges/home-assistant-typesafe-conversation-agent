"""Synthetic adapter regressions: no real catalog, cloud calls or devices."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.typesafe_conversation.extraction import media_chunks
from custom_components.typesafe_conversation.household import HouseholdRuntime
from custom_components.typesafe_conversation.household_fast import (
    FastDecisions,
    check_source_numbers,
    number_candidates,
)
from custom_components.typesafe_conversation.household_policy import (
    Budget,
    Catalog,
    PolicyError,
)
from custom_components.typesafe_conversation.household_review import TypedReview


def field(options, required=True):
    return {"required": required, "selector": {"select": {"options": options}}}


def synthetic_catalog(*, raw=False):
    scripts = {
        "voice_llm_room_lights": {
            "fields": {
                "room": field(["sample_room"]),
                "fixture": field(["main", "ambient", "all"], False),
                "light_action": field(["on", "off", "adjust"]),
                "brightness_action": field(
                    ["unchanged", "set", "increase", "decrease"], False
                ),
                "brightness_percent": {"selector": {"number": {"min": 1, "max": 100}}},
                "brightness_step_percent": {
                    "selector": {"number": {"min": 1, "max": 50}}
                },
                "color_profile": field(["unchanged", "warm", "cool", "red"], False),
            },
            "sequence": [
                {
                    "variables": {
                        "lighting_targets": {
                            "sample_room:main": ["light.sample_main"],
                            "sample_room:ambient": ["light.sample_ambient"],
                            "sample_room:all": [
                                "light.sample_main",
                                "light.sample_ambient",
                            ],
                        },
                        "room_names": {"sample_room": "комната"},
                        "fixture_names": {
                            "main": "основной свет",
                            "ambient": "контурная подсветка",
                            "all": "все источники",
                        },
                    }
                }
            ],
        },
        "voice_llm_receiver_power": {
            "fields": {"power_action": field(["on", "off"])},
            "sequence": [],
        },
        "voice_llm_receiver_volume": {
            "fields": {
                "volume_action": field(["set", "increase", "decrease"]),
                "volume_percent": {"selector": {"number": {"min": 0, "max": 100}}},
                "step_percent": {"selector": {"number": {"min": 1, "max": 20}}},
            },
            "sequence": [],
        },
        "voice_llm_play_music": {
            "fields": {
                "query": {"required": True, "selector": {"text": {}}},
                "destination": field(["receiver"]),
                "media_type": field(["auto", "track", "artist"], False),
                "volume_percent": {"selector": {"number": {"min": 0, "max": 100}}},
            },
            "sequence": [
                {
                    "variables": {
                        "players": {"receiver": "media_player.sample_receiver"},
                        "player_names": {"receiver": "ресивер"},
                    }
                }
            ],
        },
    }
    if raw:
        return scripts
    return Catalog(
        scripts, {name: "light" if "lights" in name else "media" for name in scripts}
    )


def plan(*operations, **updates):
    return {
        "operations": list(operations),
        "preserve": [],
        "home_answer": None,
        "home_evidence": [],
        "external_questions": [],
        "clarification": None,
        **updates,
    }


def operation(catalog, name, **arguments):
    return {
        "capability": name,
        "arguments": {
            key: arguments.get(key)
            for key in catalog.tools[name]["arguments"]["properties"]
        },
    }


@pytest.fixture
def runtime():
    catalog = synthetic_catalog()
    config = {
        "execution_switch": "input_boolean.sample_execution",
        "models": {
            "decision": "typesafe/jev-1.13",
            "planner": "openai/gpt-4o-mini",
            "answer": "openai/gpt-4o",
            "web": "openai/gpt-4o-mini",
        },
        "prices": {
            "openai/gpt-4o-mini": {"input_per_million": 0.15, "output_per_million": 0.6}
        },
        "budget": {"request_usd": 0.035, "daily_usd": 0.25, "monthly_usd": 3},
        "capabilities": {name: tool["group"] for name, tool in catalog.tools.items()},
        "status_sensor": "sensor.sample_budget",
        "read_entities": {"sensor.sample_temperature": "Комната — температура"},
        "extra_context_entities": {},
        "instructions": "",
        "review_threshold": 0.8,
        "fast_margin": 0.15,
    }
    hass = SimpleNamespace(
        states=SimpleNamespace(
            is_state=Mock(return_value=True),
            async_set=Mock(),
            get=Mock(return_value=None),
        ),
        services=SimpleNamespace(
            has_service=Mock(return_value=True),
            async_call=AsyncMock(
                return_value={"success": True, "message": "HA confirmed."}
            ),
        ),
    )
    with patch("custom_components.typesafe_conversation.household.Store"):
        engine = HouseholdRuntime(hass, config, "synthetic", True)
    engine.store = SimpleNamespace(async_save=AsyncMock())
    engine.budget = Budget(config["budget"])
    engine.catalog = catalog
    engine.fast_compiler = None
    engine.snapshot = Mock(return_value={})
    result = plan(operation(catalog, "voice_llm_receiver_power", power_action="off"))
    result = catalog.checked_plan(result, {"media"}, {})

    async def classify(_text, _history, request):
        request["timing"] = {"choice": "now", "confidence": 0.99}
        return "home_control", {"media"}, {}

    engine.classify = AsyncMock(side_effect=classify)
    engine.plan = AsyncMock(return_value=result)
    engine.verify = AsyncMock(return_value=True)
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "preview,entry_enabled,switch_enabled",
    [(True, True, True), (False, False, True), (False, True, False)],
)
async def test_all_execution_gates(runtime, preview, entry_enabled, switch_enabled):
    runtime.execution_enabled = entry_enabled
    runtime.hass.states.is_state.return_value = switch_enabled
    result = await runtime.process("выключи ресивер", context=None, preview=preview)
    assert not result["executed"]
    runtime.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_conversation_adapter_runs_confirmed_script(runtime):
    result = await runtime.process("выключи ресивер", context="user-context")
    assert result["speech"] == "HA confirmed."
    assert result["executed"]
    assert (
        runtime.hass.services.async_call.await_args.kwargs["context"] == "user-context"
    )


@pytest.mark.asyncio
async def test_semantic_rejection_does_not_replan_or_act(runtime):
    runtime.verify.return_value = False
    result = await runtime.process("выключи ресивер", context=None)
    assert not result["executed"]
    runtime.plan.assert_awaited_once()
    runtime.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_action_stops_chain_without_retry(runtime):
    ops = runtime.plan.return_value["operations"] * 2
    runtime.hass.services.async_call.return_value = {"success": False}
    results, executed = await runtime.execute(ops, None, False)
    assert executed and not results[0]["success"]
    runtime.hass.services.async_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_whole_chain_preflight(runtime):
    runtime.hass.services.has_service.return_value = False
    with pytest.raises(PolicyError):
        await runtime.execute(runtime.plan.return_value["operations"], None, False)
    runtime.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_keeps_panel_dialogue(runtime):
    runtime.last_text, runtime.last_reply = "question", "answer"
    await runtime.process("выключи ресивер", context=None, preview=True)
    assert (runtime.last_text, runtime.last_reply) == ("question", "answer")


@pytest.mark.asyncio
async def test_retired_runtime_cannot_bill_act_or_publish(runtime):
    runtime.active = False
    result = await runtime.process("выключи ресивер", context=None)
    assert result["error"] and not result["executed"]
    runtime.classify.assert_not_awaited()
    runtime.hass.services.async_call.assert_not_awaited()
    runtime.hass.states.async_set.assert_not_called()
    assert await runtime.execute(
        runtime.plan.return_value["operations"], None, False
    ) == ([], False)


def test_protected_light_uses_underlying_entities():
    catalog = synthetic_catalog()
    op = operation(
        catalog,
        "voice_llm_room_lights",
        room="sample_room",
        fixture="all",
        light_action="off",
    )
    with pytest.raises(PolicyError):
        catalog.checked_plan(plan(op, preserve=["sample_room:ambient"]), {"light"}, {})


@pytest.mark.parametrize("number", [50, 75])
def test_no_model_invented_volume(number):
    with pytest.raises(PolicyError):
        check_source_numbers(
            plan(
                {
                    "capability": "voice_llm_receiver_volume",
                    "arguments": {"volume_percent": number},
                }
            ),
            "сделай чуть тише на ресивере",
        )


def test_relative_volume_cannot_be_absolute():
    catalog = synthetic_catalog()
    with pytest.raises(PolicyError):
        catalog.checked_plan(
            plan(
                operation(
                    catalog,
                    "voice_llm_receiver_volume",
                    volume_action="decrease",
                    volume_percent=50,
                )
            ),
            {"media"},
            {},
        )


def test_russian_values_and_title_are_literal():
    assert 25 in {
        item["value"]
        for item in number_candidates("на двадцать пять процентов").values()
    }
    assert "We Will Rock You" in media_chunks(
        "включи песню We Will Rock You на ресивере"
    )
    assert "Спокойная ночь" in media_chunks("включи песню «Спокойная ночь» на ресивере")


def test_closed_set_relative_volume_is_one_typed_plan(runtime):
    fast = FastDecisions(
        runtime.catalog, runtime.config, "сделай чуть тише на ресивере"
    )

    def answer(name, value):
        criteria = fast.questions[name]["criteria"]
        return {
            "type": "choice",
            "choice": value,
            "confidence": 0.99,
            "probabilities": {
                key: 0.99 if key == value else 0.01 / (len(criteria) - 1)
                for key in criteria
            },
        }

    answers = {
        name: answer(name, value)
        for name, value in {
            "shape": "simple",
            "intent": "receiver_volume",
            "volume": "decrease",
            "amount": "none",
        }.items()
    }
    request = {
        "decisions": {
            "route": {
                "type": "choice",
                "choice": "home_control",
                "confidence": 0.99,
                "probabilities": {"home_control": 0.99, "home_query": 0.01},
            }
        },
        "timing": {"choice": "now", "confidence": 0.99},
    }
    result, reason = fast.compile(answers, request, {"media"}, {})
    assert reason == "closed_set_complete"
    assert result["operations"][0]["arguments"] == {"volume_action": "decrease"}


def test_budget_migration_preserves_existing_spend():
    data = {"days": {"2026-10-01": 0.2}, "months": {"2026-10": 1.2}, "blocked": False}
    budget = Budget(
        {"request_usd": 0.035, "daily_usd": 0.25, "monthly_usd": 3},
        data,
        now=lambda: datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert budget.status()["day_usd"] == 0.2
    reservation = budget.reserve(0.01, 0)
    budget.settle(reservation, 0.002)
    assert budget.status()["month_usd"] == 1.202


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"days": {"day": -1}, "months": {}, "blocked": False},
        {"days": {}, "months": {}, "blocked": "false"},
    ],
)
def test_corrupt_ledger_cannot_reset_budget(data):
    with pytest.raises(PolicyError):
        Budget({}, data)


def test_unrequested_yaml_step_default_is_omitted(runtime):
    runtime.catalog.defaults["voice_llm_receiver_volume"]["step_percent"] = 5
    result = plan(
        {
            "capability": "voice_llm_receiver_volume",
            "arguments": {"volume_action": "decrease", "step_percent": 5},
        }
    )
    check_source_numbers(result, "немного тише на ресивере", runtime.catalog)
    assert result["operations"][0]["arguments"] == {"volume_action": "decrease"}


def test_unrequested_nondefault_step_is_rejected(runtime):
    runtime.catalog.defaults["voice_llm_receiver_volume"]["step_percent"] = 5
    result = plan(
        {
            "capability": "voice_llm_receiver_volume",
            "arguments": {"volume_action": "decrease", "step_percent": 15},
        }
    )
    with pytest.raises(PolicyError):
        check_source_numbers(result, "немного тише на ресивере", runtime.catalog)


def test_unknown_cost_keeps_reservation():
    budget = Budget({"request_usd": 0.035, "daily_usd": 0.25, "monthly_usd": 3})
    assert budget.settle(budget.reserve(0.01, 0), None) == 0.01


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, True])
def test_invalid_limits_fail_closed(value):
    with pytest.raises(PolicyError):
        Budget({"request_usd": value, "daily_usd": 0.25, "monthly_usd": 3})


def test_review_selects_specific_fields_not_global_confidence(runtime):
    review = TypedReview(runtime.catalog, runtime.plan.return_value, "выключи ресивер")
    answers = {}
    for name, expected in review.expected.items():
        criteria = review.questions[name]["criteria"]
        answers[name] = {
            "type": "choice",
            "choice": expected,
            "confidence": 0.4,
            "probabilities": {
                key: 0.98 if key == expected else 0.02 / (len(criteria) - 1)
                for key in criteria
            },
        }
    approved, _verdict = review.approve(answers, 0.8, 0.15)
    assert approved
    answers["operation_0_power_action"]["choice"] = "on"
    assert not review.approve(answers, 0.8, 0.15)[0]


@pytest.mark.asyncio
async def test_compact_candidate_plan_uses_same_local_validator(runtime):
    raw = plan(
        {
            "capability": "voice_llm_receiver_power",
            "arguments_json": '{"power_action":"off"}',
        },
        operation_sources=["выключи ресивер"],
    )
    runtime.api = AsyncMock(
        return_value={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(raw)},
                }
            ]
        }
    )
    request = {
        "model_overrides": {"planner": "google/gemini-3.1-flash-lite"},
        "timing": {"choice": "now"},
    }
    result = await HouseholdRuntime.plan(
        runtime, "выключи ресивер", [], {"media"}, {}, request
    )
    assert result["operations"] == [
        {"capability": "voice_llm_receiver_power", "arguments": {"power_action": "off"}}
    ]
    assert request["operation_sources"] == ["выключи ресивер"]


def test_overspend_blocks_execution_budget():
    budget = Budget({"request_usd": 0.035, "daily_usd": 0.25, "monthly_usd": 3})
    with pytest.raises(PolicyError):
        budget.settle(budget.reserve(0.01, 0), 0.02)
    with pytest.raises(PolicyError):
        budget.reserve(0.001, 0)
