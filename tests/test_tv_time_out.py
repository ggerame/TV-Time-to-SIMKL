"""Tests for the optional TV Time Out export parser."""
from __future__ import annotations

import io
import json
import zipfile

from src.records import apply_records_to_backup, extract_media_records
from src.tv_time_out import apply_tvtime_out_mappings, mapping_key, parse_tvtime_out_zip


def _make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_series_statuses_are_applied_to_the_final_backup():
    series = [
        {"title": "Finished Show", "status": "up_to_date", "id": {"tvdb": 1, "imdb": "tt0000001"}},
        {"title": "Current Show", "status": "continuing", "id": {"tvdb": 2}},
        {"title": "Stopped Show", "status": "stopped", "id": {"tvdb": 3}},
        {"title": "Planned Show", "status": "not_started_yet", "id": {"tvdb": 4}},
    ]
    zip_bytes = _make_zip({
        "tvtime-series-2026-07-11.json": json.dumps(series),
        "tvtime-series-2026-07-11.csv": (
            "uuid,tvdb_id,imdb_id,title,status,created_at\n"
            ",,,Status Only,stopped,\n"
        ),
    })
    parsed = parse_tvtime_out_zip(zip_bytes)

    assert parsed.mappings[mapping_key("show", "Finished Show", None)].watch_status == "completed"
    assert parsed.mappings[mapping_key("show", "Current Show", None)].watch_status == "watching"
    assert parsed.mappings[mapping_key("show", "Stopped Show", None)].watch_status == "dropped"
    assert parsed.mappings[mapping_key("show", "Planned Show", None)].watch_status == "plantowatch"
    assert parsed.mappings[mapping_key("show", "Status Only", None)].watch_status == "dropped"

    backup = {
        "shows": [
            {
                "status": "watching", "watched_episodes_count": 1, "is_rewatch": False,
                "show": {"title": item["title"]},
            }
            for item in series
        ],
        "anime": [],
        "movies": [],
    }
    records = extract_media_records(backup)
    assert apply_tvtime_out_mappings(records, parsed.mappings) == 4

    updated = apply_records_to_backup(backup, records)
    statuses = {entry["show"]["title"]: entry["status"] for entry in updated["shows"]}
    assert statuses == {
        "Finished Show": "completed",
        "Current Show": "watching",
        "Stopped Show": "dropped",
        "Planned Show": "plantowatch",
    }
    assert updated["shows"][0]["show"]["ids"] == {"imdb": "tt0000001", "tvdb": 1}