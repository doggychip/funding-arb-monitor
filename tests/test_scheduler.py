import subprocess
import time
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


def test_cross_perp_runs_after_hourly_scan() -> None:
    jobs = due_jobs(datetime(2026, 7, 30, 18, 6), {})

    assert [job.name for job in jobs] == ["cross-perp"]
    assert jobs[0].command == ("cross-perp",)


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
    runs = store.scheduled_job_runs()
    assert len(runs) == 1
    assert runs[0]["name"] == "update"
    assert runs[0]["status"] == "failed"
    assert runs[0]["exit_code"] == 2
    assert runs[0]["completed_at_ms"] >= runs[0]["started_at_ms"]


def test_scheduler_health_flags_an_overdue_hourly_job(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    run_id = store.start_scheduled_job("update", "2026-07-28T09:12")
    store.finish_scheduled_job(run_id, exit_code=0)
    now_ms = int(time.time() * 1000)
    with store.connect() as connection:
        connection.execute(
            "UPDATE scheduled_job_runs SET completed_at_ms = ? WHERE id = ?",
            (now_ms - 3 * 3_600_000, run_id),
        )

    health = store.scheduler_health(now_ms=now_ms)

    assert health["healthy"] is False
    assert health["unhealthy_jobs"] == ["update:overdue"]


def test_failed_cross_perp_job_is_visible_but_not_critical(tmp_path) -> None:
    store = Store(tmp_path / "test.db")
    store.initialize()
    run_id = store.start_scheduled_job("cross-perp", "2026-07-30T18:06")
    store.finish_scheduled_job(run_id, exit_code=2, error="venue outage")

    health = store.scheduler_health()

    latest = next(row for row in health["latest_jobs"] if row["name"] == "cross-perp")
    assert latest["status"] == "failed"
    assert health["unhealthy_jobs"] == []
