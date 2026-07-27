import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from funding_arb_monitor.scheduler import ScheduledJob, due_jobs, execute_job
from funding_arb_monitor.store import Store


def test_hourly_job_runs_once_per_minute() -> None:
    slots: dict[str, str] = {}
    now = datetime(2026, 7, 23, 18, 5, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    assert [job.name for job in due_jobs(now, slots)] == ["scan"]
    assert due_jobs(now, slots) == []


def test_daily_report_only_runs_at_configured_hour() -> None:
    assert due_jobs(datetime(2026, 7, 23, 16, 15), {}) == []
    assert [job.name for job in due_jobs(datetime(2026, 7, 23, 17, 15), {})] == ["report"]


def test_shadow_paper_job_runs_after_hourly_scan() -> None:
    assert [job.name for job in due_jobs(datetime(2026, 7, 23, 18, 7), {})] == ["shadow"]


def test_daily_heartbeat_follows_report() -> None:
    assert [job.name for job in due_jobs(datetime(2026, 7, 23, 17, 16), {})] == [
        "heartbeat"
    ]


def test_failed_job_sends_discord_alert(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    alerts = []
    monkeypatch.setattr(
        "funding_arb_monitor.scheduler.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 2),
    )
    monkeypatch.setattr(
        "funding_arb_monitor.scheduler.send_discord_alert",
        lambda message, **kwargs: alerts.append((message, kwargs["event_type"])),
    )

    result = execute_job(ScheduledJob("update", 12, ("paper", "update")), "test.db", store)

    assert result == 2
    assert alerts[0][1] == "scheduler_update_failed"
    assert "process exited with code 2" in alerts[0][0]
