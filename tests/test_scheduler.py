import subprocess
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import funding_arb_monitor.scheduler as scheduler
from funding_arb_monitor.scheduler import ScheduledJob, due_jobs, execute_job
from funding_arb_monitor.store import Store


def test_hourly_job_runs_once_per_minute() -> None:
    slots: dict[str, str] = {}
    now = datetime(2026, 7, 23, 18, 5, tzinfo=ZoneInfo("Asia/Hong_Kong"))

    assert [job.name for job in due_jobs(now, slots)] == ["scan"]
    assert due_jobs(now, slots) == []


def test_daily_report_only_runs_at_configured_hour() -> None:
    assert due_jobs(
        datetime(2026, 7, 23, 16, 15),
        {"cross-perp": "2026-07-23T16:06"},
    ) == []
    assert [
        job.name
        for job in due_jobs(
            datetime(2026, 7, 23, 17, 15),
            {"cross-perp": "2026-07-23T17:06"},
        )
    ] == ["report"]


def test_shadow_paper_job_runs_after_hourly_scan() -> None:
    assert [
        job.name
        for job in due_jobs(
            datetime(2026, 7, 23, 18, 7),
            {"cross-perp": "2026-07-23T18:06"},
        )
    ] == ["shadow"]


def test_cross_perp_runs_after_hourly_scan() -> None:
    jobs = due_jobs(datetime(2026, 7, 30, 18, 6), {})

    assert [job.name for job in jobs] == ["cross-perp"]
    assert jobs[0].command == ("cross-perp",)


def test_successful_cross_perp_from_previous_hour_gets_current_hour_catch_up() -> None:
    slots = {"cross-perp": "2026-07-30T17:06"}

    jobs = due_jobs(datetime(2026, 7, 30, 18, 8), slots)

    assert [job.name for job in jobs] == ["cross-perp"]
    assert slots["cross-perp"] == "2026-07-30T18:06"
    assert due_jobs(datetime(2026, 7, 30, 18, 9), slots) == []


def test_scan_crossing_six_and_long_cross_perp_dispatch_each_paper_job_once(
    tmp_path, monkeypatch
) -> None:
    class Clock:
        current = datetime(2026, 7, 30, 18, 5)

        @classmethod
        def now(cls, timezone=None):
            return cls.current

    class FastStop:
        def __init__(self) -> None:
            self.event = threading.Event()

        def is_set(self) -> bool:
            return self.event.is_set()

        def set(self) -> None:
            self.event.set()

        def wait(self, timeout: float) -> bool:
            return self.event.wait(0.002)

    stop = FastStop()
    release_cross_perp = threading.Event()
    cross_perp_started = threading.Event()
    cross_perp_finished = threading.Event()
    paper_started = {
        name: threading.Event() for name in ("shadow", "accrue", "update")
    }
    calls: list[str] = []

    def fake_execute(job, database_path, store):
        calls.append(job.name)
        if job.name == "scan":
            Clock.current = datetime(2026, 7, 30, 18, 6)
        elif job.name == "cross-perp":
            cross_perp_started.set()
            release_cross_perp.wait(2)
            cross_perp_finished.set()
        elif job.name in paper_started:
            paper_started[job.name].set()
        return 0

    observations: dict[str, bool] = {}

    def advance_clock() -> None:
        cross_perp_started.wait(1)
        for minute, job_name in ((7, "shadow"), (10, "accrue"), (12, "update")):
            Clock.current = datetime(2026, 7, 30, 18, minute)
            observations[job_name] = paper_started[job_name].wait(0.2)
        release_cross_perp.set()
        cross_perp_finished.wait(1)
        stop.set()

    monkeypatch.setattr(scheduler, "datetime", Clock)
    monkeypatch.setattr(scheduler, "execute_job", fake_execute)
    driver = threading.Thread(target=advance_clock)
    driver.start()

    scheduler.run_scheduler(stop, str(tmp_path / "test.db"))
    driver.join()

    assert observations == {"shadow": True, "accrue": True, "update": True}
    assert calls.count("scan") == 1
    assert calls.count("cross-perp") == 1
    assert calls.count("shadow") == 1
    assert calls.count("accrue") == 1
    assert calls.count("update") == 1


def test_scheduler_prevents_overlapping_cross_perp_runs(
    tmp_path, monkeypatch
) -> None:
    class Clock:
        current = datetime(2026, 7, 30, 18, 6)

        @classmethod
        def now(cls, timezone=None):
            return cls.current

    class FastStop:
        def __init__(self) -> None:
            self.event = threading.Event()

        def is_set(self) -> bool:
            return self.event.is_set()

        def set(self) -> None:
            self.event.set()

        def wait(self, timeout: float) -> bool:
            return self.event.wait(0.002)

    stop = FastStop()
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_execute(job, database_path, store):
        nonlocal calls, active, max_active
        with lock:
            calls += 1
            active += 1
            max_active = max(max_active, active)
            if calls == 1:
                first_started.set()
            else:
                second_started.set()
        release.wait(2)
        with lock:
            active -= 1
            if active == 0:
                finished.set()
        return 0

    overlap_observed = {"value": False}

    def advance_clock() -> None:
        first_started.wait(1)
        Clock.current = datetime(2026, 7, 30, 19, 6)
        overlap_observed["value"] = second_started.wait(0.1)
        release.set()
        finished.wait(1)
        stop.set()

    monkeypatch.setattr(scheduler, "datetime", Clock)
    monkeypatch.setattr(scheduler, "execute_job", fake_execute)
    driver = threading.Thread(target=advance_clock)
    driver.start()

    scheduler.run_scheduler(stop, str(tmp_path / "test.db"))
    driver.join()

    assert overlap_observed["value"] is False
    assert calls == 1
    assert max_active == 1


def test_daily_heartbeat_follows_report() -> None:
    assert [
        job.name
        for job in due_jobs(
            datetime(2026, 7, 23, 17, 16),
            {"cross-perp": "2026-07-23T17:06"},
        )
    ] == [
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
