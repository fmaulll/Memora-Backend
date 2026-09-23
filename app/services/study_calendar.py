"""The calendar boundary shared by projections and historical attribution."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


def as_utc(value: datetime) -> datetime:
    # SQLite strips timezone information; persisted backend timestamps are UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def local_date(value: datetime, timezone_name: str) -> date:
    return as_utc(value).astimezone(ZoneInfo(timezone_name)).date()


def local_today(timezone_name: str) -> date:
    return local_date(datetime.now(timezone.utc), timezone_name)
