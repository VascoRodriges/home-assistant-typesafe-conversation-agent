"""Async client for the TypeSafe System One (Jev) API.

Deliberately not the ``typesafe-sdk`` package: it depends on ``httpx2``, which
Home Assistant does not ship, and the endpoint is a single POST. Using the
aiohttp session HA already manages keeps the integration dependency-free.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import (
    API_BACKOFF,
    API_MAX_RETRIES,
    API_TIMEOUT,
    CIRCUIT_FAILURE_THRESHOLD,
    CIRCUIT_RESET_SECONDS,
    DEFAULT_OPENROUTER_MODEL,
    LOGGER,
    OPENROUTER_API_URL,
    OPENROUTER_KEY_URL,
    PROVIDER_OPENROUTER,
    PROVIDER_TYPESAFE,
    TYPESAFE_API_URL,
    TYPESAFE_MODELS_URL,
)


class SystemOneError(Exception):
    """Any failure talking to the System One API."""


class SystemOneAuthError(SystemOneError):
    """The API key is missing, invalid, or lacks access."""


class SystemOneRequestError(SystemOneError):
    """Our request was malformed. This is a bug in our question builder."""


class SystemOneUnavailableError(SystemOneError):
    """Jev is rate limited, overloaded, unreachable, or the breaker is open."""


@dataclass(slots=True)
class ChoiceAnswer:
    """One Choice answer."""

    choice: str
    probabilities: dict[str, float]
    confidence: float

    @property
    def margin(self) -> float:
        """p(top) - p(second).

        Confidence and margin fail differently: a distribution can be peaked
        enough to look confident while two options remain effectively tied.
        """
        if len(self.probabilities) < 2:
            return 1.0
        top, second = sorted(self.probabilities.values(), reverse=True)[:2]
        return top - second


@dataclass(slots=True)
class ScoreAnswer:
    """One Score answer."""

    score: float
    legend: dict[str, str]
    probabilities: dict[str, float]
    confidence: float


@dataclass(slots=True)
class NoulAnswer:
    """One Noul answer. Has no confidence - the value carries the uncertainty."""

    noul: float


Answer = ChoiceAnswer | ScoreAnswer | NoulAnswer


@dataclass(slots=True)
class SystemOneResponse:
    """A parsed System One response."""

    model: str
    answers: dict[str, Answer]
    input_tokens: int
    output_tokens: int
    latency_ms: float
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    cost_usd: float | None = None

    def choice(self, key: str) -> ChoiceAnswer | None:
        answer = self.answers.get(key)
        return answer if isinstance(answer, ChoiceAnswer) else None

    def score(self, key: str) -> ScoreAnswer | None:
        answer = self.answers.get(key)
        return answer if isinstance(answer, ScoreAnswer) else None

    def noul(self, key: str) -> float | None:
        answer = self.answers.get(key)
        return answer.noul if isinstance(answer, NoulAnswer) else None


def _number(value: Any, *, probability: bool = False) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or (probability and not 0 <= value <= 1)
    ):
        raise SystemOneError("Invalid numeric decision value")
    return float(value)


def _parse_answer(key: str, payload: dict[str, Any]) -> Answer:
    """Reject malformed numbers before the pure router can consider acting."""
    try:
        return _parse_typed_answer(payload)
    except (KeyError, TypeError, AttributeError) as err:
        raise SystemOneError("Malformed typed decision answer") from err


def _parse_typed_answer(payload: dict[str, Any]) -> Answer:
    kind = payload.get("type")
    if kind == "choice":
        choice = payload["choice"]
        probabilities = {
            k: _number(v, probability=True) for k, v in payload["probabilities"].items()
        }
        if not isinstance(choice, str) or choice not in probabilities:
            raise SystemOneError("Choice is not in its probability distribution")
        return ChoiceAnswer(
            choice=choice,
            probabilities=probabilities,
            confidence=_number(payload["confidence"], probability=True),
        )
    if kind == "score":
        return ScoreAnswer(
            score=_number(payload["score"]),
            legend=dict(payload.get("legend", {})),
            probabilities={
                k: _number(v, probability=True)
                for k, v in payload["probabilities"].items()
            },
            confidence=_number(payload["confidence"], probability=True),
        )
    if kind == "noul":
        return NoulAnswer(noul=_number(payload["noul"], probability=True))
    raise SystemOneError("Unknown decision answer type")


class SystemOneClient:
    """Talks to ``POST /v1/systemone``, with retries and a circuit breaker."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        model: str,
        *,
        provider: str = PROVIDER_TYPESAFE,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._model = model
        if provider not in (PROVIDER_TYPESAFE, PROVIDER_OPENROUTER):
            raise ValueError("Unknown decision provider")
        if provider == PROVIDER_OPENROUTER and model != DEFAULT_OPENROUTER_MODEL:
            raise ValueError("OpenRouter decisions require pinned typesafe/jev-1.13")
        self._provider = provider
        self._api_url = (
            OPENROUTER_API_URL if provider == PROVIDER_OPENROUTER else TYPESAFE_API_URL
        )
        # A timed-out billed request might already have completed. Do not repeat it.
        self._attempts = 1 if provider == PROVIDER_OPENROUTER else API_MAX_RETRIES
        self._consecutive_failures = 0
        self._open_until = 0.0

    @property
    def circuit_open(self) -> bool:
        """True while we are deliberately not calling the API."""
        if self._open_until and time.monotonic() >= self._open_until:
            # Half-open: let the next request through and see what happens.
            self._open_until = 0.0
            self._consecutive_failures = 0
        return bool(self._open_until)

    async def async_validate(self) -> list[str]:
        """Check the key and return the model names the account can use."""
        try:
            async with self._session.get(
                OPENROUTER_KEY_URL
                if self._provider == PROVIDER_OPENROUTER
                else TYPESAFE_MODELS_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise SystemOneAuthError("Invalid decision-provider API key")
                if 300 <= response.status < 400:
                    raise SystemOneUnavailableError("Provider redirect rejected")
                response.raise_for_status()
                payload = await response.json()
        except SystemOneError:
            raise
        except aiohttp.ClientError as err:
            raise SystemOneUnavailableError(
                "Could not reach decision provider"
            ) from err
        except TimeoutError as err:
            raise SystemOneUnavailableError("Timed out reaching TypeSafe") from err
        if self._provider == PROVIDER_OPENROUTER:
            if not isinstance(payload.get("data"), dict):
                raise SystemOneUnavailableError("Unexpected key-validation response")
            return [self._model]
        return [m["name"] for m in payload.get("models", [])]

    async def async_ask(
        self, state: Any, questions: dict[str, dict[str, Any]]
    ) -> SystemOneResponse:
        """Send one state and every question, and return the typed answers."""
        if self.circuit_open:
            raise SystemOneUnavailableError("System One circuit breaker is open")

        body = {"state": state, "model": self._model, "questions": questions}
        started = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(self._attempts):
            try:
                payload = await self._post(body)
            except SystemOneUnavailableError as err:
                last_error = err
                if attempt == self._attempts - 1:
                    break
                delay = (
                    err.retry_after
                    if isinstance(err, _RetryableError) and err.retry_after
                    else API_BACKOFF[attempt]
                )
                LOGGER.debug(
                    "Request attempt %s/%s failed (%s); retrying in %.2fs",
                    attempt + 1,
                    self._attempts,
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            except SystemOneError:
                # Auth and validation errors are not worth retrying.
                self._record_failure()
                raise

            if self._provider == PROVIDER_OPENROUTER:
                served = payload.get("model")
                if served != self._model and not (
                    isinstance(served, str) and served.startswith(self._model + "-")
                ):
                    raise SystemOneError(
                        "Unexpected decision model; no action permitted"
                    )
            self._consecutive_failures = 0
            latency_ms = (time.monotonic() - started) * 1000
            usage = payload.get("usage", {})
            response = SystemOneResponse(
                model=payload.get("model", self._model),
                answers={
                    key: _parse_answer(key, value)
                    for key, value in payload.get("answers", {}).items()
                },
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                latency_ms=latency_ms,
                raw=payload,
                cost_usd=_parse_cost(usage.get("cost")),
            )
            LOGGER.debug(
                "%s answered %s questions in %.0fms (%s input tokens)",
                response.model,
                len(response.answers),
                latency_ms,
                response.input_tokens,
            )
            return response

        self._record_failure()
        raise SystemOneUnavailableError(
            str(last_error) if last_error else "System One unavailable"
        )

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._session.post(
                self._api_url,
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise SystemOneAuthError("Invalid decision-provider API key")
                if 300 <= response.status < 400:
                    raise SystemOneUnavailableError("Provider redirect rejected")
                if response.status in (400, 422):
                    # Provider errors can echo credentials or private home state.
                    raise SystemOneRequestError(
                        f"The API rejected the request (HTTP {response.status})"
                    )
                if response.status in (429, 529):
                    raise _RetryableError(
                        f"The API returned {response.status}",
                        retry_after=_parse_retry_after(
                            response.headers.get("retry-after")
                        ),
                    )
                if response.status >= 500:
                    raise _RetryableError(f"The API returned {response.status}")
                response.raise_for_status()
                return await response.json()
        except SystemOneError:
            raise
        except aiohttp.ClientError as err:
            raise _RetryableError("Could not reach decision provider") from err
        except TimeoutError as err:
            raise _RetryableError("Timed out talking to the System One API") from err

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            self._open_until = time.monotonic() + CIRCUIT_RESET_SECONDS
            LOGGER.warning(
                "The API failed %s times in a row; pausing calls for %.0fs and "
                "serving the fallback agent instead",
                self._consecutive_failures,
                CIRCUIT_RESET_SECONDS,
            )


class _RetryableError(SystemOneUnavailableError):
    """A failure worth another attempt."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        result = float(value)
        return max(0.0, result) if math.isfinite(result) else None
    except ValueError:
        return None


def _parse_cost(value: Any) -> float | None:
    """Missing cost is unknown, not free."""
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value)
    return None


__all__ = [
    "ChoiceAnswer",
    "NoulAnswer",
    "ScoreAnswer",
    "SystemOneAuthError",
    "SystemOneClient",
    "SystemOneError",
    "SystemOneRequestError",
    "SystemOneResponse",
    "SystemOneUnavailableError",
]
