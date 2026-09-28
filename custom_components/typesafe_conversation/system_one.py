"""Async client for the TypeSafe System One (Jev) API.

Deliberately not the ``typesafe-sdk`` package: it depends on ``httpx2``, which
Home Assistant does not ship, and the endpoint is a single POST. Using the
aiohttp session HA already manages keeps the integration dependency-free.
"""

from __future__ import annotations

import asyncio
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
    LOGGER,
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

    def choice(self, key: str) -> ChoiceAnswer | None:
        answer = self.answers.get(key)
        return answer if isinstance(answer, ChoiceAnswer) else None

    def score(self, key: str) -> ScoreAnswer | None:
        answer = self.answers.get(key)
        return answer if isinstance(answer, ScoreAnswer) else None

    def noul(self, key: str) -> float | None:
        answer = self.answers.get(key)
        return answer.noul if isinstance(answer, NoulAnswer) else None


def _parse_answer(key: str, payload: dict[str, Any]) -> Answer:
    kind = payload.get("type")
    if kind == "choice":
        return ChoiceAnswer(
            choice=payload["choice"],
            probabilities={k: float(v) for k, v in payload["probabilities"].items()},
            confidence=float(payload["confidence"]),
        )
    if kind == "score":
        return ScoreAnswer(
            score=float(payload["score"]),
            legend=dict(payload.get("legend", {})),
            probabilities={k: float(v) for k, v in payload["probabilities"].items()},
            confidence=float(payload["confidence"]),
        )
    if kind == "noul":
        return NoulAnswer(noul=float(payload["noul"]))
    raise SystemOneError(f"Unknown answer type {kind!r} for question {key!r}")


class SystemOneClient:
    """Talks to ``POST /v1/systemone``, with retries and a circuit breaker."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        model: str,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._model = model
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
                TYPESAFE_MODELS_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as response:
                if response.status in (401, 403):
                    raise SystemOneAuthError("Invalid TypeSafe API key")
                response.raise_for_status()
                payload = await response.json()
        except SystemOneError:
            raise
        except aiohttp.ClientError as err:
            raise SystemOneUnavailableError(str(err)) from err
        except TimeoutError as err:
            raise SystemOneUnavailableError("Timed out reaching TypeSafe") from err
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

        for attempt in range(API_MAX_RETRIES):
            try:
                payload = await self._post(body)
            except SystemOneUnavailableError as err:
                last_error = err
                if attempt == API_MAX_RETRIES - 1:
                    break
                delay = (
                    err.retry_after
                    if isinstance(err, _RetryableError) and err.retry_after
                    else API_BACKOFF[attempt]
                )
                LOGGER.debug(
                    "Request attempt %s/%s failed (%s); retrying in %.2fs",
                    attempt + 1,
                    API_MAX_RETRIES,
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            except SystemOneError:
                # Auth and validation errors are not worth retrying.
                self._record_failure()
                raise

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
                TYPESAFE_API_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as response:
                if response.status in (401, 403):
                    raise SystemOneAuthError("Invalid TypeSafe API key")
                if response.status == 422:
                    detail = await response.text()
                    # Our question builder produced something invalid. The body
                    # names the offending field, so log it loudly - the
                    # build-time validator should have caught this.
                    raise SystemOneRequestError(
                        f"The API rejected the request: {detail}"
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
            raise _RetryableError(str(err)) from err
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
        return max(0.0, float(value))
    except ValueError:
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
