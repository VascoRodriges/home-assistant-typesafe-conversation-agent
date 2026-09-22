"""The System One client: request shape, error mapping, retries, circuit breaker."""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    mock_aiohttp_client,
)

from custom_components.typesafe_conversation.const import TYPESAFE_API_URL
from custom_components.typesafe_conversation.system_one import (
    ChoiceAnswer,
    SystemOneAuthError,
    SystemOneClient,
    SystemOneRequestError,
    SystemOneUnavailableError,
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
    with pytest.raises(SystemOneRequestError, match="criteria"):
        await client.async_ask({}, {})
    assert len(mocker.mock_calls) == 1


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
