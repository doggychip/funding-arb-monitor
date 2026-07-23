from datetime import datetime
from zoneinfo import ZoneInfo

from funding_arb_monitor.scheduler import due_jobs


def test_hourly_job_runs_once_per_minute() -> None:
    slots: dict[str, str] = {}
    now = datetime(2026, 7, 23, 18, 5, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    assert [job.name for job in due_jobs(now, slots)] == ["scan"]
    assert due_jobs(now, slots) == []


def test_daily_report_only_runs_at_configured_hour() -> None:
    assert due_jobs(datetime(2026, 7, 23, 16, 15), {}) == []
    assert [job.name for job in due_jobs(datetime(2026, 7, 23, 17, 15), {})] == ["report"]
