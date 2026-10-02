"""Bounded, read-only Recorder queries; models never calculate sensor history."""

import asyncio
import json
import math
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from homeassistant.auth.permissions.const import POLICY_READ

from .household_fast import choice, read_choice
from .household_policy import PolicyError, object_schema, validate

METRICS = {
    "point": "Last recorded value at a requested past instant, not an average.",
    "minimum": "Lowest recorded value over the requested interval.",
    "maximum": "Highest recorded value over the requested interval.",
    "average": "Time-weighted average over the requested interval.",
    "min_max": "Both lowest and highest values over the requested interval.",
    "compare_current": "Compare the current measurement with a past instant.",
}
PERIODS = {
    "yesterday_now": "Yesterday at the same LOCAL clock time as this request ONLY when latest_request says 'at this time' / 'в это время'. An explicit clock such as 21:00 MUST use custom, never this option.",
    "yesterday": "The whole previous local calendar day.",
    "today": "Today from local midnight until this request.",
    "last_night": "Most recent completed night, default 00:00-08:00 local time; an explicitly different interval needs custom.",
    "last_24h": "The rolling last 24 hours.",
    "last_7d": "The rolling last seven days.",
    "custom": "Another past date/time or interval, requiring explicit temporal parsing.",
}
MODE = {
    "historical_only": "The entire latest request ONLY asks about past household numeric sensor measurements, their statistics or a comparison with now.",
    "historical_mixed": "A historical measurement AND another task, device action or question is requested.",
    "current": "Only current household measurements are requested, no past period.",
    "other": "Not a household numeric sensor history question.",
    "unclear": "Cannot distinguish historical sensor reading from other tasks.",
}


def explicit_time(text):
    return bool(
        re.search(
            r"\b\d{1,2}:\d{2}\b|\b(?:в|at)\s+\d{1,2}\.\d{2}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b",
            text,
            re.IGNORECASE,
        )
    )


def selected(answer, criteria, threshold=0.85, margin=0.15):
    value, _confidence, gap = read_choice(answer, criteria)
    if answer["probabilities"][value] < threshold or gap < margin:
        raise PolicyError("Uncertain historical query")
    return value


class HistoryQueries:
    def __init__(self, config):
        self.options = config.get("history", {})
        self.entities = {
            key: label
            for key, label in config["read_entities"].items()
            if key.startswith("sensor.")
        }
        self.questions = {
            "reading_mode": choice(
                "Classify ONLY latest_request; history resolves 'there', 'it' and omitted room/measurement, never repeats earlier tasks.",
                MODE,
            ),
            "history_entity": choice(
                "Which ONE allowlisted sensor supplies the historical measurement? Use conversation history to resolve omitted room and metric. Room temperature uses the room sensor, not a clock unless explicitly requested.",
                {
                    **self.entities,
                    "unknown": "Multiple sensors, unspecified sensor, or no sensor history requested.",
                },
            ),
            "history_metric": choice(
                "Which historical measurement/statistic is requested? Use point for 'what was it yesterday at this time'.",
                {
                    **METRICS,
                    "unknown": "Cannot determine a single supported historical measurement.",
                },
            ),
            "history_period": choice(
                "Select the requested LOCAL historical period. Only latest_request specifies time; dialogue resolves references.",
                {**PERIODS, "unknown": "No identifiable past period."},
            ),
        }

    def fast(self, answers, text=""):
        # A typed classifier can still confuse an explicit clock with 'now'.
        # Clock/date syntax is a safety guard, not a phrase-to-action parser.
        if explicit_time(text):
            return None
        values = {}
        for field in ("entity", "metric", "period"):
            name = "history_" + field
            value = selected(answers.get(name), self.questions[name]["criteria"])
            if value in ("unknown", "custom"):
                return None
            values[field if field != "entity" else "entity_id"] = value
        if (values["metric"] in ("point", "compare_current")) != (
            values["period"] == "yesterday_now"
        ):
            return None
        return {**values, "start_local": None, "end_local": None}

    def schema(self):
        nullable_time = {
            "anyOf": [{"type": "string", "maxLength": 40}, {"type": "null"}]
        }
        return object_schema(
            {
                "entity_id": {"type": "string", "enum": [*self.entities, "unknown"]},
                "metric": {"type": "string", "enum": list(METRICS)},
                "period": {"type": "string", "enum": list(PERIODS)},
                "start_local": nullable_time,
                "end_local": nullable_time,
                "clarification": {
                    "anyOf": [{"type": "string", "maxLength": 400}, {"type": "null"}]
                },
            }
        )

    def review(self, query):
        questions = dict(self.questions)
        expected = {
            "reading_mode": "historical_only",
            "history_entity": query["entity_id"],
            "history_metric": query["metric"],
            "history_period": query["period"],
        }
        if query["period"] == "custom":
            questions["history_time_match"] = choice(
                "Do the proposed LOCAL start/end instants exactly express the latest requested time, anchored in request_time? No future dates or invented interval; dialogue resolves references only.",
                {
                    "faithful": "Requested instants, time zone and statistic match.",
                    "different": "Time/interval is invented, incorrect or cannot be resolved.",
                },
            )
            expected["history_time_match"] = "faithful"
        return questions, expected


def interval(query, now, options):
    """Resolve calendar periods locally; never substitute today's value for yesterday."""
    metric, period = query["metric"], query["period"]
    midnight = datetime.combine(now.date(), time(), now.tzinfo)
    if period == "yesterday_now":
        start = end = now - timedelta(days=1)
    elif period in ("yesterday", "today"):
        start, end = (
            (midnight - timedelta(days=1), midnight)
            if period == "yesterday"
            else (midnight, now)
        )
    elif period == "last_night":
        day = now.date() if now.hour >= 8 else now.date() - timedelta(days=1)
        start = datetime.combine(day, time(), now.tzinfo)
        end = datetime.combine(day, time(8), now.tzinfo)
    elif period in ("last_24h", "last_7d"):
        end = now
        start = now - timedelta(days=1 if period == "last_24h" else 7)
    else:
        try:
            start = datetime.fromisoformat(query["start_local"])
            end = datetime.fromisoformat(query["end_local"])
        except (TypeError, ValueError) as error:
            raise PolicyError("Invalid historical time") from error
        if start.tzinfo is None or end.tzinfo is None:
            raise PolicyError("Historical times need an explicit time zone")
        start, end = start.astimezone(now.tzinfo), end.astimezone(now.tzinfo)
    start, end, anchor = start.astimezone(UTC), end.astimezone(UTC), now.astimezone(UTC)
    if (
        not anchor - timedelta(days=options.get("max_days", 7))
        <= start
        <= end
        <= anchor
    ):
        raise PolicyError("Historical interval is outside the allowed window")
    if (metric in ("point", "compare_current")) != (start == end):
        raise PolicyError("Historical statistic and interval disagree")
    if period != "custom" and (query.get("start_local") or query.get("end_local")):
        raise PolicyError("Named historical period may not override dates")
    return start, end


def summarize(records, start, end, metric, max_age):
    """Last-known state, bounded freshness and time-weighted valid coverage."""
    rows = []
    for row in records:
        stamp = row["time"]
        if stamp.tzinfo is None:
            raise PolicyError("Recorder timestamp lacks time zone")
        stamp = stamp.astimezone(UTC)
        try:
            value = float(row["state"])
            if not math.isfinite(value):
                value = None
        except TypeError, ValueError:
            value = None
        rows.append((stamp, value))
    rows.sort(key=lambda item: item[0])
    if start == end:
        prior = [(stamp, value) for stamp, value in rows if stamp <= start]
        if not prior or prior[-1][1] is None or start - prior[-1][0] > max_age:
            return None
        stamp, value = prior[-1]
        return {"value": value, "recorded_at": stamp.isoformat(), "coverage": 1.0}
    covered, integral, values = 0.0, 0.0, []
    for index, (stamp, value) in enumerate(rows):
        next_stamp = rows[index + 1][0] if index + 1 < len(rows) else end
        left, right = max(start, stamp), min(end, next_stamp, stamp + max_age)
        seconds = (right - left).total_seconds()
        if value is not None and seconds > 0:
            covered += seconds
            integral += value * seconds
            values.append((value, stamp))
    if not values:
        return None
    low, high = min(values), max(values)
    result = {
        "minimum": low[0],
        "maximum": high[0],
        "average": integral / covered,
        "minimum_at": low[1].isoformat(),
        "maximum_at": high[1].isoformat(),
        "coverage": covered / (end - start).total_seconds(),
    }
    result["value"] = result.get(metric)
    return result


async def read_recorder(hass, entity_id, start, end, max_age):
    """Query only one validated sensor, through the Recorder executor, no SQL from models."""
    from homeassistant.components.recorder.history import get_significant_states
    from homeassistant.helpers.recorder import get_instance

    if "recorder" not in hass.config.components:
        raise PolicyError("Recorder is unavailable")

    def load():
        result = get_significant_states(
            hass,
            start - max_age,
            end + timedelta(microseconds=1),
            [entity_id],
            include_start_time_state=False,
            significant_changes_only=False,
            minimal_response=False,
            no_attributes=False,
        )
        rows = result.get(entity_id, [])
        if len(rows) > 30000:
            raise PolicyError("Historical result is too large")
        return [
            {
                "time": state.last_updated,
                "state": state.state,
                "unit": state.attributes.get("unit_of_measurement"),
            }
            for state in rows
        ]

    async with asyncio.timeout(20):
        return await get_instance(hass).async_add_executor_job(load)


async def answer_history(runtime, text, history, request, context):
    """Historical-only branch can never call the device executor or answer model."""
    router = runtime.history_router
    options = router.options
    if not options.get("enabled", False):
        return "Чтение истории датчиков сейчас отключено. Текущее показание не заменяет историческое."
    now = datetime.now(ZoneInfo(runtime.hass.config.time_zone))
    schema = router.schema()
    try:
        query = router.fast(runtime.fast_answers, text)
    except PolicyError:
        query = None
    if query is None:
        request["path"] = "history_planner_review"
        data = await runtime.api(
            "planner",
            {
                "temperature": 0,
                "max_tokens": 600,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "sensor_history_query",
                        "strict": True,
                        "schema": schema,
                    },
                },
                "messages": [
                    {
                        "role": "system",
                        "content": "Parse ONE READ-ONLY historical numeric sensor query. History resolves references, never authorizes device actions. Use ONLY the allowed entity IDs. If insufficient context or multiple sensors/tasks, ask a concise Russian clarification and use entity_id unknown. clarification MUST be null for a complete valid query: it is ONLY a question requiring missing information from the user, NEVER a note explaining your interpretation. Named periods are resolved locally: start_local/end_local MUST be null. yesterday_now means yesterday at request_time's local clock; an explicit requested clock requires custom. Point/compare_current requires a single instant; other metrics need a range. Custom timestamps MUST be offset-aware ISO datetimes in the given local zone, not UTC without conversion. If unspecified, night means last completed 00:00-08:00 local; use last_night and clarification null. Local code explains the actual interval. Do NOT answer measurements or invent values. max_days bounds the available read window.",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "latest_request": text,
                                "history": history,
                                "request_time": now.isoformat(),
                                "time_zone": runtime.hass.config.time_zone,
                                "sensors": router.entities,
                                "max_days": options.get("max_days", 7),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
            },
            request,
        )
        query = json.loads(runtime.chat_content(data))
        validate(query, schema)
        if query["clarification"] or query["entity_id"] == "unknown":
            return (
                query["clarification"]
                or "Уточните комнату, показатель и время для чтения истории."
            )
        # Validate window/type before spending on semantic review.
        if explicit_time(text) and query["period"] != "custom":
            raise PolicyError(
                "Explicit requested clock/date must not use a named period"
            )
        interval(query, now, options)
        questions, expected = router.review(query)
        reviewed = (
            await runtime.client.async_ask(
                {
                    "latest_request": text,
                    "history": history,
                    "request_time": now.isoformat(),
                    "proposed_history_query": query,
                },
                questions,
            )
        ).raw["answers"]
        if any(
            selected(reviewed.get(key), questions[key]["criteria"], 0.8) != value
            for key, value in expected.items()
        ):
            return "Уточните, пожалуйста, датчик и исторический период: не уверен, что понял их правильно."
    else:
        request["path"] = "history_jev_only"
    entity = query["entity_id"]
    if entity not in router.entities:
        raise PolicyError("Historical sensor is not allowlisted")
    if context and context.user_id:
        user = await runtime.hass.auth.async_get_user(context.user_id)
        if user is None or not user.permissions.check_entity(entity, POLICY_READ):
            raise PolicyError("Historical sensor read permission denied")
    start, end = interval(query, now, options)
    max_age = timedelta(minutes=options.get("max_age_minutes", 120))
    rows = await read_recorder(runtime.hass, entity, start, end, max_age)
    relevant = [row for row in rows if row["time"] <= end and row["unit"]]
    units = {row["unit"] for row in relevant}
    if len(units) > 1:
        return "Единица измерения датчика менялась в этом периоде. Не буду смешивать несовместимые показания."
    if not units and rows:
        return "В истории датчика нет единицы измерения. Не буду угадывать, что означают значения."
    summary = summarize(rows, start, end, query["metric"], max_age)
    evidence = {
        "entity_id": entity,
        "metric": query["metric"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "time_zone": runtime.hass.config.time_zone,
        "summary": summary,
    }
    request["history_evidence"] = evidence
    label = router.entities[entity]
    current = runtime.hass.states.get(entity)
    return render_history(
        label,
        next(iter(units), ""),
        summary,
        start,
        end,
        query["metric"],
        now.tzinfo,
        current,
    )


def render_history(label, unit, summary, start, end, metric, zone, current=None):
    def when(value):
        return value.astimezone(zone).strftime("%d.%m.%Y %H:%M")

    def number(value):
        return f"{value:.2f}".rstrip("0").rstrip(".")

    period = when(start) if start == end else when(start) + " — " + when(end)
    if summary is None:
        return f"{label}: за {period} нет доступного показания с достаточной свежестью. История могла не сохраниться или датчик был недоступен."
    if metric in ("point", "compare_current"):
        speech = f"{label}: на {period} было {number(summary['value'])} {unit}. Последняя запись: {when(datetime.fromisoformat(summary['recorded_at']))}."
        if metric == "compare_current":
            try:
                value = float(current.state)
                if (
                    not math.isfinite(value)
                    or current.attributes.get("unit_of_measurement") != unit
                ):
                    raise ValueError
                speech += f" Сейчас {number(value)} {unit}; разница {value - summary['value']:+.2f} {unit}."
            except AttributeError, TypeError, ValueError:
                speech += " Текущее показание недоступно для сравнения."
        return speech
    if metric == "min_max":
        value = f"минимум {number(summary['minimum'])}, максимум {number(summary['maximum'])} {unit}"
    else:
        name = {
            "minimum": "минимум",
            "maximum": "максимум",
            "average": "среднее по времени",
        }[metric]
        value = f"{name} {number(summary['value'])} {unit}"
    return f"{label}, {period}: {value}. Покрытие данными: {summary['coverage']:.0%}; пропуски не считались нулём."
