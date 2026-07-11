"""Parser for the optional "TV Time Out by Refract" export ZIP.

That Chrome extension can export a user's shows/movies with their TVDB and
IMDb IDs already attached, plus the user's TV Time list state for each show.
When provided alongside the TV Time GDPR export, this preserves those states
and improves the speed and accuracy of SIMKL matching.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .csv_utils import clean_title, parse_csv
from .simkl_client import clean_imdb_id, clean_numeric_id, normalize_title
from .zip_utils import read_zip_entries


@dataclass
class TvTimeOutMapping:
    source_type: str  # "show" | "movie"
    title: str
    year: int | None
    tvdb_id: str
    imdb_id: str
    watch_status: str
    source: str


@dataclass
class TvTimeOutStats:
    rows: int = 0
    mappings: int = 0
    conflicts: int = 0
    ignored: int = 0


@dataclass
class TvTimeOutResult:
    mappings: dict[str, TvTimeOutMapping] = field(default_factory=dict)
    stats: TvTimeOutStats = field(default_factory=TvTimeOutStats)


def mapping_key(source_type: str, title: Any, year: int | None) -> str:
    """Build the lookup key shared between TV Time Out mappings and media records."""
    normalized = normalize_title(title)
    if not normalized:
        return ""
    return f"{source_type}|{normalized}|{year or '' if source_type == 'movie' else ''}"


_TV_TIME_STATUS_TO_SIMKL = {
    "not_started_yet": "plantowatch",
    "continuing": "watching",
    "up_to_date": "completed",
    "stopped": "dropped",
    "watching": "watching",
    "completed": "completed",
    "hold": "hold",
    "dropped": "dropped",
    "plantowatch": "plantowatch",
}


def _normalize_watch_status(value: Any) -> str:
    status = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _TV_TIME_STATUS_TO_SIMKL.get(status, "")


def parse_tvtime_out_zip(zip_bytes: bytes) -> TvTimeOutResult:
    """Parse a TV Time Out by Refract export ZIP into a mapping table."""
    if not zip_bytes:
        return TvTimeOutResult()

    entries = read_zip_entries(zip_bytes)
    stats = TvTimeOutStats()
    rows: list[TvTimeOutMapping] = []

    for name, data in entries.items():
        lower_name = name.lower()
        text = data.decode("utf-8", errors="replace")
        if lower_name.endswith(".json"):
            _parse_json_file(name, text, rows, stats)
        elif lower_name.endswith(".csv"):
            _parse_csv_file(name, text, rows, stats)

    mappings: dict[str, TvTimeOutMapping] = {}
    conflicted_keys: set[str] = set()

    for row in rows:
        key = mapping_key(row.source_type, row.title, row.year)
        if not key:
            continue
        existing = mappings.get(key)
        if existing is None:
            mappings[key] = row
            continue

        merged, conflict = _merge_mapping(existing, row)
        if conflict:
            conflicted_keys.add(key)
            stats.conflicts += 1
            continue
        mappings[key] = merged

    for key in conflicted_keys:
        mappings.pop(key, None)

    stats.mappings = len(mappings)
    return TvTimeOutResult(mappings=mappings, stats=stats)


def _parse_json_file(file_name: str, text: str, rows: list[TvTimeOutMapping], stats: TvTimeOutStats) -> None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        stats.ignored += 1
        return
    if not isinstance(data, list):
        stats.ignored += 1
        return

    if re.search(r"movies", file_name, re.IGNORECASE):
        for item in data:
            _add_mapping(rows, stats, source_type="movie", title=item.get("title"), year=item.get("year"),
                         tvdb_id=(item.get("id") or {}).get("tvdb"), imdb_id=(item.get("id") or {}).get("imdb"),
                         watch_status=None, source=file_name)
        return

    if re.search(r"series", file_name, re.IGNORECASE):
        for item in data:
            _add_mapping(rows, stats, source_type="show", title=item.get("title"), year=None,
                         tvdb_id=(item.get("id") or {}).get("tvdb"), imdb_id=(item.get("id") or {}).get("imdb"),
                         watch_status=item.get("status"), source=file_name)


def _parse_csv_file(file_name: str, text: str, rows: list[TvTimeOutMapping], stats: TvTimeOutStats) -> None:
    try:
        parsed = parse_csv(text)
    except Exception:  # noqa: BLE001 - defensive: never let a bad optional file break the run
        stats.ignored += 1
        return

    if re.search(r"movies", file_name, re.IGNORECASE):
        for row in parsed.rows:
            _add_mapping(rows, stats, source_type="movie", title=row.get("title"), year=row.get("year"),
                         tvdb_id=row.get("tvdb_id"), imdb_id=row.get("imdb_id"), watch_status=None,
                         source=file_name)
        return

    if re.search(r"series", file_name, re.IGNORECASE) and not re.search(r"episodes", file_name, re.IGNORECASE):
        for row in parsed.rows:
            _add_mapping(rows, stats, source_type="show", title=row.get("title"), year=None,
                         tvdb_id=row.get("tvdb_id"), imdb_id=row.get("imdb_id"),
                         watch_status=row.get("status"), source=file_name)


def _add_mapping(
    rows: list[TvTimeOutMapping], stats: TvTimeOutStats, *, source_type: str, title: Any, year: Any,
    tvdb_id: Any, imdb_id: Any, watch_status: Any, source: str,
) -> None:
    clean = clean_title(title)
    tvdb = clean_numeric_id(tvdb_id)
    imdb = clean_imdb_id(imdb_id)
    status = _normalize_watch_status(watch_status)
    if not clean or (not tvdb and not imdb and not status):
        stats.ignored += 1
        return

    try:
        parsed_year = int(str(year)) if year else None
    except (TypeError, ValueError):
        parsed_year = None

    rows.append(TvTimeOutMapping(
        source_type=source_type, title=clean, year=parsed_year, tvdb_id=tvdb, imdb_id=imdb,
        watch_status=status, source=source,
    ))
    stats.rows += 1


def _merge_mapping(left: TvTimeOutMapping, right: TvTimeOutMapping) -> tuple[TvTimeOutMapping, bool]:
    if left.tvdb_id and right.tvdb_id and left.tvdb_id != right.tvdb_id:
        return left, True
    if left.imdb_id and right.imdb_id and left.imdb_id.lower() != right.imdb_id.lower():
        return left, True

    return TvTimeOutMapping(
        source_type=left.source_type,
        title=left.title,
        year=left.year,
        tvdb_id=left.tvdb_id or right.tvdb_id,
        imdb_id=left.imdb_id or right.imdb_id,
        watch_status=(
            left.watch_status or right.watch_status
            if not left.watch_status or not right.watch_status or left.watch_status == right.watch_status
            else ""
        ),
        source=", ".join(filter(None, [left.source, right.source])),
    ), False


def apply_tvtime_out_mappings(records: list[Any], mappings: dict[str, TvTimeOutMapping]) -> int:
    """Apply external IDs and show-list states from TV Time Out data."""
    if not mappings:
        return 0

    applied = 0
    for record in records:
        key = mapping_key("movie" if record.source_type == "movie" else "show", record.title, record.year)
        mapping = mappings.get(key)
        if not mapping:
            continue

        changed = False
        if mapping.imdb_id and not record.input_imdb_id:
            record.input_imdb_id = mapping.imdb_id
            record.initial_imdb_id = mapping.imdb_id
            changed = True
        if mapping.tvdb_id and not record.input_tvdb_id:
            record.input_tvdb_id = mapping.tvdb_id
            record.initial_tvdb_id = mapping.tvdb_id
            changed = True
        if mapping.watch_status and record.watch_status != mapping.watch_status:
            record.watch_status = mapping.watch_status
            changed = True
        if changed:
            record.lookup_source = record.lookup_source or "tv_time_out"
            applied += 1

    return applied
