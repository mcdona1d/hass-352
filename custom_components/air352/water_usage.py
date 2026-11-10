"""Track elapsed wall-clock time since an observed humidifier refill."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping


def water_shortage_value(value: object) -> int | None:
    """Accept only the documented normal/shortage values."""
    if value in (0, "0"):
        return 0
    if value in (1, "1"):
        return 1
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass
class WaterUsageTracker:
    """Keep refill detection independent of entity reads and HA restarts."""

    last_status: int | None = None
    last_status_at: datetime | None = None
    last_refilled_at: datetime | None = None

    @classmethod
    def from_attributes(cls, attributes: Mapping, now: datetime) -> "WaterUsageTracker":
        tracker = cls(
            last_status=water_shortage_value(attributes.get("last_water_shortage")),
            last_status_at=_parse_datetime(attributes.get("last_water_status_at")),
            last_refilled_at=_parse_datetime(attributes.get("last_refilled_at")),
        )
        if tracker.last_status_at and tracker.last_status_at > now:
            tracker.last_status = None
            tracker.last_status_at = None
        if tracker.last_refilled_at and tracker.last_refilled_at > now:
            tracker.last_refilled_at = None
        return tracker

    def observe(self, prop: object, now: datetime) -> None:
        """A valid shortage-to-normal transition is evidence of a refill.

        Prefer the property's cloud timestamp (milliseconds), including on
        restart. Ignore older snapshots so they cannot create a false refill.
        Missing samples leave the last valid observation intact.
        """
        value = prop.get("value") if isinstance(prop, dict) else prop
        status = water_shortage_value(value)
        if status is None:
            return

        reported_at = now
        timestamp = prop.get("time") if isinstance(prop, dict) else None
        if timestamp is not None:
            try:
                reported_at = datetime.fromtimestamp(float(timestamp) / 1000, timezone.utc)
            except (ValueError, TypeError, OverflowError, OSError):
                return
            if reported_at > now or reported_at.timestamp() <= 0:
                return
        if self.last_status_at and reported_at <= self.last_status_at:
            return

        if self.last_status == 1 and status == 0:
            self.last_refilled_at = reported_at
        self.last_status = status
        self.last_status_at = reported_at

    def elapsed_hours(self, now: datetime) -> float | None:
        if self.last_refilled_at is None:
            return None
        return round(max(0, (now - self.last_refilled_at).total_seconds()) / 3600, 2)

    def attributes(self) -> dict:
        """Attributes are saved by HA's restore-state mechanism."""
        return {
            "last_refilled_at": self.last_refilled_at.isoformat() if self.last_refilled_at else None,
            "last_water_shortage": self.last_status,
            "last_water_status_at": self.last_status_at.isoformat() if self.last_status_at else None,
        }
