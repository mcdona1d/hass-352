"""Refill timing across repeated polls, missing data and saved-state recovery."""

from datetime import datetime, timedelta, timezone
import importlib
import json

import pytest

from scripts._air352_loader import load_api

api = load_api()
water_usage = importlib.import_module(f"{api.__package__}.water_usage")
WaterUsageTracker = water_usage.WaterUsageTracker
START = datetime(2026, 11, 8, tzinfo=timezone.utc)


def sample(value, at):
    return {"value": value, "time": int(at.timestamp() * 1000)}


def test_initial_normal_or_shortage_does_not_invent_a_refill():
    for value in (0, 1):
        tracker = WaterUsageTracker()
        tracker.observe(sample(value, START), START)
        assert tracker.elapsed_hours(START + timedelta(hours=8)) is None


def test_refill_starts_timer_and_repeated_normal_polls_do_not_reset_it():
    tracker = WaterUsageTracker()
    tracker.observe(sample(1, START), START)
    refill = START + timedelta(minutes=5)
    tracker.observe(sample(0, refill), refill + timedelta(seconds=10))
    later = refill + timedelta(hours=2, minutes=30)
    tracker.observe(sample(0, later), later)
    assert tracker.last_refilled_at == refill
    assert tracker.elapsed_hours(later) == 2.5
    # A further shortage does not reset elapsed wall-clock time.
    shortage = later + timedelta(seconds=1)
    tracker.observe(sample(1, shortage), shortage)
    assert tracker.last_status == 1
    assert tracker.elapsed_hours(later + timedelta(hours=1)) == 3.5


def test_next_refill_restarts_timer():
    tracker = WaterUsageTracker()
    for hours, status in ((0, 1), (1, 0), (5, 1), (6, 0)):
        at = START + timedelta(hours=hours)
        tracker.observe(sample(status, at), at)
    assert tracker.elapsed_hours(START + timedelta(hours=7)) == 1


def test_json_restore_preserves_refill_and_last_status_across_restart():
    tracker = WaterUsageTracker()
    tracker.observe(sample(1, START), START)
    refill = START + timedelta(minutes=10)
    tracker.observe(sample(0, refill), refill)
    restart = refill + timedelta(hours=4)
    restored = WaterUsageTracker.from_attributes(json.loads(json.dumps(tracker.attributes())), restart)
    restored.observe(sample(0, refill), restart)
    assert restored.elapsed_hours(restart) == 4
    assert restored.last_refilled_at == refill
    # A shortage followed by a refill during a restart gap uses cloud report time.
    restored.observe(sample(1, restart), restart)
    resumed = WaterUsageTracker.from_attributes(restored.attributes(), restart + timedelta(hours=2))
    next_refill = restart + timedelta(hours=1)
    resumed.observe(sample(0, next_refill), restart + timedelta(hours=2))
    assert resumed.last_refilled_at == next_refill
    assert resumed.elapsed_hours(restart + timedelta(hours=2)) == 1


@pytest.mark.parametrize("prop", [None, {}, {"value": None}, {"value": 2},
                                  {"value": 65535}, {"value": "bad"},
                                  {"value": 0, "time": "bad"},
                                  {"value": 0, "time": -1},
                                  {"value": 0, "time": float("nan")}])
def test_invalid_samples_do_not_clear_shortage_or_trigger_refill(prop):
    tracker = WaterUsageTracker()
    tracker.observe(sample(1, START), START)
    tracker.observe(prop, START + timedelta(minutes=1))
    assert tracker.last_status == 1
    assert tracker.last_refilled_at is None
    # Keep the valid shortage observation through a temporary unknown period.
    refill = START + timedelta(minutes=2)
    tracker.observe(sample(0, refill), refill)
    assert tracker.last_refilled_at == refill


def test_stale_same_time_and_future_snapshots_do_not_invent_refills():
    tracker = WaterUsageTracker()
    tracker.observe(sample(1, START), START)
    now = START + timedelta(minutes=5)
    for at in (START - timedelta(minutes=1), START, now + timedelta(minutes=1)):
        tracker.observe(sample(0, at), now)
        assert tracker.last_refilled_at is None
        assert tracker.last_status == 1


def test_missing_timestamp_uses_detection_time_and_supports_bool_and_string_values():
    for shortage, normal in ((True, False), ("1", "0"), (1, 0)):
        tracker = WaterUsageTracker()
        tracker.observe(shortage, START)
        refill = START + timedelta(minutes=1)
        tracker.observe({"value": normal}, refill)
        assert tracker.last_refilled_at == refill
        assert tracker.elapsed_hours(refill + timedelta(seconds=90)) == 0.03


@pytest.mark.parametrize("attributes", [
    {},
    {"last_refilled_at": "broken", "last_water_shortage": "unknown"},
    {"last_refilled_at": "2026-11-08T00:00:00"},  # no timezone
    {"last_refilled_at": (START + timedelta(days=1)).isoformat()},
    {"last_water_shortage": 1, "last_water_status_at": (START + timedelta(days=1)).isoformat()},
])
def test_bad_or_future_saved_values_do_not_create_elapsed_time(attributes):
    tracker = WaterUsageTracker.from_attributes(attributes, START)
    tracker.observe(sample(0, START), START)
    assert tracker.elapsed_hours(START) is None
