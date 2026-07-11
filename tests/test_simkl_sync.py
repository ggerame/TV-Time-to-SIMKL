"""Tests for direct authenticated SIMKL import planning."""
from __future__ import annotations

import asyncio
import csv
import io

from src.records import extract_media_records
from src.simkl_sync import (
    build_direct_sync_plan,
    build_failed_import_csv,
    direct_sync_issue_reasons,
    sync_job_directly,
)


def test_direct_sync_separates_history_from_final_statuses():
    backup = {
        "shows": [
            {
                "status": "dropped", "watched_episodes_count": 1, "is_rewatch": False,
                "show": {"title": "Dropped Show", "ids": {"simkl": 1}},
                "seasons": [{"number": 1, "episodes": [{"number": 2, "watched_at": "2020-01-02T00:00:00Z"}]}],
            },
            {
                "status": "plantowatch", "watched_episodes_count": 0, "is_rewatch": False,
                "show": {"title": "Planned Show", "ids": {"simkl": 2}},
            },
        ],
        "anime": [],
        "movies": [
            {
                "status": "completed", "watched_episodes_count": 1, "is_rewatch": False,
                "last_watched_at": "2021-03-04T00:00:00Z",
                "movie": {"title": "Seen Movie", "year": 2021, "ids": {"simkl": 3}},
            },
        ],
    }
    plan = build_direct_sync_plan(backup, extract_media_records(backup))

    assert len(plan.history_batches) == 1
    history = plan.history_batches[0]
    assert [item["title"] for item in history["shows"]] == ["Dropped Show"]
    assert history["shows"][0]["seasons"][0]["episodes"] == [
        {"number": 2, "watched_at": "2020-01-02T00:00:00Z"},
    ]
    assert [item["title"] for item in history["movies"]] == ["Seen Movie"]
    assert all("Planned Show" not in str(batch) for batch in plan.history_batches)

    statuses = {batch["to"]: batch for batch in plan.status_batches}
    assert statuses["dropped"]["shows"][0]["title"] == "Dropped Show"
    assert statuses["plantowatch"]["shows"][0]["title"] == "Planned Show"
    assert statuses["completed"]["movies"][0]["title"] == "Seen Movie"
    assert plan.skipped_unmatched == []


def test_direct_sync_writes_history_before_statuses():
    backup = {
        "shows": [{
            "status": "dropped", "watched_episodes_count": 1, "is_rewatch": False,
            "show": {"title": "Dropped Show", "ids": {"simkl": 1}},
            "seasons": [{"number": 1, "episodes": [{"number": 1}]}],
        }],
        "anime": [],
        "movies": [],
    }

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def post_user_data(self, path, payload, access_token, *, params=None):
            self.calls.append((path, payload, access_token, params))
            return {"not_found": {"movies": [], "shows": []}}

    client = FakeClient()
    result = asyncio.run(sync_job_directly(
        client, "token", backup, extract_media_records(backup),
    ))

    assert [call[0] for call in client.calls] == ["/sync/history", "/sync/add-to-list"]
    assert client.calls[0][3] == {"skip_auto_watching": "yes"}
    assert client.calls[1][3] is None
    assert "to" not in client.calls[1][1]
    assert client.calls[1][1]["shows"][0]["to"] == "dropped"
    assert result.history_batches == 1
    assert result.status_batches == 1


def test_direct_sync_does_not_overwrite_history_derived_show_status_with_watching():
    backup = {
        "shows": [{
            "status": "watching", "watched_episodes_count": 2, "is_rewatch": False,
            "show": {"title": "Finished Show", "ids": {"simkl": 1}},
            "seasons": [{"number": 1, "episodes": [{"number": 1}, {"number": 2}]}],
        }],
        "anime": [],
        "movies": [],
    }

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def post_user_data(self, path, payload, access_token, *, params=None):
            self.calls.append((path, payload, access_token, params))
            return {"not_found": {"movies": [], "shows": []}}

    client = FakeClient()
    result = asyncio.run(sync_job_directly(
        client, "token", backup, extract_media_records(backup),
    ))

    assert [call[0] for call in client.calls] == ["/sync/history"]
    assert result.history_batches == 1
    assert result.status_batches == 0


def test_direct_sync_reports_rewatches_and_cross_type_titles():
    backup = {
        "shows": [{
            "status": "watching", "watched_episodes_count": 1, "is_rewatch": False,
            "show": {"title": "Actually a Movie"}, "seasons": [],
        }],
        "anime": [],
        "movies": [{
            "status": "completed", "watched_episodes_count": 1, "is_rewatch": True,
            "movie": {"title": "Rewatched Movie", "ids": {"simkl": 2}},
        }],
    }
    records = extract_media_records(backup)
    show_record = next(record for record in records if record.title == "Actually a Movie")
    show_record.simkl_type = "movie"
    show_record.input_simkl_id = "1"

    plan = build_direct_sync_plan(backup, records)

    assert plan.skipped_unmatched == ["Actually a Movie"]
    assert plan.skipped_rewatches == 1
    assert [(item["title"], item["reason"]) for item in plan.failed_items] == [
        ("Actually a Movie", "cross_type_container_mismatch"),
        ("Rewatched Movie", "rewatch_not_supported"),
    ]
    assert direct_sync_issue_reasons(show_record) == [
        "source show cannot be sent as movie without review",
    ]
    rewatch_record = next(record for record in records if record.title == "Rewatched Movie")
    assert direct_sync_issue_reasons(rewatch_record) == [
        "1 rewatch session(s) require SIMKL Pro/VIP handling",
    ]


def test_direct_sync_failure_csv_includes_planning_and_api_failures():
    backup = {
        "shows": [{
            "status": "dropped", "watched_episodes_count": 1, "is_rewatch": False,
            "show": {"title": "Missing Show", "ids": {"simkl": 9}},
            "seasons": [{"number": 1, "episodes": [{"number": 1}]}],
        }],
        "anime": [],
        "movies": [{
            "status": "completed", "watched_episodes_count": 1, "is_rewatch": True,
            "movie": {"title": "Rewatched Movie", "year": 2020, "ids": {"simkl": 2}},
        }],
    }

    class FakeClient:
        async def post_user_data(self, path, payload, access_token, *, params=None):
            if path == "/sync/history":
                return {"not_found": {"shows": [{"title": "Missing Show", "ids": {"simkl": 9}}]}}
            return {"not_found": {"shows": []}}

    result = asyncio.run(sync_job_directly(
        FakeClient(), "token", backup, extract_media_records(backup),
    ))
    filename, content = build_failed_import_csv(result)
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))

    assert filename.startswith("SimklDirectImportFailures-")
    assert [(row["title"], row["reason"], row["phase"]) for row in rows] == [
        ("Rewatched Movie", "rewatch_not_supported", "planning"),
        ("Missing Show", "simkl_not_found", "history"),
    ]
    assert rows[0]["simkl_id"] == "2"
    assert rows[1]["watch_status"] == ""