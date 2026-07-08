"""In-process job registry, with lightweight SQLite-backed persistence.

Because each browser tab keeps its own long-lived NiceGUI coroutine, a job
only strictly needs to live in memory for the lifetime of that coroutine.
This registry exists so a job can also be resumed - by pasting its ID - from
a different tab or after a server restart, since completed jobs are mirrored
to the local SQLite database.
"""
from __future__ import annotations

from typing import Any, Optional

from .pipeline import Job
from .records import record_from_dict, record_to_dict
from .sqlite_store import SqliteStore


class JobManager:
    """Keeps processed jobs in memory and mirrors them to the SQLite store."""

    def __init__(self, store: Optional[SqliteStore] = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._store = store

    def add(self, job: Job) -> None:
        self._jobs[job.id] = job

    async def get(self, job_id: str) -> Optional[Job]:
        job = self._jobs.get(job_id)
        if job is not None:
            return job

        if self._store is None:
            return None

        document = await self._store.get_job(job_id)
        if document is None:
            return None

        job = _job_from_dict(document)
        self._jobs[job.id] = job
        return job

    async def persist(self, job: Job) -> None:
        """Mirror a job to SQLite so it can be resumed after a restart."""
        if self._store is None:
            return
        await self._store.save_job(_job_to_dict(job))


def _job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "created_at": job.created_at,
        "backup": job.backup,
        "records": [record_to_dict(record) for record in job.records],
        "report_rows": job.report_rows,
        "summary": job.summary,
        "notes": job.notes,
    }


def _job_from_dict(document: dict[str, Any]) -> Job:
    return Job(
        id=document["id"],
        created_at=document.get("created_at", ""),
        client_id="",
        backup=document.get("backup", {}),
        records=[record_from_dict(record) for record in document.get("records", [])],
        report_rows=document.get("report_rows", []),
        summary=document.get("summary", {}),
        notes=document.get("notes", []),
    )
