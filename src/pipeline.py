"""End-to-end orchestration: TV Time ZIP -> reviewed job -> SIMKL backup ZIP.

The NiceGUI UI runs each user's workflow as a single async coroutine, so
there is no separate REST "job" endpoint to poll here: progress is reported
directly to the UI via a callback as the pipeline runs.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Config
from .converter import ConversionOptions, convert_tvtime_to_simkl_json, load_tvtime_data
from .imdb_client import ImdbClient
from .records import (
    MediaRecord,
    ProgressCallback,
    apply_stored_mappings,
    enrich_records,
    extract_media_records,
    mark_not_found,
)
from .simkl_client import SimklClient
from .sqlite_store import SqliteStore
from .tv_time_out import apply_tvtime_out_mappings, parse_tvtime_out_zip
from .tvmaze_client import TvMazeClient


def _noop_progress(_phase: str, _done: int, _total: int) -> None:
    return None


@dataclass
class Job:
    """Everything the review UI needs for one processed TV Time export."""

    id: str
    created_at: str
    client_id: str
    backup: dict[str, Any]
    records: list[MediaRecord]
    report_rows: list[dict[str, Any]]
    summary: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def records_by_id(self) -> dict[str, MediaRecord]:
        return {record.id: record for record in self.records}


async def create_job(
    *,
    tvtime_zip_bytes: bytes,
    tvtime_out_zip_bytes: Optional[bytes],
    client_id: str,
    include_plan_to_watch: bool,
    include_rewatches: bool,
    config: Config,
    store: Optional[SqliteStore],
    progress: ProgressCallback = _noop_progress,
) -> Job:
    """Run the full pipeline: parse -> convert -> prefill IDs -> enrich via SIMKL."""
    progress("reading TV Time export", 0, 1)
    loaded = load_tvtime_data(tvtime_zip_bytes)

    progress("converting TV Time history", 0, 1)
    conversion = convert_tvtime_to_simkl_json(
        loaded,
        ConversionOptions(include_plan_to_watch=include_plan_to_watch, include_rewatches=include_rewatches),
        progress=progress,
    )

    records = extract_media_records(conversion.simkl_backup)
    notes = list(conversion.notes)

    if tvtime_out_zip_bytes:
        try:
            tv_time_out = parse_tvtime_out_zip(tvtime_out_zip_bytes)
            applied = apply_tvtime_out_mappings(records, tv_time_out.mappings)
            notes.append(f"{applied} records were prefilled with IMDb/TVDB IDs from TV Time Out by Refract.")
            if tv_time_out.stats.conflicts:
                notes.append(f"{tv_time_out.stats.conflicts} TV Time Out ID mappings were ignored because they had conflicting IDs.")
        except Exception as exc:  # noqa: BLE001 - optional input, never fail the whole run because of it
            notes.append(f"TV Time Out ZIP ignored: {exc}")

    if store is not None:
        try:
            mapping_docs = await store.get_mappings(records)
            cached, needs_type = apply_stored_mappings(records, mapping_docs)
            if cached:
                notes.append(f"{cached} SIMKL IDs were loaded from the local database cache.")
            if needs_type:
                notes.append(f"{needs_type} cached SIMKL IDs need type confirmation from SIMKL.")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Local database cache ignored: {exc}")

    if client_id:
        async with SimklClient(
            client_id, min_delay_ms=config.simkl_api_delay_ms, timeout_ms=config.simkl_api_timeout_ms,
        ) as client, ImdbClient() as imdb_client, TvMazeClient() as tvmaze_client:
            await enrich_records(records, client, imdb_client, tvmaze_client, progress)
    else:
        for record in records:
            if record.status != "found" or not record.verified_simkl_id:
                mark_not_found(record, "simkl_client_id_missing")

    if store is not None:
        try:
            await store.save_mappings(records)
        except Exception:  # noqa: BLE001 - caching failures must never break the job
            pass

    summary = {
        **conversion.summary,
        "media_records": len(records),
        "simkl_found": len([r for r in records if r.status == "found"]),
        "simkl_not_found": len([r for r in records if r.status != "found"]),
    }

    return Job(
        id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        client_id=client_id,
        backup=conversion.simkl_backup,
        records=records,
        report_rows=conversion.report_rows,
        summary=summary,
        notes=notes,
    )
