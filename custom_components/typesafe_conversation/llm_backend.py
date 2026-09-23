"""The two things we still need a text-generating model for.

Everything about controlling the home is decided by Jev and executed by code.
The LLM is used only where a string genuinely has to be produced:

1. splitting a compound request into atomic commands, and
2. answering a general-knowledge or prose question.

It is never given tools and never told it can control anything, so a slow or
failing LLM can delay an answer but can never block or corrupt a device action.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import json
import re
from typing import Any

import aiohttp

from .const import (
    ANSWER_MAX_TOKENS,
    ANSWER_TEMPERATURE,
    ANSWER_TIMEOUT,
    BACKEND_OLLAMA,
    BACKEND_OPENAI_COMPAT,
    LOGGER,
    MAX_SUB_COMMANDS,
    OLLAMA_KEEP_ALIVE,
    SPLIT_MAX_TOKENS,
    SPLIT_TIMEOUT,
)

SPLIT_SYSTEM_PROMPT = """\
You split a smart-home voice command into atomic commands.

Rules:
- Output ONLY a JSON array of strings. No prose, no explanation, no markdown, \
no code fences.
- Each string must be a complete, self-contained command that names its own \
target.
- Distribute shared subjects and verbs: "turn off the lights and the fan" -> \
["turn off the lights", "turn off the fan"]
- Keep the user's original wording and their original order.
- Never invent a device, room, value or action that is not in the input.
- Do not split one action applied to several devices: "turn off all the \
lights" is ONE command.
- If the input is a single command, return a one-element array.

Input: turn off the kitchen lights and lock the front door
Output: ["turn off the kitchen lights", "lock the front door"]

Input: dim the living room lights to 30% and start the coffee maker
Output: ["dim the living room lights to 30%", "start the coffee maker"]

Input: turn off all of the lights in the house
Output: ["turn off all of the lights in the house"]

Input: set the thermostat to 21, close the blinds, and play some jazz
Output: ["set the thermostat to 21", "close the blinds", "play some jazz"]"""

ANSWER_SYSTEM_PROMPT = """\
You are the voice assistant for a Home Assistant smart home.

- Reply in plain spoken text. No markdown, no bullet lists, no emoji, no \
headings.
- Be brief: one or two sentences, under 40 words, as if speaking aloud.
- You cannot control any device in this mode. Never claim to have turned \
anything on or off, and never promise to do something.
- Use the home state below when the question is about the home. If the answer \
is not in it, or you are not confident, say you do not know rather than \
guessing.
- Answer general-knowledge questions truthfully and briefly from your own \
knowledge.

Current time: {local_time} on {weekday}. The speaker is in {speaker_area}.

Home state:
{home_state}"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class LLMBackendError(Exception):
    """Any failure talking to the configured LLM."""


class LLMBackend(ABC):
    """Two operations. Nothing else is ever asked of the LLM."""

    name: str

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        model: str,
        api_key: str | None = None,
        answer_timeout: float = ANSWER_TIMEOUT,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._answer_timeout = answer_timeout

    @abstractmethod
    async def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> str:
        """Send a chat completion and return the assistant's text."""

    async def async_warm_up(self) -> None:
        """Nudge the model into memory. Overridden where it helps."""

    async def split_compound(self, utterance: str) -> list[str]:
        """Break a compound request into atomic commands.

        Never raises. If the model is slow, unreachable, or returns something
        that is not a JSON array, we fall back to treating the utterance as a
        single command - which is exactly what would have happened without the
        compound question. A failure here must not cost the user their command.
        """
        try:
            raw = await self._chat(
                [
                    {"role": "system", "content": SPLIT_SYSTEM_PROMPT},
                    {"role": "user", "content": utterance},
                ],
                max_tokens=SPLIT_MAX_TOKENS,
                temperature=0.0,
                timeout=SPLIT_TIMEOUT,
            )
        except LLMBackendError as err:
            LOGGER.warning("Could not split a compound request (%s)", err)
            return [utterance]

        parts = _parse_string_array(raw)
        if not parts:
            LOGGER.warning("LLM split returned no usable array: %r", raw[:200])
            return [utterance]
        if len(parts) > MAX_SUB_COMMANDS:
            LOGGER.warning(
                "LLM split produced %s parts; keeping the first %s",
                len(parts),
                MAX_SUB_COMMANDS,
            )
            parts = parts[:MAX_SUB_COMMANDS]
        LOGGER.debug("Split %r into %s", utterance, parts)
        return parts

    async def answer_freeform(
        self,
        utterance: str,
        history: list[tuple[str, str]],
        *,
        home_state: str,
        local_time: str,
        weekday: str,
        speaker_area: str | None,
    ) -> str:
        """Answer a general or prose question in natural language."""
        system = ANSWER_SYSTEM_PROMPT.format(
            local_time=local_time,
            weekday=weekday,
            speaker_area=speaker_area or "an unknown room",
            home_state=home_state,
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for user_text, assistant_text in history:
            messages.append({"role": "user", "content": user_text})
            if assistant_text:
                messages.append({"role": "assistant", "content": assistant_text})
        messages.append({"role": "user", "content": utterance})

        text = await self._chat(
            messages,
            max_tokens=ANSWER_MAX_TOKENS,
            temperature=ANSWER_TEMPERATURE,
            timeout=self._answer_timeout,
        )
        return text.strip()


class OllamaBackend(LLMBackend):
    """Ollama's native chat endpoint."""

    name = BACKEND_OLLAMA

    async def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> str:
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        data = await _post_json(
            self._session, f"{self._base_url}/api/chat", payload, headers, timeout
        )
        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as err:
            raise LLMBackendError(f"Unexpected Ollama response: {data}") from err

    async def async_warm_up(self) -> None:
        """Keep the model resident.

        A cold 7B load is several seconds and would be blamed on this
        integration, so we pay for it in the background instead.
        """
        try:
            await self._chat(
                [{"role": "user", "content": "hi"}],
                max_tokens=1,
                temperature=0.0,
                timeout=SPLIT_TIMEOUT,
            )
        except LLMBackendError as err:
            LOGGER.debug("Ollama warm-up did not succeed: %s", err)


class OpenAICompatBackend(LLMBackend):
    """Any OpenAI-compatible /v1/chat/completions endpoint, such as OpenRouter."""

    name = BACKEND_OPENAI_COMPAT

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        model: str,
        api_key: str | None = None,
        referer: str | None = None,
        title: str | None = None,
        answer_timeout: float = ANSWER_TIMEOUT,
    ) -> None:
        super().__init__(session, base_url, model, api_key, answer_timeout)
        self._referer = referer
        self._title = title

    async def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> str:
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # OpenRouter attributes traffic with these; harmless elsewhere.
        if self._referer:
            headers["HTTP-Referer"] = self._referer
        if self._title:
            headers["X-Title"] = self._title
        data = await _post_json(
            self._session,
            f"{self._base_url}/v1/chat/completions",
            payload,
            headers,
            timeout,
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as err:
            raise LLMBackendError(f"Unexpected response: {data}") from err


async def _post_json(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    try:
        async with asyncio.timeout(timeout):
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status >= 400:
                    body = await response.text()
                    raise LLMBackendError(f"HTTP {response.status}: {body[:300]}")
                return await response.json()
    except LLMBackendError:
        raise
    except (TimeoutError, asyncio.TimeoutError) as err:
        raise LLMBackendError(f"Timed out after {timeout}s") from err
    except aiohttp.ClientError as err:
        raise LLMBackendError(str(err)) from err
    except json.JSONDecodeError as err:
        raise LLMBackendError(f"Response was not JSON: {err}") from err


def _parse_string_array(raw: str) -> list[str]:
    """Get a list of strings out of whatever the model actually returned.

    Models wrap JSON in fences, prefix it with "Output:", or bury it in a
    sentence. Try the strict reading first, then progressively looser ones.
    """
    for candidate in _candidates(raw):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            items = [p.strip() for p in parsed if isinstance(p, str) and p.strip()]
            if items:
                return items
    return []


def _candidates(raw: str):
    text = raw.strip()
    yield text
    if (fenced := _FENCE_RE.search(text)) is not None:
        yield fenced.group(1)
    if (array := _ARRAY_RE.search(text)) is not None:
        yield array.group(0)


def create_backend(
    session: aiohttp.ClientSession, settings: dict[str, Any]
) -> LLMBackend | None:
    """Build the configured backend, or None when no LLM is set up.

    Without an LLM the agent still handles every command and query; it just
    cannot split compound requests or answer general questions.
    """
    from .const import (
        CONF_LLM_API_KEY,
        CONF_LLM_BACKEND,
        CONF_LLM_BASE_URL,
        CONF_LLM_MODEL,
        CONF_LLM_REFERER,
        CONF_LLM_TIMEOUT,
        CONF_LLM_TITLE,
        DEFAULT_LLM_REFERER,
        DEFAULT_LLM_TITLE,
    )

    backend = settings.get(CONF_LLM_BACKEND)
    base_url = settings.get(CONF_LLM_BASE_URL)
    model = settings.get(CONF_LLM_MODEL)
    if not backend or not base_url or not model:
        return None

    timeout = float(settings.get(CONF_LLM_TIMEOUT) or ANSWER_TIMEOUT)

    if backend == BACKEND_OLLAMA:
        return OllamaBackend(
            session, base_url, model, settings.get(CONF_LLM_API_KEY), timeout
        )
    if backend == BACKEND_OPENAI_COMPAT:
        return OpenAICompatBackend(
            session,
            base_url,
            model,
            settings.get(CONF_LLM_API_KEY),
            settings.get(CONF_LLM_REFERER) or DEFAULT_LLM_REFERER,
            settings.get(CONF_LLM_TITLE) or DEFAULT_LLM_TITLE,
            timeout,
        )
    LOGGER.error("Unknown LLM backend %r", backend)
    return None


__all__ = [
    "LLMBackend",
    "LLMBackendError",
    "OllamaBackend",
    "OpenAICompatBackend",
    "create_backend",
]
