"""The question set: validity, caps, and what it costs."""

from __future__ import annotations

import pytest

from conftest import build_catalog
from custom_components.typesafe_conversation.const import MAX_CHOICE_OPTIONS
from custom_components.typesafe_conversation.entities import CatalogEntity
from custom_components.typesafe_conversation.extraction import extract
from custom_components.typesafe_conversation.questions import (
    QuestionSetError,
    build_questions,
    estimate_tokens,
    validate_questions,
)


def _build(**kwargs):
    entities, areas = build_catalog()
    domains = tuple(sorted({e.domain for e in entities}))
    return build_questions(
        entities=entities,
        areas=areas,
        domains=domains,
        extraction=kwargs.pop(
            "extraction", extract("", want_media=False, want_color=False)
        ),
        **kwargs,
    )


def test_every_question_is_valid():
    validate_questions(_build())


def test_entity_options_are_ids_with_an_escape_hatch():
    questions = _build()
    entities, _ = build_catalog()
    criteria = questions["target_entity"]["criteria"]
    for entity in entities:
        assert entity.entity_id in criteria
    assert "no_single_entity" in criteria, "the model must be able to say 'none'"
    # Null descriptions: the detail already lives in `home.entities`.
    assert criteria[entities[0].entity_id] is None


def test_inline_descriptions_are_opt_in_and_cost_more():
    lean = estimate_tokens(_build())
    rich = estimate_tokens(_build(inline_descriptions=True))
    assert rich > lean


def test_one_action_question_per_present_domain():
    questions = _build()
    entities, _ = build_catalog()
    domains = {e.domain for e in entities}
    assert "action_light" in questions
    assert "action_lock" in questions
    # A home with no humidifier pays nothing for humidifier questions.
    assert "humidifier" not in domains
    assert "action_humidifier" not in questions
    for key, question in questions.items():
        if key.startswith("action_"):
            assert "not_targeted" in question["criteria"], key


def test_conditional_questions_only_appear_when_earned():
    assert "value_pick" not in _build()
    with_value = _build(
        extraction=extract("set it to 30%", want_media=True, want_color=True)
    )
    assert "value_pick" in with_value
    assert "30%" in with_value["value_pick"]["criteria"]


def test_choice_cap_is_enforced():
    entities = tuple(
        CatalogEntity(
            entity_id=f"light.l{i}",
            name=f"Light {i}",
            aliases=(),
            area_id=None,
            area_name=None,
            floor_name=None,
            domain="light",
            device_class=None,
            supported_features=0,
        )
        for i in range(MAX_CHOICE_OPTIONS + 50)
    )
    with pytest.raises(QuestionSetError, match="exceeds the cap"):
        build_questions(
            entities=entities,
            areas=(),
            domains=("light",),
            extraction=extract("", want_media=False, want_color=False),
        )


def test_the_whole_request_fits_the_context_budget():
    """Jev allows 64k per request and 32k for state plus the longest question."""
    from conftest import load_home

    questions = _build()
    state_tokens = estimate_tokens({"home": load_home()})
    longest = max(estimate_tokens(q) for q in questions.values())
    total = state_tokens + estimate_tokens(questions)
    assert state_tokens + longest < 30_000
    assert total < 60_000


def test_validator_rejects_a_malformed_question():
    with pytest.raises(QuestionSetError, match="instructions"):
        validate_questions({"x": {"type": "noul", "instructions": ""}})
    with pytest.raises(QuestionSetError, match="2-10 levels"):
        validate_questions(
            {"x": {"type": "score", "instructions": "how much", "criteria": ["one"]}}
        )
    with pytest.raises(QuestionSetError, match="unknown question type"):
        validate_questions({"x": {"type": "vibe", "instructions": "hmm"}})
