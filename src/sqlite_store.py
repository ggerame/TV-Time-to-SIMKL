"""Lightweight SQLite-backed cache for confirmed SIMKL ID mappings and jobs.

This is a simple, zero-setup replacement for a full database server: it's a
single local file, created automatically on first use. It stores two kinds
of data, each as a JSON document keyed by ID:

- Confirmed SIMKL/IMDb/TVDB ID mappings, reused across future conversions so
  fewer records need manual fixing each time.
- Completed jobs, so a job can be resumed by ID after a server restart.

All access goes through :mod:`sqlite3` (Python's standard library), run in a
worker thread via ``asyncio.to_thread`` so it never blocks the NiceGUI event
loop.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .records import TYPE_VERIFIER_VERSION, MediaRecord
from .simkl_client import normalize_title


class SqliteStore:
    """Local SQLite-backed store for ID mappings and jobs."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS id_mappings ("
                "id TEXT PRIMARY KEY, normalized_title TEXT, document TEXT NOT NULL)",
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_id_mappings_normalized_title ON id_mappings(normalized_title)",
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, document TEXT NOT NULL)",
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    # -- ID mapping cache ----------------------------------------------------

    async def get_mappings(self, records: list[MediaRecord]) -> dict[str, dict[str, Any]]:
        """Fetch cached mappings for the given records, keyed by record id."""
        return await asyncio.to_thread(self._get_mappings_sync, [record.id for record in records])

    def _get_mappings_sync(self, record_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not record_ids:
            return {}
        placeholders = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT id, document FROM id_mappings WHERE id IN ({placeholders})", record_ids,
            ).fetchall()
        return {row[0]: json.loads(row[1]) for row in rows}

    async def save_mappings(self, records: list[MediaRecord]) -> dict[str, int]:
        """Upsert confirmed mappings for records that have a verified SIMKL ID."""
        return await asyncio.to_thread(self._save_mappings_sync, records)

    def _save_mappings_sync(self, records: list[MediaRecord]) -> dict[str, int]:
        now = datetime.now(timezone.utc).isoformat()
        saved = 0

        with self._connect() as connection:
            for record in records:
                if record.status != "found" or not record.verified_simkl_id:
                    continue

                document = {
                    "sourceType": record.source_type,
                    "title": record.title,
                    "normalizedTitle": normalize_title(record.title),
                    "year": record.year,
                    "ids": {
                        "imdb": record.verified_imdb_id or "",
                        "tvdb": int(record.verified_tvdb_id) if record.verified_tvdb_id else None,
                    },
                    "simkl": {
                        "id": int(record.verified_simkl_id),
                        "type": record.simkl_type,
                        "title": record.simkl_title,
                        "year": record.simkl_year,
                        "typeVerified": record.type_verified,
                        "typeVerifiedBy": record.type_verified_by,
                        "typeVerifierVersion": TYPE_VERIFIER_VERSION,
                    },
                    "verifiedBy": record.lookup_source,
                    "verifiedAt": now,
                    "updatedAt": now,
                }
                connection.execute(
                    "INSERT INTO id_mappings (id, normalized_title, document) VALUES (?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET normalized_title = excluded.normalized_title, document = excluded.document",
                    (record.id, document["normalizedTitle"], json.dumps(document)),
                )
                saved += 1
            connection.commit()

        return {"saved": saved}

    # -- Job persistence ------------------------------------------------------

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_job_sync, job_id)

    def _get_job_sync(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT document FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    async def save_job(self, job_document: dict[str, Any]) -> None:
        await asyncio.to_thread(self._save_job_sync, job_document)

    def _save_job_sync(self, job_document: dict[str, Any]) -> None:
        job_id = job_document["id"]
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs (id, document) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET document = excluded.document",
                (job_id, json.dumps(job_document)),
            )
            connection.commit()
