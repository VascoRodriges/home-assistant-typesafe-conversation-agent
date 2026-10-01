"""Synthetic Recorder/history regressions: no cloud or real entities."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.typesafe_conversation.household_history import (
    HistoryQueries,
    answer_history,
    interval,
    read_recorder,
    render_history,
    summarize,
)
from custom_components.typesafe_conversation.household_policy import PolicyError
from tests.test_household import runtime as make_runtime

NOW = datetime(2026, 10, 1, 23, 30, tzinfo=ZoneInfo("Europe/Moscow"))
SENSOR = "sensor.sample_temperature"


@pytest.fixture
def runtime():
    return make_runtime.__wrapped__()


def decision(criteria, value, probability=0.99):
    return {
        "type": "choice",
        "choice": value,
        "confidence": 0.4,
        "probabilities": {
            key: probability
            if key == value
            else (1 - probability) / (len(criteria) - 1)
            for key in criteria
        },
    }


def query(metric="point", period="yesterday_now", **updates):
    return {
        "entity_id": SENSOR,
        "metric": metric,
        "period": period,
        "start_local": None,
        "end_local": None,
        **updates,
    }


def row(stamp, value, unit="°C"):
    return {"time": stamp, "state": value, "unit": unit}


def historical(runtime, metric="point", period="yesterday_now"):
    runtime.config["history"] = {"enabled": True, "max_days": 7, "max_age_minutes": 120}
    runtime.history_router = HistoryQueries(runtime.config)
    runtime.hass.config = SimpleNamespace(time_zone="Europe/Moscow")
    values = {
        "reading_mode": "historical_only",
        "history_entity": SENSOR,
        "history_metric": metric,
        "history_period": period,
    }
    runtime.fast_answers = {
        key: decision(runtime.history_router.questions[key]["criteria"], value)
        for key, value in values.items()
    }


def test_yesterday_uses_local_calendar_and_clock():
    start, end = interval(query(), NOW, {})
    assert start == end == datetime(2026, 9, 30, 20, 30, tzinfo=UTC)
    start, end = interval(query("average", "yesterday"), NOW, {})
    assert start == datetime(2026, 9, 29, 21, tzinfo=UTC)
    assert end == datetime(2026, 9, 30, 21, tzinfo=UTC)


@pytest.mark.parametrize("hour,date", [(23, 1), (3, 30)])
def test_last_completed_night(hour, date):
    start, end = interval(query("minimum", "last_night"), NOW.replace(hour=hour), {})
    assert end.astimezone(NOW.tzinfo).day == date
    assert (end - start).total_seconds() == 8 * 3600


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-10-01T23:31:00+03:00", "2026-10-01T23:31:00+03:00"),
        ("2026-09-01T10:00:00+03:00", "2026-09-01T10:00:00+03:00"),
        ("2026-09-30T10:00:00", "2026-09-30T10:00:00"),
        ("2026-09-30T10:00:00+03:00", "2026-09-30T09:00:00+03:00"),
    ],
)
def test_invalid_future_old_naive_or_reversed_time_rejected(start, end):
    with pytest.raises(PolicyError):
        interval(query(period="custom", start_local=start, end_local=end), NOW, {})


def test_point_never_uses_future_sample_or_skips_unavailable():
    point = NOW - timedelta(days=1)
    records = [
        row(point - timedelta(minutes=1), "21.5"),
        row(point + timedelta(seconds=1), "99"),
    ]
    assert (
        summarize(records, point, point, "point", timedelta(hours=2))["value"] == 21.5
    )
    records.append(row(point - timedelta(seconds=30), "unavailable"))
    assert summarize(records, point, point, "point", timedelta(hours=2)) is None


def test_stale_point_and_nonfinite_values_are_not_measurements():
    point = NOW - timedelta(days=1)
    for records in ([row(point - timedelta(hours=3), "21")], [row(point, "NaN")], []):
        assert summarize(records, point, point, "point", timedelta(hours=2)) is None


def test_average_is_duration_weighted_not_sample_mean():
    start = NOW - timedelta(hours=1)
    result = summarize(
        [row(start, "10"), row(start + timedelta(minutes=15), "30")],
        start,
        NOW,
        "average",
        timedelta(hours=2),
    )
    assert result["value"] == 25
    assert result["minimum"] == 10 and result["maximum"] == 30
    assert result["coverage"] == 1


def test_gap_coverage_is_explicit_and_not_zero():
    start = NOW - timedelta(hours=1)
    result = summarize(
        [
            row(start, "20"),
            row(start + timedelta(minutes=15), "unknown"),
            row(start + timedelta(minutes=45), "30"),
        ],
        start,
        NOW,
        "average",
        timedelta(hours=2),
    )
    assert result["value"] == 25 and result["coverage"] == 0.5
    assert "50%" in render_history(
        "Комната", "°C", result, start, NOW, "average", NOW.tzinfo
    )


@pytest.mark.asyncio
async def test_one_batch_history_is_non_actuating_and_not_billed_again(runtime):
    historical(runtime)
    runtime.api = AsyncMock()

    async def records(_hass, entity, start, _end, _age):
        assert entity == SENSOR
        return [row(start - timedelta(minutes=1), "22.4")]

    with patch(
        "custom_components.typesafe_conversation.household_history.read_recorder",
        side_effect=records,
    ):
        result = await answer_history(runtime, "там вчера?", [], {}, None)
    assert "22.4 °C" in result and "Последняя запись" in result
    runtime.api.assert_not_awaited()
    runtime.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_records_returns_readable_answer(runtime):
    historical(runtime)
    with patch(
        "custom_components.typesafe_conversation.household_history.read_recorder",
        new=AsyncMock(return_value=[]),
    ):
        result = await answer_history(runtime, "вчера", [], {}, None)
    assert "нет доступного показания" in result
    assert "26.8" not in result


@pytest.mark.asyncio
async def test_permission_is_checked_before_recorder(runtime):
    historical(runtime)
    runtime.hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=None))
    with patch(
        "custom_components.typesafe_conversation.household_history.read_recorder",
        new=AsyncMock(),
    ) as read:
        with pytest.raises(PolicyError, match="permission"):
            await answer_history(
                runtime, "вчера", [], {}, SimpleNamespace(user_id="sample-user")
            )
        read.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_history_does_not_read_or_call_planner(runtime):
    runtime.api = AsyncMock()
    assert "отключено" in await answer_history(runtime, "вчера", [], {}, None)
    runtime.api.assert_not_awaited()


@pytest.mark.asyncio
async def test_planner_custom_date_has_typed_review_and_no_measurements(runtime):
    historical(runtime, period="custom")
    data = query(
        period="custom",
        start_local=(NOW - timedelta(days=1)).isoformat(),
        end_local=(NOW - timedelta(days=1)).isoformat(),
        clarification=None,
    )
    runtime.api = AsyncMock(
        return_value={
            "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps(data)}}
            ]
        }
    )
    questions, expected = runtime.history_router.review(data)
    answers = {
        key: decision(questions[key]["criteria"], value)
        for key, value in expected.items()
    }
    runtime.client = SimpleNamespace(
        async_ask=AsyncMock(return_value=SimpleNamespace(raw={"answers": answers}))
    )
    with patch(
        "custom_components.typesafe_conversation.household_history.read_recorder",
        new=AsyncMock(return_value=[]),
    ):
        result = await answer_history(runtime, "вчера в 23:30", [], {}, None)
    assert "нет доступного показания" in result
    runtime.api.assert_awaited_once()
    runtime.client.async_ask.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_history_never_enters_device_planner_or_executor(runtime):
    historical(runtime)

    async def classify(_text, _history, request):
        return "home_query", {"sensors"}, {}

    runtime.classify = AsyncMock(side_effect=classify)
    with patch(
        "custom_components.typesafe_conversation.household_history.read_recorder",
        new=AsyncMock(return_value=[]),
    ):
        result = await runtime.process("вчера?", context=None)
    assert not result["error"] and not result["executed"]
    assert result["diagnostics"]["path"] == "history_jev_only"
    runtime.plan.assert_not_awaited()
    runtime.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_history_request_is_not_partially_executed(runtime):
    historical(runtime)
    runtime.fast_answers["reading_mode"] = decision(
        runtime.history_router.questions["reading_mode"]["criteria"], "historical_mixed"
    )
    runtime.classify = AsyncMock(return_value=("mixed", {"sensors", "light"}, {}))
    result = await runtime.process("прочитай историю и включи свет", context=None)
    assert not result["executed"] and "Разделите" in result["speech"]
    runtime.plan.assert_not_awaited()
    runtime.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_recorder_worker_preserves_actual_timestamps_and_units():
    stamp = NOW - timedelta(days=1)
    hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
    state = SimpleNamespace(
        state="22.4", last_updated=stamp, attributes={"unit_of_measurement": "°C"}
    )
    worker = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=lambda work: work())
    )
    with (
        patch(
            "homeassistant.components.recorder.history.get_significant_states",
            return_value={SENSOR: [state]},
        ) as get_states,
        patch("homeassistant.helpers.recorder.get_instance", return_value=worker),
    ):
        result = await read_recorder(hass, SENSOR, stamp, stamp, timedelta(hours=2))
    assert result == [row(stamp, "22.4")]
    assert get_states.call_args.kwargs["include_start_time_state"] is False
    assert get_states.call_args.args[3] == [SENSOR]
    worker.async_add_executor_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_changed_units_are_not_combined(runtime):
    historical(runtime, "average", "yesterday")
    stamp = NOW - timedelta(days=1)
    records = [row(stamp, "22", "°C"), row(stamp + timedelta(minutes=1), "72", "°F")]
    with patch(
        "custom_components.typesafe_conversation.household_history.read_recorder",
        new=AsyncMock(return_value=records),
    ):
        result = await answer_history(runtime, "средняя вчера", [], {}, None)
    assert "Единица измерения" in result
