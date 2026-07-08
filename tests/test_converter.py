"""Unit tests for the TV Time -> SIMKL conversion pipeline."""
from __future__ import annotations

import io
import zipfile

from src.converter import ConversionOptions, convert_tvtime_to_simkl_json, load_tvtime_data


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_watched_episodes_and_rewatches():
    zip_bytes = make_zip({
        "tracking-prod-records-v2.csv": (
            "key,series_name,season_number,episode_number,created_at\n"
            "watch-episode-1,Breaking Bad,1,1,2020-01-01 10:00:00\n"
            "watch-episode-2,Breaking Bad,1,2,2020-01-02 10:00:00\n"
            "rewatch-episode-1,Breaking Bad,1,1,2021-01-01 10:00:00\n"
        ),
    })
    loaded = load_tvtime_data(zip_bytes)
    result = convert_tvtime_to_simkl_json(loaded, ConversionOptions())

    assert len(result.simkl_backup["shows"]) == 2
    watch_entry = next(e for e in result.simkl_backup["shows"] if not e["is_rewatch"])
    rewatch_entry = next(e for e in result.simkl_backup["shows"] if e["is_rewatch"])

    assert watch_entry["watched_episodes_count"] == 2
    assert watch_entry["status"] == "watching"
    assert rewatch_entry["watched_episodes_count"] == 1
    assert rewatch_entry["rewatch_id"] == 1
    assert result.summary["shows"] == 1
    assert result.summary["show_rewatch_entries"] == 1


def test_movies_watch_towatch_and_rewatch():
    zip_bytes = make_zip({
        "tracking-prod-records.csv": (
            "type,entity_type,movie_name,release_date,updated_at,rewatch_count,uuid\n"
            "watch,movie,Inception,2010-01-01,2020-05-01 10:00:00,,\n"
            "towatch,movie,Interstellar,2014-01-01,2020-05-02 10:00:00,,\n"
            "rewatch,movie,Inception,2010-01-01,2022-01-01 10:00:00,1,abc\n"
        ),
    })
    loaded = load_tvtime_data(zip_bytes)
    result = convert_tvtime_to_simkl_json(loaded, ConversionOptions())

    movies = result.simkl_backup["movies"]
    titles_status = {(m["movie"]["title"], m["status"], m["is_rewatch"]) for m in movies}
    assert ("Inception", "completed", False) in titles_status
    assert ("Interstellar", "plantowatch", False) in titles_status
    assert ("Inception", "completed", True) in titles_status


def test_invalid_episode_rows_are_reported_and_skipped():
    zip_bytes = make_zip({
        "tracking-prod-records-v2.csv": (
            "key,series_name,season_number,episode_number,created_at\n"
            "watch-episode-1,,1,1,2020-01-01 10:00:00\n"  # missing title
        ),
    })
    loaded = load_tvtime_data(zip_bytes)
    result = convert_tvtime_to_simkl_json(loaded, ConversionOptions())

    assert result.simkl_backup["shows"] == []
    assert len(result.report_rows) == 1
    assert result.report_rows[0]["action"] == "not converted"


def test_include_rewatches_false_excludes_rewatch_entries():
    zip_bytes = make_zip({
        "tracking-prod-records-v2.csv": (
            "key,series_name,season_number,episode_number,created_at\n"
            "watch-episode-1,Breaking Bad,1,1,2020-01-01 10:00:00\n"
            "rewatch-episode-1,Breaking Bad,1,1,2021-01-01 10:00:00\n"
        ),
    })
    loaded = load_tvtime_data(zip_bytes)
    result = convert_tvtime_to_simkl_json(loaded, ConversionOptions(include_rewatches=False))

    assert len(result.simkl_backup["shows"]) == 1
    assert result.simkl_backup["shows"][0]["is_rewatch"] is False
