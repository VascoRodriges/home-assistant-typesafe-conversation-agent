"""Synthetic network capabilities; no household identifiers or router writes."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.typesafe_conversation.household import HouseholdRuntime
from custom_components.typesafe_conversation.household_fast import FastDecisions
from custom_components.typesafe_conversation.household_policy import (
    Catalog,
    PlanError,
    PolicyError,
)
from custom_components.typesafe_conversation.household_review import TypedReview


def catalog():
    field = {
        "required": True,
        "description": "ONE allowed client",
        "selector": {"select": {"options": ["sample_android", "sample_iphone"]}},
    }
    scripts = {
        "voice_llm_network_access": {
            "description": "Change one allowed client's WAN access.",
            "fields": {
                "client": field,
                "access_action": {
                    "required": True,
                    "selector": {"select": {"options": ["block", "allow"]}},
                },
            },
            "sequence": [
                {
                    "variables": {
                        "client_names": {
                            "sample_android": "Sample Android phone",
                            "sample_iphone": "Sample iPhone",
                        }
                    }
                }
            ],
        },
        "voice_llm_network_status": {
            "description": "Read one client's WAN status; NO changes.",
            "fields": {"client": field},
            "sequence": [
                {
                    "variables": {
                        "client_names": {
                            "sample_android": "Sample Android phone",
                            "sample_iphone": "Sample iPhone",
                        }
                    }
                }
            ],
        },
    }
    return Catalog(scripts, dict.fromkeys(scripts, "network"))


def answers(fast, picks):
    result = {}
    for name, chosen in picks.items():
        criteria = fast.questions[name]["criteria"]
        result[name] = {
            "type": "choice",
            "choice": chosen,
            "confidence": 0.99,
            "probabilities": {
                key: 0.99 if key == chosen else 0.01 / (len(criteria) - 1)
                for key in criteria
            },
        }
    return result


def request(route="home_control", timing="now"):
    return {
        "decisions": {
            "route": {
                "type": "choice",
                "choice": route,
                "confidence": 0.99,
                "probabilities": {route: 0.99, "unclear": 0.01},
            }
        },
        "timing": {"choice": timing, "confidence": 0.99},
    }


@pytest.mark.parametrize("action", ["block", "allow"])
def test_one_jev_network_control_is_closed_set(action):
    cat = catalog()
    fast = FastDecisions(cat, {"read_entities": {}}, "operate sample phone")
    data = answers(
        fast,
        {
            "shape": "simple",
            "intent": "network_access",
            "network_access_client": "sample_android",
            "network_action": action,
        },
    )
    plan, reason = fast.compile(data, request(), {"network"}, {})
    assert reason == "closed_set_complete"
    assert plan["operations"] == [
        {
            "capability": "voice_llm_network_access",
            "arguments": {"client": "sample_android", "access_action": action},
        }
    ]
    assert "mac" not in plan["operations"][0]["arguments"]


def test_status_question_selects_only_readonly_capability():
    fast = FastDecisions(catalog(), {"read_entities": {}}, "is sample phone blocked?")
    data = answers(
        fast,
        {
            "shape": "simple",
            "intent": "network_status",
            "network_status_client": "sample_iphone",
        },
    )
    plan, reason = fast.compile(
        data, request("home_query", "no_control"), {"network"}, {}
    )
    assert reason == "closed_set_complete"
    assert plan["operations"][0]["capability"] == "voice_llm_network_status"
    HouseholdRuntime.check_timing(plan, request("home_query", "no_control"))


@pytest.mark.parametrize(
    "route,timing",
    [
        ("home_query", "no_control"),
        ("general", "no_control"),
        ("home_control", "deferred"),
    ],
)
def test_question_hypothetical_and_delay_cannot_be_fast_control(route, timing):
    fast = FastDecisions(catalog(), {"read_entities": {}}, "sample text")
    data = answers(
        fast,
        {
            "shape": "simple",
            "intent": "network_access",
            "network_access_client": "sample_android",
            "network_action": "block",
        },
    )
    plan, _ = fast.compile(data, request(route, timing), {"network"}, {})
    assert plan is None


def test_complex_or_unknown_client_does_not_compile():
    fast = FastDecisions(catalog(), {"read_entities": {}}, "two phones")
    for shape, client in (("complex", "sample_android"), ("simple", "unknown")):
        data = answers(
            fast,
            {
                "shape": shape,
                "intent": "network_access",
                "network_access_client": client,
                "network_action": "block",
            },
        )
        assert fast.compile(data, request(), {"network"}, {})[0] is None


def test_no_control_permission_cannot_hide_mutation_behind_read_status():
    operations = [
        {
            "capability": "voice_llm_network_status",
            "arguments": {"client": "sample_android"},
        },
        {
            "capability": "voice_llm_network_access",
            "arguments": {"client": "sample_android", "access_action": "allow"},
        },
    ]
    with pytest.raises(PlanError):
        HouseholdRuntime.check_timing(
            {"operations": operations}, request("home_query", "no_control")
        )


@pytest.mark.asyncio
async def test_question_planner_schema_contains_only_readonly_network_action():
    result = {
        "operations": [
            {
                "capability": "voice_llm_network_status",
                "arguments": {"client": "sample_android"},
            }
        ],
        "operation_sources": ["is my phone blocked?"],
        "preserve": [],
        "home_answer": None,
        "home_evidence": [],
        "external_questions": [],
        "clarification": None,
    }
    api = AsyncMock(
        return_value={
            "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps(result)}}
            ]
        }
    )
    runtime = SimpleNamespace(
        catalog=catalog(),
        config={"models": {"planner": "synthetic"}, "instructions": ""},
        api=api,
        chat_content=HouseholdRuntime.chat_content,
    )
    plan = await HouseholdRuntime.plan(
        runtime,
        "is my phone blocked?",
        [],
        {"network"},
        {},
        request("home_query", "no_control"),
    )
    assert plan["operations"][0]["capability"] == "voice_llm_network_status"
    payload = api.call_args.args[1]
    serialized = json.dumps(payload["response_format"])
    assert "voice_llm_network_status" in serialized
    assert "voice_llm_network_access" not in serialized


def test_review_offers_human_client_names_not_raw_ids():
    cat = catalog()
    plan = {
        "operations": [
            {
                "capability": "voice_llm_network_access",
                "arguments": {"client": "sample_android", "access_action": "block"},
            }
        ],
        "preserve": [],
        "home_answer": None,
        "home_evidence": [],
        "external_questions": [],
        "clarification": None,
    }
    review = TypedReview(cat, plan, "block sample Android")
    assert (
        review.questions["operation_0_client"]["criteria"]["sample_android"]
        == "Sample Android phone"
    )


def test_raw_mac_and_outside_client_are_not_legal_arguments():
    cat = catalog()
    for args in (
        {"client": "outside", "access_action": "block"},
        {
            "client": "sample_android",
            "access_action": "block",
            "mac": "02:00:00:00:00:00",
        },
    ):
        plan = {
            "operations": [
                {"capability": "voice_llm_network_access", "arguments": args}
            ],
            "preserve": [],
            "home_answer": None,
            "home_evidence": [],
            "external_questions": [],
            "clarification": None,
        }
        with pytest.raises(PolicyError):
            cat.checked_plan(plan, {"network"}, {})


@pytest.mark.asyncio
async def test_execution_off_allows_only_fixed_readonly_status():
    service = AsyncMock(return_value={"success": True, "message": "Readonly status"})
    runtime = SimpleNamespace(
        active=True,
        execution_enabled=False,
        config={
            "execution_switch": "input_boolean.sample",
            "capabilities": {
                "voice_llm_network_status": "network",
                "voice_llm_network_access": "network",
            },
        },
        hass=SimpleNamespace(
            states=SimpleNamespace(is_state=Mock(return_value=False)),
            services=SimpleNamespace(
                has_service=Mock(return_value=True), async_call=service
            ),
        ),
    )
    status = {
        "capability": "voice_llm_network_status",
        "arguments": {"client": "sample_android"},
    }
    access = {
        "capability": "voice_llm_network_access",
        "arguments": {"client": "sample_android", "access_action": "block"},
    }
    result, invoked = await HouseholdRuntime.execute(runtime, [status], None, False)
    assert invoked and result[0]["success"]
    service.assert_awaited_once()
    service.reset_mock()
    assert await HouseholdRuntime.execute(runtime, [status, access], None, False) == (
        [],
        False,
    )
    assert await HouseholdRuntime.execute(runtime, [status], None, True) == ([], False)
    service.assert_not_awaited()
