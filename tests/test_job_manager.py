"""Unit tests for the in-memory job registry and its SQLite persistence."""
from __future__ import annotations

import asyncio

from src.job_manager import JobManager
from src.pipeline import Job
from src.sqlite_store import SqliteStore


def make_job(job_id: str = "job-1") -> Job:
    return Job(
        id=job_id, created_at="2026-01-01T00:00:00+00:00", client_id="secret-client-id",
        backup={"shows": [], "anime": [], "movies": []}, records=[], report_rows=[],
        summary={"media_records": 0}, notes=["hello"],
    )


def test_add_and_get_from_memory():
    manager = JobManager(store=None)
    job = make_job()
    manager.add(job)

    fetched = asyncio.run(manager.get(job.id))
    assert fetched is job


def test_get_returns_none_when_unknown_and_no_store():
    manager = JobManager(store=None)
    assert asyncio.run(manager.get("missing")) is None


def test_persist_and_resume_via_sqlite(tmp_path):
    store = SqliteStore(str(tmp_path / "test.sqlite3"))
    manager = JobManager(store=store)
    job = make_job("job-42")

    async def run():
        await manager.persist(job)
        # Simulate a restart: a fresh manager with an empty in-memory cache.
        fresh_manager = JobManager(store=store)
        return await fresh_manager.get("job-42")

    resumed = asyncio.run(run())
    assert resumed is not None
    assert resumed.id == "job-42"
    assert resumed.notes == ["hello"]
    # client_id is intentionally not persisted (short-lived UI state, not job data).
    assert resumed.client_id == ""
