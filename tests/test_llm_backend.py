"""The two LLM operations, against mocked HTTP."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    mock_aiohttp_client,
)

from custom_components.typesafe_conversation.llm_backend import (
    LLMBackendError,
    OllamaBackend,
    OpenAICompatBackend,
)


@pytest.fixture(name="mocker")
def mocker_fixture():
    """Home Assistant's own aiohttp mock.

    aioresponses does not track the aiohttp version HA pins, so use the mock
    that ships with the test harness instead.
    """
    with mock_aiohttp_client() as mocker:
        yield mocker


@pytest.fixture(name="session")
async def session_fixture(mocker: AiohttpClientMocker):
    import asyncio

    return mocker.create_session(asyncio.get_running_loop())


def _ollama(text: str) -> dict:
    return {"message": {"role": "assistant", "content": text}}


def _openai(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["turn off the lights", "lock the door"]',
         ["turn off the lights", "lock the door"]),
        ('```json\n["a", "b"]\n```', ["a", "b"]),
        ('Sure! Output: ["a", "b"]', ["a", "b"]),
        ('["only one"]', ["only one"]),
    ],
)
async def test_split_parses_the_shapes_models_actually_return(
    session, mocker, raw, expected
):
    backend = OllamaBackend(session, "http://ollama:11434", "qwen")
    mocker.post("http://ollama:11434/api/chat", json=_ollama(raw))
    assert await backend.split_compound("anything") == expected


@pytest.mark.parametrize("raw", ["I cannot do that", "", "{}", "[1, 2, 3]"])
async def test_split_falls_back_to_the_original_utterance(session, mocker, raw):
    """A useless split must cost the user nothing.

    The request still runs as a single command, which is what would have
    happened if the compound question had never fired.
    """
    backend = OllamaBackend(session, "http://ollama:11434", "qwen")
    mocker.post("http://ollama:11434/api/chat", json=_ollama(raw))
    assert await backend.split_compound("turn on the lamp") == ["turn on the lamp"]


async def test_split_survives_the_llm_being_down(session, mocker):
    backend = OllamaBackend(session, "http://ollama:11434", "qwen")
    mocker.post("http://ollama:11434/api/chat", status=500, text="boom")
    assert await backend.split_compound("a and b") == ["a and b"]


async def test_split_is_capped(session, mocker):
    backend = OllamaBackend(session, "http://ollama:11434", "qwen")
    raw = "[" + ",".join(f'"cmd {i}"' for i in range(20)) + "]"
    mocker.post("http://ollama:11434/api/chat", json=_ollama(raw))
    assert len(await backend.split_compound("x")) == 6


async def test_ollama_request_shape(session, mocker):
    backend = OllamaBackend(session, "http://ollama:11434", "qwen")
    mocker.post("http://ollama:11434/api/chat", json=_ollama("[]"))
    await backend.split_compound("x")
    body = mocker.mock_calls[0][2]
    assert body["stream"] is False, "streaming would only add latency here"
    assert body["keep_alive"] == "30m"
    assert body["options"]["temperature"] == 0.0


async def test_openrouter_headers_and_shape(session, mocker):
    backend = OpenAICompatBackend(
        session,
        "https://openrouter.ai/api",
        "deepseek/deepseek-v4-flash",
        api_key="sk-test",
        referer="https://example.invalid",
        title="Test",
    )
    mocker.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json=_openai("The Oakland A's."),
    )
    answer = await backend.answer_freeform(
        "who won the 1989 world series",
        [],
        home_state="",
        local_time="10:00",
        weekday="Monday",
        speaker_area="Kitchen",
    )
    _method, _url, body, headers = mocker.mock_calls[0]
    assert answer == "The Oakland A's."
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["HTTP-Referer"] == "https://example.invalid"
    assert headers["X-Title"] == "Test"
    assert body["stream"] is False


async def test_answer_raises_when_the_backend_fails(session, mocker):
    """Unlike split, a failed answer has no safe default - it must surface."""
    backend = OpenAICompatBackend(session, "https://x.invalid", "m", api_key="k")
    mocker.post("https://x.invalid/v1/chat/completions", status=502, text="bad")
    with pytest.raises(LLMBackendError):
        await backend.answer_freeform(
            "hi", [], home_state="", local_time="10:00",
            weekday="Monday", speaker_area=None,
        )


async def test_home_state_reaches_the_prompt(session, mocker):
    backend = OllamaBackend(session, "http://ollama:11434", "qwen")
    mocker.post("http://ollama:11434/api/chat", json=_ollama("ok"))
    await backend.answer_freeform(
        "is the garage shut?",
        [("earlier", "reply")],
        home_state="Garage Door (cover, Garage): closed",
        local_time="22:40",
        weekday="Sunday",
        speaker_area="Kitchen",
    )
    messages = mocker.mock_calls[0][2]["messages"]
    assert "Garage Door (cover, Garage): closed" in messages[0]["content"]
    assert "Kitchen" in messages[0]["content"]
    # The model is told plainly that it cannot act.
    assert "cannot control any device" in messages[0]["content"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
