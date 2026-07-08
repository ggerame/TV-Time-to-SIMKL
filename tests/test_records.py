"""Unit tests for media record extraction, enrichment application and export."""
from __future__ import annotations

import asyncio
import csv
import io

from src.imdb_client import ImdbCandidate
from src.records import (
    apply_lookup_result,
    apply_records_to_backup,
    build_download,
    build_simkl_csv_export,
    extract_media_records,
    lookup_record,
    make_record_id,
    mark_not_found,
)
from src.simkl_client import LookupResult
from src.tvmaze_client import TvMazeShow


def sample_backup() -> dict:
    return {
        "shows": [
            {
                "status": "watching", "watched_episodes_count": 2, "is_rewatch": False,
                "show": {"title": "Breaking Bad"}, "seasons": [],
            },
            {
                "status": "watching", "watched_episodes_count": 1, "is_rewatch": True,
                "show": {"title": "Breaking Bad"}, "seasons": [], "rewatch_id": 1,
            },
        ],
        "anime": [],
        "movies": [
            {"status": "completed", "watched_episodes_count": 1, "is_rewatch": False, "movie": {"title": "Inception", "year": 2010}},
        ],
    }


def test_extract_media_records_groups_by_title():
    records = extract_media_records(sample_backup())
    titles = {record.title for record in records}
    assert titles == {"Breaking Bad", "Inception"}

    show_record = next(r for r in records if r.title == "Breaking Bad")
    assert show_record.occurrences == 2
    assert show_record.watched_episodes == 3
    assert show_record.rewatch_entries == 1
    assert len(show_record.refs) == 2


def test_extract_media_records_does_not_merge_different_kanji_hangul_shows():
    """Regression test: two distinct shows titled purely in Kanji/Hangul (and
    with no year, like all TV Time shows) must not collapse into a single
    record just because their titles are outside the Latin alphabet.
    """
    backup = {
        "shows": [
            {"status": "watching", "watched_episodes_count": 26, "is_rewatch": False, "show": {"title": "\u9b3c\u6ec5\u306e\u5203"}, "seasons": []},
            {"status": "watching", "watched_episodes_count": 12, "is_rewatch": False, "show": {"title": "\uc9c4\uACA9\uc758 \uac70\uc778"}, "seasons": []},
        ],
        "anime": [], "movies": [],
    }
    records = extract_media_records(backup)
    assert len(records) == 2
    assert {r.id for r in records} == {
        make_record_id("show", "\u9b3c\u6ec5\u306e\u5203", None),
        make_record_id("show", "\uc9c4\uACA9\uc758 \uac70\uc778", None),
    }
    episode_counts = {r.title: r.watched_episodes for r in records}
    assert episode_counts == {"\u9b3c\u6ec5\u306e\u5203": 26, "\uc9c4\uACA9\uc758 \uac70\uc778": 12}


def test_apply_lookup_result_found_and_not_found():
    records = extract_media_records(sample_backup())
    record = records[0]

    apply_lookup_result(record, LookupResult(status="found", simkl_id=123, simkl_type="tv", title="Breaking Bad", confidence=95))
    assert record.status == "found"
    assert record.verified_simkl_id == "123"
    assert record.input_simkl_id == "123"

    mark_not_found(record, "no_match")
    assert record.status == "not_found"
    assert record.verified_simkl_id == ""


def test_apply_records_to_backup_injects_ids():
    backup = sample_backup()
    records = extract_media_records(backup)
    for record in records:
        record.input_simkl_id = "999"

    updated = apply_records_to_backup(backup, records)
    assert updated["shows"][0]["show"]["ids"]["simkl"] == 999
    assert updated["movies"][0]["movie"]["ids"]["simkl"] == 999


def test_apply_records_to_backup_drops_excluded_records():
    backup = sample_backup()
    records = extract_media_records(backup)
    movie_record = next(r for r in records if r.title == "Inception")
    movie_record.excluded = True

    updated = apply_records_to_backup(backup, records)

    assert updated["movies"] == []
    assert len(updated["shows"]) == 2  # the show entries are untouched


def test_build_download_produces_valid_zip():
    backup = sample_backup()
    records = extract_media_records(backup)
    filename, zip_bytes = build_download(backup, records, include_tv=True, include_movies=False, include_anime=True)

    assert filename.startswith("SimklBackup-") and filename.endswith(".zip")
    assert len(zip_bytes) > 0


def test_build_simkl_csv_export_matches_simkl_bulk_import_format():
    backup = {
        "shows": [
            {
                "status": "watching", "watched_episodes_count": 2, "is_rewatch": False,
                "last_watched": "S01E02", "last_watched_at": "2020-01-02T10:00:00Z", "show": {"title": "Breaking Bad"},
            },
            {"status": "plantowatch", "watched_episodes_count": 0, "is_rewatch": False, "show": {"title": "The Wire"}},
            # A rewatch entry for the same show as above - must be skipped, the format has no rewatch concept.
            {
                "status": "watching", "watched_episodes_count": 1, "is_rewatch": True,
                "last_watched": "S01E01", "last_watched_at": "2021-01-01T00:00:00Z", "show": {"title": "Breaking Bad"},
            },
        ],
        "anime": [],
        "movies": [
            {
                "status": "completed", "watched_episodes_count": 1, "is_rewatch": False,
                "last_watched_at": "2019-11-10T00:00:00Z", "movie": {"title": "El Camino", "year": 2019},
            },
        ],
    }
    records = extract_media_records(backup)
    by_title = {r.title: r for r in records}
    by_title["Breaking Bad"].input_simkl_id = "1"
    by_title["Breaking Bad"].input_imdb_id = "tt0903747"
    by_title["El Camino"].input_simkl_id = "5"
    by_title["El Camino"].input_imdb_id = "tt9243946"
    by_title["El Camino"].input_tvdb_id = "12345"

    filename, csv_bytes = build_simkl_csv_export(backup, records)

    assert filename.startswith("SimklImport-") and filename.endswith(".csv")
    text = csv_bytes.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))

    assert rows[0] == ["simkl_id", "TVDB_ID", "TMDB", "IMDB_ID", "MAL_ID", "Type", "Title", "Year", "LastEpWatched", "Watchlist", "WatchedDate", "Rating", "Memo"]
    assert rows[1] == ["1", "", "", "tt0903747", "", "tv", "Breaking Bad", "", "s1e2", "watching", "1/2/2020", "", ""]
    assert rows[2] == ["", "", "", "", "", "tv", "The Wire", "", "", "plan to watch", "", "", ""]
    assert rows[3] == ["5", "12345", "", "tt9243946", "", "movie", "El Camino", "2019", "", "completed", "11/10/2019", "", ""]
    # The rewatch entry must not produce a 4th data row.
    assert len(rows) == 4


def test_build_simkl_csv_export_respects_type_filters_and_exclusion():
    backup = sample_backup()
    records = extract_media_records(backup)
    for record in records:
        if record.title == "Inception":
            record.excluded = True

    _, csv_bytes = build_simkl_csv_export(backup, records, include_tv=True, include_movies=False, include_anime=True)
    text = csv_bytes.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))

    assert all(row[5] != "movie" for row in rows[1:])
    titles = {row[6] for row in rows[1:]}
    assert "Inception" not in titles


class FakeImdbClient:
    """Stand-in for ImdbClient that returns a canned candidate list, no network."""

    def __init__(self, candidates):
        self._candidates = candidates

    async def search(self, _query):
        return self._candidates


class FakeTvMazeClient:
    """Stand-in for TvMazeClient that returns a canned show (or None), no network."""

    def __init__(self, show=None):
        self._show = show

    async def lookup_by_imdb(self, _imdb_id):
        return self._show


class FakeSimklClient:
    """Stand-in for SimklClient that records which lookup path was used."""

    def __init__(self, *, external_id_result=None, search_result=None, search_results=None):
        self._external_id_result = external_id_result
        self._search_result = search_result
        self._search_results = list(search_results) if search_results is not None else None
        self.calls = []
        self.preferred_types_seen = []

    async def lookup_by_external_id(self, kind, value, preferred_types, record):
        self.calls.append(("external_id", kind, value))
        self.preferred_types_seen.append(list(preferred_types))
        return self._external_id_result or LookupResult(status="not_found", reason="not_found")

    async def enrich_media_record(self, record):
        self.calls.append(("title_search", record.title))
        if self._search_results is not None:
            if self._search_results:
                return self._search_results.pop(0)
            return LookupResult(status="not_found", reason="no_match")
        return self._search_result or LookupResult(status="not_found", reason="no_match")


def test_lookup_record_uses_imdb_id_when_imdb_has_a_confident_match():
    records = extract_media_records({"shows": [{"status": "watching", "watched_episodes_count": 1, "is_rewatch": False, "show": {"title": "9-1-1"}}], "anime": [], "movies": []})
    record = records[0]

    imdb_client = FakeImdbClient([ImdbCandidate(imdb_id="tt7235466", title="9-1-1", year=None, category="tvSeries", rank=478)])
    tvmaze_client = FakeTvMazeClient(None)
    simkl_client = FakeSimklClient(external_id_result=LookupResult(status="found", simkl_id=999, simkl_type="tv", title="9-1-1", confidence=100))

    result = asyncio.run(lookup_record(simkl_client, imdb_client, tvmaze_client, record))

    assert result.status == "found"
    assert result.source == "imdb_search"
    assert simkl_client.calls == [("external_id", "imdb", "tt7235466")]
    assert result.needs_review is False


def test_lookup_record_flags_non_latin_fallback_matches_for_review():
    records = extract_media_records({"shows": [{"status": "watching", "watched_episodes_count": 26, "is_rewatch": False, "show": {"title": "\u9b3c\u6ec5\u306e\u5203"}}], "anime": [], "movies": []})
    record = records[0]

    imdb_client = FakeImdbClient([ImdbCandidate(
        imdb_id="tt9335498", title="Demon Slayer: Kimetsu no Yaiba", year=2019, category="tvSeries", rank=523, needs_review=True,
    )])
    tvmaze_client = FakeTvMazeClient(None)
    simkl_client = FakeSimklClient(external_id_result=LookupResult(
        status="found", simkl_id=42, simkl_type="tv", title="Demon Slayer: Kimetsu no Yaiba", confidence=95,
    ))

    result = asyncio.run(lookup_record(simkl_client, imdb_client, tvmaze_client, record))

    assert result.status == "found"
    assert result.needs_review is True
    assert result.confidence <= 65
    assert result.reason

    apply_lookup_result(record, result)

    # Auto-matched, but flagged: the record IS "found" and the ID IS applied...
    assert record.status == "found"
    assert record.verified_simkl_id == "42"
    assert record.input_simkl_id == "42"
    assert record.reason
    # ...but it must show as "pending" (yellow, needs a human look), not "found" (green).
    assert record.visual_status() == "pending"


def test_apply_lookup_result_confident_match_is_not_flagged_for_review():
    records = extract_media_records({"shows": [{"status": "watching", "watched_episodes_count": 2, "is_rewatch": False, "show": {"title": "Breaking Bad"}}], "anime": [], "movies": []})
    record = records[0]

    apply_lookup_result(record, LookupResult(status="found", simkl_id=1, simkl_type="tv", title="Breaking Bad", confidence=95))

    assert record.visual_status() == "found"


def test_lookup_record_falls_back_to_title_search_when_imdb_has_no_match():
    records = extract_media_records({"shows": [{"status": "watching", "watched_episodes_count": 1, "is_rewatch": False, "show": {"title": "Some Obscure Show"}}], "anime": [], "movies": []})
    record = records[0]

    imdb_client = FakeImdbClient([])  # no IMDb candidates at all
    tvmaze_client = FakeTvMazeClient(None)
    simkl_client = FakeSimklClient(search_result=LookupResult(status="found", simkl_id=42, simkl_type="tv", title="Some Obscure Show"))

    result = asyncio.run(lookup_record(simkl_client, imdb_client, tvmaze_client, record))

    assert result.status == "found"
    assert result.simkl_id == 42
    assert simkl_client.calls == [("title_search", "Some Obscure Show")]


def test_lookup_record_falls_back_when_simkl_rejects_the_imdb_id():
    records = extract_media_records({"shows": [{"status": "watching", "watched_episodes_count": 1, "is_rewatch": False, "show": {"title": "9-1-1"}}], "anime": [], "movies": []})
    record = records[0]

    imdb_client = FakeImdbClient([ImdbCandidate(imdb_id="tt7235466", title="9-1-1", year=None, category="tvSeries", rank=478)])
    tvmaze_client = FakeTvMazeClient(None)
    simkl_client = FakeSimklClient(
        external_id_result=LookupResult(status="not_found", reason="id_not_found"),
        search_result=LookupResult(status="found", simkl_id=7, simkl_type="tv", title="9-1-1"),
    )

    result = asyncio.run(lookup_record(simkl_client, imdb_client, tvmaze_client, record))

    assert result.status == "found"
    assert result.simkl_id == 7
    assert simkl_client.calls == [("external_id", "imdb", "tt7235466"), ("title_search", "9-1-1")]


def test_lookup_record_retries_title_search_with_imdb_title_when_it_differs():
    """Regression test for e.g. TV Time's "El Camino: A Breaking Bad Movie" vs
    IMDb's canonical "El Camino": when SIMKL doesn't recognize the resolved
    IMDb ID directly, retry SIMKL's title search using IMDb's own (often
    shorter) title/year before giving up to the original TV Time title.
    """
    records = extract_media_records({"shows": [], "anime": [], "movies": [
        {"status": "completed", "watched_episodes_count": 1, "is_rewatch": False, "movie": {"title": "El Camino: A Breaking Bad Movie", "year": 2019}},
    ]})
    record = records[0]

    imdb_client = FakeImdbClient([ImdbCandidate(imdb_id="tt9243946", title="El Camino", year=2019, category="movie", rank=2103)])
    tvmaze_client = FakeTvMazeClient(None)
    simkl_client = FakeSimklClient(
        external_id_result=LookupResult(status="not_found", reason="id_not_found"),
        search_results=[
            LookupResult(status="found", simkl_id=555, simkl_type="movie", title="El Camino"),
        ],
    )

    result = asyncio.run(lookup_record(simkl_client, imdb_client, tvmaze_client, record))

    assert result.status == "found"
    assert result.simkl_id == 555
    assert result.source == "imdb_title"
    # The retry must use IMDb's title ("El Camino"), not TV Time's full subtitled title.
    assert simkl_client.calls == [("external_id", "imdb", "tt9243946"), ("title_search", "El Camino")]


def test_lookup_record_falls_back_to_original_title_when_imdb_title_search_also_fails():
    records = extract_media_records({"shows": [], "anime": [], "movies": [
        {"status": "completed", "watched_episodes_count": 1, "is_rewatch": False, "movie": {"title": "El Camino: A Breaking Bad Movie", "year": 2019}},
    ]})
    record = records[0]

    imdb_client = FakeImdbClient([ImdbCandidate(imdb_id="tt9243946", title="El Camino", year=2019, category="movie", rank=2103)])
    tvmaze_client = FakeTvMazeClient(None)
    simkl_client = FakeSimklClient(
        external_id_result=LookupResult(status="not_found", reason="id_not_found"),
        search_results=[
            LookupResult(status="not_found", reason="no_match"),
            LookupResult(status="found", simkl_id=777, simkl_type="movie", title="El Camino: A Breaking Bad Movie"),
        ],
    )

    result = asyncio.run(lookup_record(simkl_client, imdb_client, tvmaze_client, record))

    assert result.status == "found"
    assert result.simkl_id == 777
    assert simkl_client.calls == [
        ("external_id", "imdb", "tt9243946"),
        ("title_search", "El Camino"),
        ("title_search", "El Camino: A Breaking Bad Movie"),
    ]


def test_lookup_record_skips_imdb_title_retry_when_titles_and_years_match():
    records = extract_media_records({"shows": [], "anime": [], "movies": [
        {"status": "completed", "watched_episodes_count": 1, "is_rewatch": False, "movie": {"title": "Inception", "year": 2010}},
    ]})
    record = records[0]

    imdb_client = FakeImdbClient([ImdbCandidate(imdb_id="tt1375666", title="Inception", year=2010, category="movie", rank=1)])
    tvmaze_client = FakeTvMazeClient(None)
    simkl_client = FakeSimklClient(
        external_id_result=LookupResult(status="not_found", reason="id_not_found"),
        search_result=LookupResult(status="found", simkl_id=1, simkl_type="movie", title="Inception"),
    )

    result = asyncio.run(lookup_record(simkl_client, imdb_client, tvmaze_client, record))

    assert result.status == "found"
    # Only one title-search call: no point retrying with an identical title/year.
    assert simkl_client.calls == [("external_id", "imdb", "tt1375666"), ("title_search", "Inception")]


def test_lookup_record_prioritizes_anime_type_when_tvmaze_tags_it_as_anime():
    records = extract_media_records({"shows": [{"status": "watching", "watched_episodes_count": 1, "is_rewatch": False, "show": {"title": "Naruto"}}], "anime": [], "movies": []})
    record = records[0]

    imdb_client = FakeImdbClient([ImdbCandidate(imdb_id="tt0409591", title="Naruto", year=2002, category="tvSeries", rank=988)])
    tvmaze_client = FakeTvMazeClient(TvMazeShow(tvmaze_id=495, name="Naruto", show_type="Animation", language="Japanese", genres=["Action", "Anime"]))
    simkl_client = FakeSimklClient(external_id_result=LookupResult(status="found", simkl_id=321, simkl_type="anime", title="Naruto", confidence=100))

    result = asyncio.run(lookup_record(simkl_client, imdb_client, tvmaze_client, record))

    assert result.status == "found"
    # "anime" must be tried before "tv" once TVmaze confirms the genre.
    assert simkl_client.preferred_types_seen == [["anime", "tv"]]


def test_lookup_record_does_not_reorder_types_for_non_anime_shows():
    records = extract_media_records({"shows": [{"status": "watching", "watched_episodes_count": 1, "is_rewatch": False, "show": {"title": "Breaking Bad"}}], "anime": [], "movies": []})
    record = records[0]

    imdb_client = FakeImdbClient([ImdbCandidate(imdb_id="tt0903747", title="Breaking Bad", year=2008, category="tvSeries", rank=100)])
    tvmaze_client = FakeTvMazeClient(TvMazeShow(tvmaze_id=169, name="Breaking Bad", show_type="Scripted", language="English", genres=["Drama", "Crime"]))
    simkl_client = FakeSimklClient(external_id_result=LookupResult(status="found", simkl_id=1, simkl_type="tv", title="Breaking Bad"))

    asyncio.run(lookup_record(simkl_client, imdb_client, tvmaze_client, record))

    assert simkl_client.preferred_types_seen == [["tv", "anime"]]


def test_lookup_record_skips_tvmaze_for_movies():
    records = extract_media_records({"shows": [], "anime": [], "movies": [{"status": "completed", "watched_episodes_count": 1, "is_rewatch": False, "movie": {"title": "Your Name", "year": 2016}}]})
    record = records[0]

    imdb_client = FakeImdbClient([ImdbCandidate(imdb_id="tt5311514", title="Your Name", year=2016, category="movie", rank=1)])

    class ExplodingTvMazeClient:
        async def lookup_by_imdb(self, _imdb_id):
            raise AssertionError("TVmaze should not be consulted for movies")

    simkl_client = FakeSimklClient(external_id_result=LookupResult(status="found", simkl_id=5, simkl_type="movie", title="Your Name"))

    result = asyncio.run(lookup_record(simkl_client, imdb_client, ExplodingTvMazeClient(), record))

    assert result.status == "found"
