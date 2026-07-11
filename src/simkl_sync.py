"""Direct, authenticated import of a converted job into a SIMKL account."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any, Callable

from .records import MediaRecord, apply_records_to_backup, make_timestamp, normalize_watch_status
from .simkl_client import SimklClient

ProgressCallback = Callable[[str, int, int], None]
BATCH_SIZE = 50


def _noop_progress(_phase: str, _done: int, _total: int) -> None:
    return None


def direct_sync_issue_reasons(record: MediaRecord) -> list[str]:
    """Return direct-import limitations for one reviewable title."""
    reasons: list[str] = []
    if record.rewatch_entries:
        reasons.append(f"{record.rewatch_entries} rewatch session(s) require SIMKL Pro/VIP handling")

    has_canonical_entry = record.occurrences > record.rewatch_entries
    if not has_canonical_entry:
        return reasons

    if not (record.input_simkl_id or record.input_imdb_id or record.input_tvdb_id):
        reasons.append("no usable media ID")

    target_is_movie = record.simkl_type == "movie"
    source_has_movie = any(list_name == "movies" for list_name, _index in record.refs)
    source_has_show = any(list_name != "movies" for list_name, _index in record.refs)
    if (target_is_movie and source_has_show) or (not target_is_movie and source_has_movie):
        reasons.append(f"source {record.source_type} cannot be sent as {record.simkl_type} without review")
    return reasons


@dataclass
class DirectSyncPlan:
    history_batches: list[dict[str, Any]] = field(default_factory=list)
    status_batches: list[dict[str, Any]] = field(default_factory=list)
    skipped_unmatched: list[str] = field(default_factory=list)
    skipped_rewatches: int = 0
    failed_items: list[dict[str, str]] = field(default_factory=list)


@dataclass
class DirectSyncResult:
    history_batches: int = 0
    status_batches: int = 0
    not_found: list[dict[str, Any]] = field(default_factory=list)
    skipped_unmatched: list[str] = field(default_factory=list)
    skipped_rewatches: int = 0
    failed_items: list[dict[str, str]] = field(default_factory=list)


FAILED_IMPORT_CSV_HEADER = [
    "reason", "phase", "source_type", "target_type", "title", "year",
    "simkl_id", "imdb_id", "tvdb_id", "tmdb_id", "watch_status",
    "is_rewatch", "details",
]


def _entry_title(entry: dict[str, Any]) -> str:
    return str((entry.get("show") or entry.get("movie") or {}).get("title") or "Unknown title")


def _failure_from_entry(
    entry: dict[str, Any], list_name: str, reason: str, details: str,
) -> dict[str, str]:
    container_key = "show" if entry.get("show") else "movie" if entry.get("movie") else ""
    container = entry.get(container_key) or {}
    ids = container.get("ids") or {}
    return {
        "reason": reason,
        "phase": "planning",
        "source_type": container_key,
        "target_type": {"shows": "tv", "anime": "anime", "movies": "movie"}[list_name],
        "title": str(container.get("title") or "Unknown title"),
        "year": str(container.get("year") or ""),
        "simkl_id": str(ids.get("simkl") or ""),
        "imdb_id": str(ids.get("imdb") or ""),
        "tvdb_id": str(ids.get("tvdb") or ""),
        "tmdb_id": str(ids.get("tmdb") or ""),
        "watch_status": str(entry.get("status") or ""),
        "is_rewatch": "yes" if entry.get("is_rewatch") else "no",
        "details": details,
    }


def _failure_from_not_found(
    failed: dict[str, Any], phase: str, target_status: str = "",
) -> dict[str, str]:
    item = failed.get("item") or {}
    ids = item.get("ids") or {}
    bucket = str(failed.get("type") or "")
    return {
        "reason": "simkl_not_found",
        "phase": phase,
        "source_type": "",
        "target_type": {"shows": "tv", "anime": "anime", "movies": "movie"}.get(bucket, bucket),
        "title": str(item.get("title") or "Unknown title"),
        "year": str(item.get("year") or ""),
        "simkl_id": str(ids.get("simkl") or ""),
        "imdb_id": str(ids.get("imdb") or ""),
        "tvdb_id": str(ids.get("tvdb") or ""),
        "tmdb_id": str(ids.get("tmdb") or ""),
        "watch_status": target_status,
        "is_rewatch": "no",
        "details": "SIMKL did not recognize this item in the API request.",
    }


def build_failed_import_csv(result: DirectSyncResult) -> tuple[str, bytes]:
    """Build a spreadsheet-friendly report of every item not imported by direct sync."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FAILED_IMPORT_CSV_HEADER)
    writer.writeheader()
    writer.writerows(result.failed_items)
    return (
        f"SimklDirectImportFailures-{make_timestamp()}.csv",
        buffer.getvalue().encode("utf-8-sig"),
    )


def _media_object(entry: dict[str, Any], list_name: str) -> dict[str, Any] | None:
    container_key = "movie" if list_name == "movies" else "show"
    container = entry.get(container_key) or {}
    ids = container.get("ids") or {}
    if not ids:
        return None

    item: dict[str, Any] = {"ids": ids}
    if container.get("title"):
        item["title"] = container["title"]
    if container.get("year"):
        item["year"] = container["year"]
    if entry.get("added_to_watchlist_at"):
        item["added_at"] = entry["added_to_watchlist_at"]
    return item


def _history_object(entry: dict[str, Any], list_name: str) -> dict[str, Any] | None:
    item = _media_object(entry, list_name)
    if item is None:
        return None

    if list_name == "movies":
        if not entry.get("watched_episodes_count"):
            return None
        if entry.get("last_watched_at"):
            item["watched_at"] = entry["last_watched_at"]
        return item

    seasons = []
    for season in entry.get("seasons") or []:
        episodes = []
        for episode in season.get("episodes") or []:
            episode_item: dict[str, Any] = {"number": episode["number"]}
            if episode.get("watched_at"):
                episode_item["watched_at"] = episode["watched_at"]
            if episode.get("ids"):
                episode_item["ids"] = episode["ids"]
            episodes.append(episode_item)
        if episodes:
            seasons.append({"number": season["number"], "episodes": episodes})
    if not seasons:
        return None
    item["seasons"] = seasons
    return item


def _batch_items(items: list[tuple[str, dict[str, Any]]], *, to: str = "") -> list[dict[str, Any]]:
    batches: list[dict[str, Any]] = []
    for offset in range(0, len(items), BATCH_SIZE):
        payload: dict[str, Any] = {"to": to} if to else {}
        for bucket, item in items[offset:offset + BATCH_SIZE]:
            payload.setdefault(bucket, []).append(item)
        batches.append(payload)
    return batches


def build_direct_sync_plan(
    backup: dict[str, Any], records: list[MediaRecord], *,
    include_tv: bool = True, include_movies: bool = True, include_anime: bool = True,
) -> DirectSyncPlan:
    """Build idempotent canonical-history and final-status API batches."""
    updated = apply_records_to_backup(backup, records)
    include_by_list = {"shows": include_tv, "anime": include_anime, "movies": include_movies}
    plan = DirectSyncPlan()
    history_items: list[tuple[str, dict[str, Any]]] = []
    status_items: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    for list_name in ("shows", "anime", "movies"):
        if not include_by_list[list_name]:
            continue
        bucket = list_name
        for entry in updated[list_name]:
            if entry.get("is_rewatch"):
                plan.skipped_rewatches += 1
                plan.failed_items.append(_failure_from_entry(
                    entry, list_name, "rewatch_not_supported",
                    "Direct rewatch writes require SIMKL Pro/VIP session handling.",
                ))
                continue
            item = _media_object(entry, list_name)
            if item is None:
                plan.skipped_unmatched.append(_entry_title(entry))
                expected_key = "movie" if list_name == "movies" else "show"
                actual_key = "show" if entry.get("show") else "movie" if entry.get("movie") else ""
                reason = "cross_type_container_mismatch" if actual_key and actual_key != expected_key else "missing_media_ids"
                details = (
                    "The reviewed media type differs from the original backup container."
                    if reason == "cross_type_container_mismatch"
                    else "No usable media ID was available for the API request."
                )
                plan.failed_items.append(_failure_from_entry(entry, list_name, reason, details))
                continue

            history_item = _history_object(entry, list_name)
            if history_item is not None:
                history_items.append((bucket, history_item))

            status = normalize_watch_status(entry.get("status"))
            if list_name == "movies" and status == "watching":
                status = "completed"
            if list_name == "movies" and status == "hold":
                status = "plantowatch"
            inferred_show_watching = list_name != "movies" and status == "watching" and history_item is not None
            if status and not inferred_show_watching:
                status_items.setdefault(status, []).append((bucket, item))

    plan.history_batches = _batch_items(history_items)
    for status in ("watching", "plantowatch", "hold", "dropped", "completed"):
        plan.status_batches.extend(_batch_items(status_items.get(status, []), to=status))
    return plan


def _collect_not_found(response: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for bucket, items in (response.get("not_found") or {}).items():
        for item in items or []:
            result.append({"type": bucket, "item": item})
    return result


async def sync_job_directly(
    client: SimklClient, access_token: str, backup: dict[str, Any], records: list[MediaRecord], *,
    include_tv: bool = True, include_movies: bool = True, include_anime: bool = True,
    progress: ProgressCallback = _noop_progress,
) -> DirectSyncResult:
    """Write canonical history first, then move every title to its final list."""
    plan = build_direct_sync_plan(
        backup, records, include_tv=include_tv, include_movies=include_movies, include_anime=include_anime,
    )
    total = len(plan.history_batches) + len(plan.status_batches)
    result = DirectSyncResult(
        skipped_unmatched=plan.skipped_unmatched,
        skipped_rewatches=plan.skipped_rewatches,
        failed_items=list(plan.failed_items),
    )
    done = 0

    for payload in plan.history_batches:
        progress("sending watch history", done, max(1, total))
        response = await client.post_user_data(
            "/sync/history", payload, access_token, params={"skip_auto_watching": "yes"},
        )
        not_found = _collect_not_found(response)
        result.not_found.extend(not_found)
        result.failed_items.extend(_failure_from_not_found(item, "history") for item in not_found)
        result.history_batches += 1
        done += 1

    for payload in plan.status_batches:
        target_status = payload["to"]
        status_payload = {
            bucket: [{**item, "to": target_status} for item in items]
            for bucket, items in payload.items()
            if bucket != "to"
        }
        progress(f"applying {target_status} status", done, max(1, total))
        response = await client.post_user_data(
            "/sync/add-to-list", status_payload, access_token,
        )
        not_found = _collect_not_found(response)
        result.not_found.extend(not_found)
        result.failed_items.extend(
            _failure_from_not_found(item, "status", target_status) for item in not_found
        )
        result.status_batches += 1
        done += 1

    progress("SIMKL account sync complete", max(1, total), max(1, total))
    return result