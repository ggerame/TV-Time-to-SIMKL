"""Media record extraction, SIMKL enrichment, manual validation and export.

A "media record" groups every TV Time entry that refers to the same show or
movie (a watch entry, plan-to-watch entry and any rewatches all collapse
into a single record) so the review UI only shows one row per title, and so
a single SIMKL ID can be applied consistently everywhere that title appears
in the final backup.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .imdb_client import ImdbCandidate, ImdbClient, find_best_match
from .tvmaze_client import TvMazeClient
from .simkl_client import (
    LookupResult,
    SimklClient,
    clean_imdb_id,
    clean_numeric_id,
    normalize_simkl_type,
    normalize_title,
    query_types_for_record,
)
from .zip_utils import create_simkl_backup_zip

ProgressCallback = Callable[[str, int, int], None]


def _noop_progress(_phase: str, _done: int, _total: int) -> None:
    return None


#: Maps a backup list name to the default SIMKL "type" used by records built from it.
LIST_NAME_TO_TYPE = {"shows": "tv", "anime": "anime", "movies": "movie"}
#: Reverse mapping: SIMKL "type" -> the backup list an entry belongs to for export.
TYPE_TO_LIST_NAME = {"tv": "shows", "anime": "anime", "movie": "movies"}
#: Watch-list states accepted by SIMKL's JSON import format.
WATCH_STATUSES = ("watching", "completed", "hold", "dropped", "plantowatch")


def normalize_watch_status(value: Any, fallback: str = "") -> str:
    """Return a supported SIMKL watch-list state without inventing one."""
    status = str(value or "").strip().lower().replace(" ", "")
    aliases = {"onhold": "hold", "paused": "hold", "planned": "plantowatch", "towatch": "plantowatch"}
    status = aliases.get(status, status)
    return status if status in WATCH_STATUSES else fallback


@dataclass
class MediaRecord:
    """One reviewable row: a unique (type, title, year) grouping."""

    id: str
    source_type: str  # "show" | "movie" | "anime" - as originally classified by the converter
    title: str
    year: Optional[int]
    refs: list[tuple[str, int]] = field(default_factory=list)

    occurrences: int = 0
    watched_episodes: int = 0
    rewatch_entries: int = 0

    # User-provided / prefilled IDs (editable in the review table).
    input_simkl_id: str = ""
    input_imdb_id: str = ""
    input_tvdb_id: str = ""

    # IDs as they were before the user made any manual edits (used to detect changes).
    initial_simkl_id: str = ""
    initial_imdb_id: str = ""
    initial_tvdb_id: str = ""

    # IDs confirmed by a successful SIMKL lookup.
    verified_simkl_id: str = ""
    verified_imdb_id: str = ""
    verified_tvdb_id: str = ""

    simkl_type: str = "tv"
    watch_status: str = ""
    simkl_title: str = ""
    simkl_year: Optional[int] = None

    confidence: Optional[int] = None
    lookup_source: str = ""
    type_verified: bool = False
    type_verified_by: str = ""
    status: str = "not_found"
    reason: str = "not_checked"
    error: str = ""
    field_errors: dict[str, str] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    #: When True, this title is dropped from the export entirely (removed by the user in the review table).
    excluded: bool = False

    def visual_status(self) -> str:
        """Traffic-light status used for row coloring in the review table.

        - ``found``: a SIMKL ID is verified and matches what's currently entered.
        - ``pending``: the user changed an ID/type since the last validation.
        - ``not_found``: no verified match, or validation failed.
        """
        if self.field_errors:
            return "not_found"
        if not (self.input_simkl_id or self.input_imdb_id or self.input_tvdb_id):
            return "not_found"
        changed = (
            self.input_simkl_id != self.initial_simkl_id
            or self.input_imdb_id != self.initial_imdb_id
            or self.input_tvdb_id != self.initial_tvdb_id
        )
        if self.status == "found" and not changed:
            return "found"
        if changed:
            return "pending"
        return "not_found"


def record_to_dict(record: MediaRecord) -> dict[str, Any]:
    """Serialize a record to a plain, JSON-friendly dictionary (for storage/export)."""
    payload = record.__dict__.copy()
    payload["refs"] = [list(ref) for ref in record.refs]
    return payload


def record_from_dict(data: dict[str, Any]) -> MediaRecord:
    """Rebuild a :class:`MediaRecord` previously serialized with :func:`record_to_dict`."""
    payload = dict(data)
    payload["refs"] = [tuple(ref) for ref in payload.get("refs", [])]
    valid_fields = {f.name for f in MediaRecord.__dataclass_fields__.values()}
    payload = {key: value for key, value in payload.items() if key in valid_fields}
    return MediaRecord(**payload)


TYPE_VERIFIER_VERSION = 1


def apply_stored_mappings(records: list[MediaRecord], mapping_docs: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """Prefill records from previously confirmed local database mappings.

    :return: ``(cached_count, needs_type_confirmation_count)``
    """
    cached = 0
    needs_type = 0

    for record in records:
        mapping = mapping_docs.get(record.id)
        if not mapping:
            continue
        simkl = mapping.get("simkl") or {}
        simkl_id = simkl.get("id")
        if not simkl_id:
            continue

        ids = mapping.get("ids") or {}
        if ids.get("imdb"):
            record.input_imdb_id = str(ids["imdb"]).lower()
            record.initial_imdb_id = record.input_imdb_id
        if ids.get("tvdb"):
            record.input_tvdb_id = str(ids["tvdb"])
            record.initial_tvdb_id = record.input_tvdb_id

        is_type_verified = simkl.get("typeVerified") is True and int(simkl.get("typeVerifierVersion") or 0) >= TYPE_VERIFIER_VERSION
        if not is_type_verified:
            record.input_simkl_id = str(simkl_id)
            record.initial_simkl_id = record.input_simkl_id
            record.lookup_source = "database_untyped"
            record.reason = "database_id_needs_type"
            needs_type += 1
            continue

        apply_lookup_result(record, LookupResult(
            status="found", source="database", simkl_id=simkl_id, simkl_type=simkl.get("type") or record.simkl_type,
            title=simkl.get("title") or "", year=simkl.get("year"), confidence=100,
            type_verified=True, type_verified_by=simkl.get("typeVerifiedBy") or "database",
        ))
        cached += 1

    return cached, needs_type


def make_record_id(source_type: str, title: str, year: Optional[int]) -> str:
    """Deterministic, stable ID for a (type, title, year) grouping."""
    digest = hashlib.sha1(f"{source_type}|{normalize_title(title)}|{year or ''}".encode("utf-8")).hexdigest()
    return f"{source_type}-{digest[:12]}"


def extract_media_records(backup: dict[str, Any]) -> list[MediaRecord]:
    """Collapse a SIMKL backup structure into one :class:`MediaRecord` per title."""
    records: dict[str, MediaRecord] = {}

    for list_name in ("shows", "anime", "movies"):
        source_type = "movie" if list_name == "movies" else ("anime" if list_name == "anime" else "show")
        for index, entry in enumerate(backup.get(list_name, [])):
            if list_name == "movies":
                title = entry.get("movie", {}).get("title", "")
                year = entry.get("movie", {}).get("year")
            else:
                title = entry.get("show", {}).get("title", "")
                year = None

            record_id = make_record_id(source_type, title, year)
            record = records.get(record_id)
            if record is None:
                record = MediaRecord(
                    id=record_id, source_type=source_type, title=title, year=year,
                    simkl_type=LIST_NAME_TO_TYPE[list_name],
                )
                records[record_id] = record

            entry_status = normalize_watch_status(entry.get("status"))
            if not entry.get("is_rewatch") and entry_status:
                record.watch_status = entry_status
            elif not record.watch_status:
                record.watch_status = entry_status

            record.refs.append((list_name, index))
            record.occurrences += 1
            record.watched_episodes += entry.get("watched_episodes_count", 0) or 0
            if entry.get("is_rewatch"):
                record.rewatch_entries += 1

    return sorted(records.values(), key=lambda r: r.title.casefold())


# --------------------------------------------------------------------------
# Applying lookup results
# --------------------------------------------------------------------------

def mark_not_found(record: MediaRecord, reason: str, error: str = "", field_errors: Optional[dict[str, str]] = None) -> None:
    record.verified_simkl_id = ""
    record.verified_imdb_id = ""
    record.verified_tvdb_id = ""
    record.status = "not_found"
    record.reason = reason
    record.error = error
    record.field_errors = field_errors or {}
    record.type_verified = False
    record.type_verified_by = ""
    record.confidence = None
    record.candidates = []


def apply_lookup_result(
    record: MediaRecord, result: LookupResult, *,
    keep_input_simkl: str = "", keep_input_imdb: str = "", keep_input_tvdb: str = "",
) -> None:
    if result.status != "found":
        mark_not_found(record, result.reason, field_errors=result.field_errors)
        return

    record.verified_simkl_id = str(result.simkl_id) if result.simkl_id else ""
    record.verified_imdb_id = result.imdb_id
    record.verified_tvdb_id = result.tvdb_id
    record.input_simkl_id = keep_input_simkl or record.verified_simkl_id
    record.input_imdb_id = keep_input_imdb or (result.imdb_id or record.input_imdb_id)
    record.input_tvdb_id = keep_input_tvdb or (result.tvdb_id or record.input_tvdb_id)
    if not result.needs_review:
        # Mark this as the confirmed baseline so the row shows as "found" (green).
        # For needs_review matches we deliberately leave initial_* untouched so
        # input_* != initial_* and the row shows as "pending" (yellow) instead -
        # flagging it for a human to double-check without blocking the export.
        record.initial_simkl_id = record.input_simkl_id
        record.initial_imdb_id = record.input_imdb_id
        record.initial_tvdb_id = record.input_tvdb_id
    record.simkl_type = result.simkl_type or record.simkl_type
    record.simkl_title = result.title
    record.simkl_year = result.year
    record.status = "found"
    record.confidence = result.confidence
    record.lookup_source = result.source
    record.type_verified = result.type_verified
    record.type_verified_by = result.type_verified_by
    record.candidates = result.candidates
    record.field_errors = {}
    record.reason = result.reason
    record.error = ""


def clean_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def normalize_record_type(value: Any) -> str:
    return normalize_simkl_type(value)


def validation_types(record: MediaRecord, preferred_type: str = "") -> list[str]:
    types = query_types_for_record(record)
    if preferred_type:
        return list(dict.fromkeys([preferred_type, *types]))
    return types


async def validate_record_ids(
    client: SimklClient, record: MediaRecord, simkl_id: str, imdb_id: str, tvdb_id: str, simkl_type: str,
) -> LookupResult:
    """Validate manually-entered IDs against SIMKL, in priority order simkl > imdb > tvdb."""
    if simkl_id:
        result = await client.lookup_by_id(simkl_id, validation_types(record, simkl_type), record)
        if result.status == "found":
            errors = _compare_external_ids(imdb_id, tvdb_id, result)
            if errors:
                return LookupResult(status="not_found", reason="id_mismatch", field_errors=errors)
        return result

    if imdb_id:
        result = await client.lookup_by_external_id("imdb", imdb_id, validation_types(record, simkl_type), record)
        if result.status == "found":
            errors = _compare_external_ids(imdb_id, tvdb_id, result)
            if errors:
                return LookupResult(status="not_found", reason="id_mismatch", field_errors=errors)
            return result

    if tvdb_id:
        result = await client.lookup_by_external_id("tvdb", tvdb_id, validation_types(record, simkl_type), record)
        if result.status == "found":
            errors = _compare_external_ids(imdb_id, tvdb_id, result)
            if errors:
                return LookupResult(status="not_found", reason="id_mismatch", field_errors=errors)
            return result

    return LookupResult(status="not_found", reason="no_ids")


def _compare_external_ids(imdb_id: str, tvdb_id: str, result: LookupResult) -> dict[str, str]:
    errors: dict[str, str] = {}
    if imdb_id and result.imdb_id and clean_imdb_id(imdb_id) != clean_imdb_id(result.imdb_id):
        errors["imdb_id"] = "IMDb ID does not match the SIMKL item."
    if tvdb_id and result.tvdb_id and clean_numeric_id(tvdb_id) != clean_numeric_id(result.tvdb_id):
        errors["tvdb_id"] = "TVDB ID does not match the SIMKL item."
    return errors


_NON_LATIN_REVIEW_REASON = "Matched via IMDb's top result for a non-Latin title - please double-check this is the right title."


def _flag_low_confidence_match(result: LookupResult, best: ImdbCandidate) -> LookupResult:
    """Mark a successful lookup for manual review when IMDb's candidate was
    accepted on a heuristic (see `find_best_match`'s non-Latin fallback)
    rather than a confident text match, so the review UI shows it as
    "pending" instead of a fully confirmed match.
    """
    if best.needs_review and result.status == "found":
        result.needs_review = True
        result.confidence = min(result.confidence, 65) if result.confidence is not None else 65
        result.reason = _NON_LATIN_REVIEW_REASON
    return result


async def lookup_record(
    client: SimklClient, imdb_client: ImdbClient, tvmaze_client: TvMazeClient, record: MediaRecord,
) -> LookupResult:
    """Find the best SIMKL match for a record that has no known IDs yet.

    SIMKL's own free-text search can fail on titles that are mostly numbers
    or punctuation (e.g. "9-1-1", "The 100", "1899"), even with the correct
    title/year. To work around this, we first resolve an IMDb ID via IMDb's
    title search, then ask SIMKL for that exact IMDb ID - an ID lookup
    instead of a fuzzy title search. If IMDb has no confident match, or
    SIMKL doesn't recognize that IMDb ID, we fall back to SIMKL's own
    title/year search - but using IMDb's own (often shorter/more canonical)
    title first, since TV Time's title can carry a subtitle IMDb/SIMKL don't
    use (e.g. TV Time's "El Camino: A Breaking Bad Movie" is just "El
    Camino" on IMDb). Only after that also fails do we search using the
    original TV Time title as a last resort.

    For shows, TVmaze is also consulted (by that same IMDb ID) for its
    genre tags: the TV Time export has no way to tell us a show is anime,
    but TVmaze does via an explicit "Anime" genre. When it says so, SIMKL's
    "anime" catalog is tried before its "tv" catalog, which both improves
    match accuracy and makes the record land in the right list on export.
    """
    preferred_types = query_types_for_record(record)

    candidates = await imdb_client.search(record.title)
    best = find_best_match(candidates, record.title, record.year, preferred_types)
    if best is not None:
        if record.source_type == "show":
            show = await tvmaze_client.lookup_by_imdb(best.imdb_id)
            if show is not None and show.is_anime():
                preferred_types = ["anime", *[t for t in preferred_types if t != "anime"]]

        result = await client.lookup_by_external_id("imdb", best.imdb_id, preferred_types, record)
        if result.status == "found":
            result.source = "imdb_search"
            return _flag_low_confidence_match(result, best)

        title_differs = best.title and normalize_title(best.title) != normalize_title(record.title)
        year_differs = best.year and best.year != record.year
        if title_differs or year_differs:
            imdb_titled_record = replace(record, title=best.title or record.title, year=best.year or record.year)
            result = await client.enrich_media_record(imdb_titled_record)
            if result.status == "found":
                result.source = "imdb_title"
                return _flag_low_confidence_match(result, best)

    return await client.enrich_media_record(record)


async def enrich_records(
    records: list[MediaRecord], client: SimklClient, imdb_client: ImdbClient, tvmaze_client: TvMazeClient,
    progress: ProgressCallback = _noop_progress,
) -> None:
    """Look up every record that doesn't already have a verified SIMKL ID."""
    total = max(1, len(records))
    for index, record in enumerate(records):
        if record.status == "found" and record.verified_simkl_id:
            progress(f"cached: {record.title}", index + 1, total)
            continue

        progress(
            f"validating known IDs: {record.title}" if (record.input_simkl_id or record.input_imdb_id or record.input_tvdb_id)
            else f"searching IMDb/SIMKL: {record.title}",
            index, total,
        )
        try:
            if record.input_simkl_id or record.input_imdb_id or record.input_tvdb_id:
                result = await validate_record_ids(
                    client, record, record.input_simkl_id, record.input_imdb_id, record.input_tvdb_id, record.simkl_type,
                )
            else:
                result = await lookup_record(client, imdb_client, tvmaze_client, record)
            apply_lookup_result(
                record, result,
                keep_input_simkl=record.input_simkl_id, keep_input_imdb=record.input_imdb_id, keep_input_tvdb=record.input_tvdb_id,
            )
        except Exception as exc:  # noqa: BLE001 - keep processing remaining records
            mark_not_found(record, "simkl_api_error", error=str(exc))

    progress("SIMKL enrichment complete", total, total)


async def validate_manual_records(
    records_by_id: dict[str, MediaRecord],
    updates: list[dict[str, str]],
    client: SimklClient,
    progress: ProgressCallback = _noop_progress,
) -> list[MediaRecord]:
    """Re-validate a batch of manually edited rows (e.g. after the user clicks "Validate changed")."""
    total = max(1, len(updates))
    changed: list[MediaRecord] = []

    for index, update in enumerate(updates):
        record = records_by_id.get(update["id"])
        if record is None:
            continue

        simkl_id = clean_id(update.get("simkl_id", ""))
        imdb_id = clean_imdb_id(update.get("imdb_id", ""))
        tvdb_id = clean_id(update.get("tvdb_id", ""))
        simkl_type = normalize_record_type(update.get("simkl_type") or record.simkl_type)

        record.input_simkl_id = simkl_id
        record.input_imdb_id = imdb_id
        record.input_tvdb_id = tvdb_id
        record.simkl_type = simkl_type
        record.field_errors = {}

        progress(f"validating: {record.title}", index, total)

        if not (simkl_id or imdb_id or tvdb_id):
            mark_not_found(record, "manual_id_removed")
            changed.append(record)
            continue

        try:
            result = await validate_record_ids(client, record, simkl_id, imdb_id, tvdb_id, simkl_type)
            apply_lookup_result(record, result, keep_input_simkl=simkl_id, keep_input_imdb=imdb_id, keep_input_tvdb=tvdb_id)
        except Exception as exc:  # noqa: BLE001
            mark_not_found(record, "simkl_api_error", error=str(exc))
            record.input_simkl_id = simkl_id
            record.input_imdb_id = imdb_id
            record.input_tvdb_id = tvdb_id

        changed.append(record)

    progress("validation complete", total, total)
    return changed


# --------------------------------------------------------------------------
# Export: re-inject confirmed IDs into the backup and build the download ZIP
# --------------------------------------------------------------------------

def make_timestamp(when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.strftime("%Y%m%d-%H%M%S")


def apply_records_to_backup(backup: dict[str, Any], records: list[MediaRecord]) -> dict[str, Any]:
    """Return a new backup dict with SIMKL/IMDb/TVDB IDs applied and entries
    regrouped into shows/anime/movies according to each record's confirmed type.
    """
    result: dict[str, list[Any]] = {"shows": [], "anime": [], "movies": []}

    for list_name in ("shows", "anime", "movies"):
        source_entries = backup.get(list_name, [])
        for index, original_entry in enumerate(source_entries):
            owner = _find_owner_record(records, list_name, index)
            if owner is not None and owner.excluded:
                continue  # user removed this title from the review table - drop it from the export

            entry = deepcopy(original_entry)
            target_list = TYPE_TO_LIST_NAME.get(owner.simkl_type, list_name) if owner else list_name

            if owner is not None:
                watch_status = normalize_watch_status(owner.watch_status)
                if watch_status and not entry.get("is_rewatch"):
                    entry["status"] = watch_status

                ids: dict[str, Any] = {}
                simkl_id = _safe_int(owner.input_simkl_id)
                if simkl_id is not None:
                    ids["simkl"] = simkl_id
                if owner.input_imdb_id:
                    ids["imdb"] = owner.input_imdb_id
                tvdb_id = _safe_int(owner.input_tvdb_id)
                if tvdb_id is not None:
                    ids["tvdb"] = tvdb_id
                if ids:
                    container_key = "movie" if list_name == "movies" else "show"
                    entry.setdefault(container_key, {})["ids"] = ids

            result[target_list].append(entry)

    return result


def _safe_int(value: Any) -> Optional[int]:
    """Best-effort int conversion; returns None instead of raising for bad input."""
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _find_owner_record(records: list[MediaRecord], list_name: str, index: int) -> Optional[MediaRecord]:
    for record in records:
        if (list_name, index) in record.refs:
            return record
    return None


def build_download(
    backup: dict[str, Any], records: list[MediaRecord], *,
    include_tv: bool = True, include_movies: bool = True, include_anime: bool = True,
) -> tuple[str, bytes]:
    """Build the final SIMKL import ZIP, applying the current record IDs.

    :return: ``(filename, zip_bytes)``
    """
    updated = apply_records_to_backup(backup, records)

    filtered = {
        "shows": updated["shows"] if include_tv else [],
        "anime": updated["anime"] if include_anime else [],
        "movies": updated["movies"] if include_movies else [],
    }

    json_text = json.dumps(filtered, indent=2, ensure_ascii=False) + "\n"
    zip_bytes = create_simkl_backup_zip(json_text)
    filename = f"SimklBackup-{make_timestamp()}.zip"
    return filename, zip_bytes


#: Header row for SIMKL's own bulk-import CSV format (see
#: https://simkl.com/apps/import - a flatter, distinct alternative to the
#: JSON backup ZIP).
SIMKL_CSV_HEADER = ["simkl_id", "TVDB_ID", "TMDB", "IMDB_ID", "MAL_ID", "Type", "Title", "Year", "LastEpWatched", "Watchlist", "WatchedDate", "Rating", "Memo"]

#: Maps this app's internal watch status to SIMKL's CSV vocabulary.
_SIMKL_CSV_WATCHLIST_LABELS = {
    "watching": "watching",
    "completed": "completed",
    "hold": "on hold",
    "dropped": "dropped",
    "plantowatch": "plan to watch",
}

_LAST_WATCHED_RE = re.compile(r"^S(\d+)E(\d+)$", re.IGNORECASE)


def _format_csv_last_episode(last_watched: Optional[str]) -> str:
    """Convert the backup's "S02E15" label to SIMKL CSV's unpadded "s2e15" form."""
    match = _LAST_WATCHED_RE.match(last_watched or "")
    if not match:
        return ""
    season, episode = int(match.group(1)), int(match.group(2))
    return f"s{season}e{episode}"


def _format_csv_date(iso_value: Optional[str]) -> str:
    """Convert an ISO 8601 timestamp to SIMKL CSV's unpadded M/D/YYYY date."""
    if not iso_value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def build_simkl_csv_export(
    backup: dict[str, Any], records: list[MediaRecord], *,
    include_tv: bool = True, include_movies: bool = True, include_anime: bool = True,
) -> tuple[str, bytes]:
    """Build a CSV in SIMKL's own bulk-import format, applying the current record IDs.

    This is a distinct, simpler format from the JSON backup ZIP (one row per
    title, a coarser status vocabulary, no rewatch support and no per-episode
    detail). If a title has both a regular watch entry and a rewatch entry,
    only the regular one is exported, since the format has no way to
    represent a rewatch.

    :return: ``(filename, csv_bytes)``
    """
    updated = apply_records_to_backup(backup, records)
    include_by_list = {"shows": include_tv, "anime": include_anime, "movies": include_movies}

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(SIMKL_CSV_HEADER)

    for list_name, type_value in (("shows", "tv"), ("anime", "anime"), ("movies", "movie")):
        if not include_by_list[list_name]:
            continue
        for entry in updated[list_name]:
            if entry.get("is_rewatch"):
                continue

            container_key = "movie" if list_name == "movies" else "show"
            container = entry.get(container_key, {})
            ids = container.get("ids", {})
            status = entry.get("status", "")
            last_episode = "" if list_name == "movies" else _format_csv_last_episode(entry.get("last_watched"))

            writer.writerow([
                ids.get("simkl", ""), ids.get("tvdb", ""), "", ids.get("imdb", ""), "",
                type_value, container.get("title", ""), container.get("year", ""),
                last_episode, _SIMKL_CSV_WATCHLIST_LABELS.get(status, status),
                _format_csv_date(entry.get("last_watched_at")), "", "",
            ])

    filename = f"SimklImport-{make_timestamp()}.csv"
    return filename, buffer.getvalue().encode("utf-8-sig")
