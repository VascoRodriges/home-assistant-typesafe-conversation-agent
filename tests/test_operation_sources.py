"""Exact clause provenance and independent compound review; synthetic homes only."""

import json
from unittest.mock import AsyncMock

import pytest

from custom_components.typesafe_conversation.household import (
    HouseholdRuntime,
    operation_sources,
)
from custom_components.typesafe_conversation.household_policy import Catalog, PlanError
from custom_components.typesafe_conversation.household_review import TypedReview


def fixture_catalog():
    def select(options, required=True):
        return {"required": required, "selector": {"select": {"options": options}}}

    return Catalog(
        {
            "voice_llm_room_lights": {
                "fields": {
                    "room": select(["office"]),
                    "fixture": select(["main", "ceiling_rim", "desk", "desk_rim"]),
                    "light_action": select(["on", "off", "adjust"]),
                    "color_profile": select(["unchanged", "blue"], False),
                },
                "sequence": [
                    {
                        "variables": {
                            "lighting_targets": {
                                "office:main": ["light.ceiling"],
                                "office:ceiling_rim": ["light.ceiling_rim"],
                                "office:desk": ["light.desk"],
                                "office:desk_rim": ["light.desk_rim"],
                            },
                            "room_names": {"office": "Office"},
                            "fixture_names": {
                                "main": "Main ceiling light",
                                "ceiling_rim": "Ceiling rim light",
                                "desk": "Desk lamp",
                                "desk_rim": "Desk lamp rim light",
                            },
                        }
                    }
                ],
            }
        },
        {"voice_llm_room_lights": "light"},
    )


def compound_plan(first="main", second="ceiling_rim"):
    return {
        "operations": [
            {
                "capability": "voice_llm_room_lights",
                "arguments": {
                    "room": "office",
                    "fixture": target,
                    "light_action": "on",
                    "color_profile": color,
                },
            }
            for target, color in [(first, "unchanged"), (second, "blue")]
        ],
        "preserve": [],
        "home_answer": None,
        "home_evidence": [],
        "external_questions": [],
        "clarification": None,
    }


def answers_for(review):
    return {
        name: {
            "type": "choice",
            "choice": expected,
            "confidence": 0.99,
            "probabilities": {
                key: 0.99 if key == expected else 0.01 / (len(question["criteria"]) - 1)
                for key in question["criteria"]
            },
        }
        for name, expected in review.expected.items()
        for question in [review.questions[name]]
    }


@pytest.mark.parametrize(
    "sources,count",
    [
        ([], 1),
        (["invented action"], 1),
        (["  "], 1),
        ([None], 1),
        ("turn on", 1),
        (["turn on", "turn on"], 1),
    ],
)
def test_missing_invented_or_mismatched_sources_fail_closed(sources, count):
    with pytest.raises(PlanError):
        operation_sources("turn on the office light", sources, count)


def test_sources_must_be_from_current_request_not_history():
    with pytest.raises(PlanError):
        operation_sources("make it blue", ["turn on the desk lamp"], 1)
    assert operation_sources("make it blue", ["make it blue"], 1) == ["make it blue"]
    assert operation_sources("what is the temperature?", [], 0) == []


def test_two_clauses_have_separate_bindings_and_target_checks():
    text = "Turn on the office light and make the rim light blue"
    sources = ["Turn on the office light", "make the rim light blue"]
    review = TypedReview(fixture_catalog(), compound_plan(), text, sources=sources)
    assert review.sources == dict(
        zip(["operation_0", "operation_1"], sources, strict=True)
    )
    assert review.expected["operation_0_target"] == "office:main"
    assert review.expected["operation_1_target"] == "office:ceiling_rim"
    assert review.expected["operation_1_source"] == "aligned"
    assert "preceding clauses" in review.questions["operation_1_target"]["instructions"]
    assert (
        '"make the rim light blue"'
        in review.questions["operation_1_target"]["instructions"]
    )
    assert (
        '"Turn on the office light"'
        in review.questions["operation_0_target"]["instructions"]
    )
    assert (
        '"Turn on the office light"'
        not in review.questions["operation_1_target"]["instructions"]
    )
    answers = answers_for(review)
    assert review.approve(answers, 0.8, 0.15)[0]
    answers["operation_1_source"] = {
        "type": "choice",
        "choice": "mismatched",
        "confidence": 0.99,
        "probabilities": {"aligned": 0.005, "mismatched": 0.99, "not_requested": 0.005},
    }
    assert not review.approve(answers, 0.8, 0.15)[0]


def test_inherited_subject_keeps_explicit_fixture_and_new_area_can_override():
    text = "Turn on the desk lamp and make its rim blue"
    review = TypedReview(
        fixture_catalog(),
        compound_plan("desk", "desk_rim"),
        text,
        sources=["Turn on the desk lamp", "make its rim blue"],
    )
    assert review.expected["operation_1_target"] == "office:desk_rim"
    instructions = review.questions["operation_1_target"]["instructions"]
    assert "unless another one is named" in instructions
    assert "not MAIN for every clause" in instructions
    answers = answers_for(review)
    assert review.approve(answers, 0.8, 0.15)[0]
    # Even an exact excerpt never permits a conflicting target verdict.
    q = review.questions["operation_1_target"]["criteria"]
    answers["operation_1_target"] = {
        "type": "choice",
        "choice": "office:main",
        "confidence": 0.99,
        "probabilities": {
            key: 0.99 if key == "office:main" else 0.01 / (len(q) - 1) for key in q
        },
    }
    assert not review.approve(answers, 0.8, 0.15)[0]


def test_typed_review_rejects_unvalidated_bindings():
    with pytest.raises(PlanError):
        TypedReview(fixture_catalog(), compound_plan(), "turn on", sources=["turn on"])
    with pytest.raises(PlanError):
        TypedReview(
            fixture_catalog(),
            compound_plan(),
            "turn on",
            sources=["turn on", "invented"],
        )


@pytest.mark.parametrize("compact", [False, True])
async def test_planner_provenance_is_removed_before_canonical_plan_validation(compact):
    from types import SimpleNamespace

    catalog = fixture_catalog()
    text = "Turn on the desk lamp and make its rim blue"
    canonical = compound_plan("desk", "desk_rim")
    transport = {
        **canonical,
        "operation_sources": ["Turn on the desk lamp", "make its rim blue"],
    }
    if compact:
        transport["operations"] = [
            {
                "capability": op["capability"],
                "arguments_json": json.dumps(op["arguments"]),
            }
            for op in canonical["operations"]
        ]
    runtime = SimpleNamespace(
        catalog=catalog,
        config={
            "models": {
                "planner": "google/gemini-3.1-flash-lite"
                if compact
                else "openai/gpt-4o-mini"
            },
            "instructions": "synthetic preferences",
        },
        api=AsyncMock(
            return_value={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(transport)},
                    }
                ]
            }
        ),
        chat_content=HouseholdRuntime.chat_content,
    )
    request = {"timing": {"choice": "now"}}
    result = await HouseholdRuntime.plan(runtime, text, [], {"light"}, {}, request)
    assert result == canonical
    assert request["operation_sources"] == transport["operation_sources"]
    schema = runtime.api.call_args.args[1]["response_format"]["json_schema"]["schema"]
    assert "operation_sources" in schema["required"]
    assert "operation_sources" not in catalog.schema({"light"})["properties"]


async def test_verify_rejects_missing_sources_before_any_model_call():
    from types import SimpleNamespace

    runtime = SimpleNamespace(
        catalog=fixture_catalog(), client=SimpleNamespace(async_ask=AsyncMock())
    )
    with pytest.raises(PlanError):
        await HouseholdRuntime.verify(runtime, "turn on", [], compound_plan(), {}, {})
    runtime.client.async_ask.assert_not_called()
