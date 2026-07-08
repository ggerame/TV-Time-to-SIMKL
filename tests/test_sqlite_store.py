"""Unit tests for the local SQLite-backed ID mapping and job store."""
from __future__ import annotations

import asyncio

from src.records import extract_media_records
from src.simkl_client import LookupResult
from src.records import apply_lookup_result
from src.sqlite_store import SqliteStore


def make_store(tmp_path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "sub" / "test.sqlite3"))


def test_creates_database_file_and_parent_dir(tmp_path):
    db_path = tmp_path / "sub" / "test.sqlite3"
    assert not db_path.exists()
    SqliteStore(str(db_path))
    assert db_path.exists()


def test_save_and_get_mappings_round_trip(tmp_path):
    store = make_store(tmp_path)
    backup = {
        "shows": [{"status": "watching", "watched_episodes_count": 1, "is_rewatch": False, "show": {"title": "Breaking Bad"}}],
        "anime": [], "movies": [],
    }
    records = extract_media_records(backup)
    record = records[0]
    apply_lookup_result(record, LookupResult(status="found", simkl_id=123, simkl_type="tv", title="Breaking Bad", confidence=95, type_verified=True))

    async def run():
        save_result = await store.save_mappings(records)
        assert save_result["saved"] == 1
        return await store.get_mappings(records)

    mappings = asyncio.run(run())
    assert mappings[record.id]["simkl"]["id"] == 123
    assert mappings[record.id]["simkl"]["typeVerified"] is True


def test_get_mappings_returns_empty_for_unknown_records(tmp_path):
    store = make_store(tmp_path)

    async def run():
        return await store.get_mappings([])

    assert asyncio.run(run()) == {}


def test_save_and_get_job_round_trip(tmp_path):
    store = make_store(tmp_path)
    job_document = {"id": "job-123", "created_at": "now", "backup": {"shows": []}, "records": [], "report_rows": [], "summary": {}, "notes": []}

    async def run():
        await store.save_job(job_document)
        return await store.get_job("job-123")

    fetched = asyncio.run(run())
    assert fetched == job_document


def test_get_job_returns_none_when_missing(tmp_path):
    store = make_store(tmp_path)

    async def run():
        return await store.get_job("does-not-exist")

    assert asyncio.run(run()) is None
