"""The System One client: request shape, error mapping, retries, circuit breaker."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    mock_aiohttp_client,
)

from custom_components.typesafe_conversation.const import (
    OPENROUTER_API_URL,
    OPENROUTER_KEY_URL,
    TYPESAFE_API_URL,
)
from custom_components.typesafe_conversation.system_one import (
    ChoiceAnswer,
    SystemOneAuthError,
    SystemOneClient,
    SystemOneError,
    SystemOneRequestError,
    SystemOneUnavailableError,
    _parse_answer,
)

OK = {
    "model": "jev-1.13.0",
    "answers": {
        "category": {
            "type": "choice",
            "choice": "command",
            "probabilities": {"command": 0.9, "query": 0.08, "information": 0.02},
            "confidence": 0.85,
        },
        "compound": {"type": "noul", "noul": 0.04},
        "frustration": {
            "type": "score",
            "score": 1.05,
            "legend": {"0": "Calm", "1": "Cross"},
            "probabilities": {"0": 0.2, "1": 0.8},
            "confidence": 0.7,
        },
    },
    "usage": {"input_tokens": 6482, "output_tokens": 210},
}


@pytest.fixture(name="mocker")
def mocker_fixture():
    with mock_aiohttp_client() as mocker:
        yield mocker


@pytest.fixture(name="client")
async def client_fixture(mocker: AiohttpClientMocker):
    session = mocker.create_session(asyncio.get_running_loop())
    return SystemOneClient(session, "sk-test", "jev-latest")


async def test_request_shape_and_typed_answers(client, mocker):
    mocker.post(TYPESAFE_API_URL, json=OK)
    response = await client.async_ask({"request": {"text": "hi"}}, {"category": {}})

    _method, _url, body, headers = mocker.mock_calls[0]
    assert body["model"] == "jev-latest"
    assert body["state"] == {"request": {"text": "hi"}}
    assert headers["Authorization"] == "Bearer sk-test"

    assert response.model == "jev-1.13.0"
    assert response.input_tokens == 6482
    assert response.choice("category").choice == "command"
    assert response.noul("compound") == 0.04
    assert response.score("frustration").score == 1.05
    # Wrong-typed access returns None rather than raising.
    assert response.choice("compound") is None


def test_margin_catches_a_confident_looking_tie():
    """Confidence and margin fail differently, which is why we check both."""
    tied = ChoiceAnswer("a", {"a": 0.45, "b": 0.44, "c": 0.11}, 0.62)
    assert tied.confidence > 0.6
    assert tied.margin < 0.05, "top two are effectively tied"


async def test_auth_failure_is_not_retried(client, mocker):
    mocker.post(TYPESAFE_API_URL, status=401, text="nope")
    with pytest.raises(SystemOneAuthError):
        await client.async_ask({}, {})
    assert len(mocker.mock_calls) == 1


async def test_validation_failure_is_not_retried(client, mocker):
    """A 422 is our bug, not a transient one - retrying just wastes time."""
    mocker.post(TYPESAFE_API_URL, status=422, text='{"detail":"questions.x.criteria"}')
    with pytest.raises(SystemOneRequestError, match="HTTP 422") as failure:
        await client.async_ask({}, {})
    assert len(mocker.mock_calls) == 1
    assert "criteria" not in str(failure.value)


@pytest.fixture(name="openrouter_client")
async def openrouter_client_fixture(mocker):
    return SystemOneClient(
        mocker.create_session(asyncio.get_running_loop()),
        "sk-or-test",
        "typesafe/jev-1.13",
        provider="openrouter",
    )


async def test_openrouter_uses_systemone_not_chat(openrouter_client, mocker):
    payload = deepcopy(OK)
    payload["model"] = "typesafe/jev-1.13-20260917"
    payload["usage"]["cost"] = 0.0003
    mocker.post(OPENROUTER_API_URL, json=payload)
    response = await openrouter_client.async_ask({}, {})
    assert mocker.mock_calls[0][2]["model"] == "typesafe/jev-1.13"
    assert response.cost_usd == 0.0003
    assert response.input_tokens == 6482


async def test_openrouter_auth_uses_authenticated_key_endpoint(
    openrouter_client, mocker
):
    mocker.get(OPENROUTER_KEY_URL, json={"data": {"limit_remaining": 1.0}})
    assert await openrouter_client.async_validate() == ["typesafe/jev-1.13"]
    assert mocker.mock_calls[0][0] == "GET"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 529])
async def test_openrouter_never_repeats_a_billed_request(
    openrouter_client,
    mocker,
    status,
):
    mocker.post(OPENROUTER_API_URL, status=status)
    with pytest.raises((SystemOneAuthError, SystemOneUnavailableError)):
        await openrouter_client.async_ask({}, {})
    assert len(mocker.mock_calls) == 1


async def test_openrouter_rejects_a_different_model(openrouter_client, mocker):
    mocker.post(OPENROUTER_API_URL, json={**OK, "model": "other/model"})
    with pytest.raises(SystemOneError, match="Unexpected decision model"):
        await openrouter_client.async_ask({}, {})


async def test_openrouter_does_not_accept_router_alias(mocker):
    with pytest.raises(ValueError, match="pinned"):
        SystemOneClient(
            mocker.create_session(asyncio.get_running_loop()),
            "sk-or-test",
            "typesafe/jev-router",
            provider="openrouter",
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "0.9", -0.1, 1.1])
def test_malformed_probabilities_are_not_decisions(value):
    with pytest.raises(SystemOneError):
        _parse_answer("risk", {"type": "noul", "noul": value})


def test_a_choice_must_appear_in_its_distribution():
    with pytest.raises(SystemOneError, match="distribution"):
        _parse_answer(
            "category",
            {
                "type": "choice",
                "choice": "invented",
                "probabilities": {"command": 1.0},
                "confidence": 1.0,
            },
        )


async def test_rate_limit_is_retried_then_gives_up(client, mocker):
    mocker.post(TYPESAFE_API_URL, status=429, text="slow down")
    with pytest.raises(SystemOneUnavailableError):
        await client.async_ask({}, {})
    assert len(mocker.mock_calls) == 3, "three attempts, then the fallback ladder"


async def test_circuit_opens_after_repeated_failure(client, mocker):
    """Three failed requests, then stop trying for a while.

    A dead API must not add six seconds of timeout to every utterance.
    """
    mocker.post(TYPESAFE_API_URL, status=500, text="boom")
    for _ in range(3):
        with pytest.raises(SystemOneUnavailableError):
            await client.async_ask({}, {})
    assert client.circuit_open

    before = len(mocker.mock_calls)
    with pytest.raises(SystemOneUnavailableError, match="circuit breaker"):
        await client.async_ask({}, {})
    assert len(mocker.mock_calls) == before, "no request while the circuit is open"


async def test_success_resets_the_failure_count(client, mocker):
    mocker.post(TYPESAFE_API_URL, status=500, text="boom")
    with pytest.raises(SystemOneUnavailableError):
        await client.async_ask({}, {})
    mocker.clear_requests()
    mocker.post(TYPESAFE_API_URL, json=OK)
    await client.async_ask({}, {})
    assert not client.circuit_open
