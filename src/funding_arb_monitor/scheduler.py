from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .alerts import render_scheduler_failure, send_discord_alert
from .store import Store


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    minute: int
    command: tuple[str, ...]
    hour: int | None = None


JOBS = (
    ScheduledJob("scan", 5, ("scan", "--days", "30", "--min-oi", "1000000")),
    ScheduledJob("shadow", 7, ("paper", "shadow")),
    ScheduledJob("accrue", 10, ("paper", "accrue")),
    ScheduledJob("update", 12, ("paper", "update")),
    ScheduledJob("report", 15, ("paper", "report"), hour=17),
    ScheduledJob("heartbeat", 16, ("paper", "heartbeat"), hour=17),
    ScheduledJob("backup", 20, ("maintenance", "backup"), hour=17),
)


def due_jobs(now: datetime, last_slots: dict[str, str]) -> list[ScheduledJob]:
    due: list[ScheduledJob] = []
    slot = now.strftime("%Y-%m-%dT%H:%M")
    for job in JOBS:
        if now.minute != job.minute or (job.hour is not None and now.hour != job.hour):
            continue
        if last_slots.get(job.name) == slot:
            continue
        last_slots[job.name] = slot
        due.append(job)
    return due


def execute_job(job: ScheduledJob, database_path: str, store: Store) -> int:
    command = [
        sys.executable,
        "-m",
        "funding_arb_monitor.cli",
        "--db",
        database_path,
        *job.command,
    ]
    job_run_id = store.start_scheduled_job(
        job.name, datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M")
    )
    print(f"scheduler: starting {job.name}", flush=True)
    try:
        result = subprocess.run(command, timeout=45 * 60, check=False)
        print(
            f"scheduler: finished {job.name} exit_code={result.returncode}",
            flush=True,
        )
        if result.returncode != 0:
            send_discord_alert(
                render_scheduler_failure(
                    job.name, f"process exited with code {result.returncode}"
                ),
                store=store,
                event_type=f"scheduler_{job.name}_failed",
            )
        store.finish_scheduled_job(
            job_run_id,
            exit_code=result.returncode,
            error=(
                f"process exited with code {result.returncode}"
                if result.returncode
                else None
            ),
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"scheduler: timed out {job.name}", flush=True)
        send_discord_alert(
            render_scheduler_failure(job.name, "timed out after 45 minutes"),
            store=store,
            event_type=f"scheduler_{job.name}_timed_out",
        )
        store.finish_scheduled_job(
            job_run_id, exit_code=124, error="timed out after 45 minutes"
        )
        return 124


def run_scheduler(stop: threading.Event, database_path: str) -> None:
    store = Store(database_path)
    store.initialize()
    timezone_name = os.getenv("FUNDING_ARB_TIMEZONE", "UTC")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        print(f"scheduler: unknown timezone {timezone_name}; using UTC", flush=True)
        timezone = ZoneInfo("UTC")
    last_slots: dict[str, str] = {}
    print(f"scheduler: started timezone={timezone.key}", flush=True)
    while not stop.is_set():
        for job in due_jobs(datetime.now(timezone), last_slots):
            execute_job(job, database_path, store)
        stop.wait(5)
