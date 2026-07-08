"""Unit tests for the IMDb suggestion-endpoint parsing and matching logic.

These tests never hit the network: they only exercise the pure parsing
(`parse_suggestion_response`) and scoring (`find_best_match`) functions.
"""
from __future__ import annotations

from src.imdb_client import ImdbCandidate, find_best_match, parse_suggestion_response


def test_parse_suggestion_response_extracts_title_candidates():
    data = {
        "d": [
            {"id": "tt7235466", "l": "9-1-1", "q": "TV series", "qid": "tvSeries", "rank": 478, "y": 2018},
            {"id": "tt33550053", "l": "9-1-1: Nashville", "qid": "tvSeries", "rank": 2277, "y": 2025},
            {"id": "nm0000001", "l": "Some Actor"},  # not a title, should be skipped
        ],
    }
    candidates = parse_suggestion_response(data)
    assert len(candidates) == 2
    assert candidates[0] == ImdbCandidate(imdb_id="tt7235466", title="9-1-1", year=2018, category="tvSeries", rank=478)


def test_parse_suggestion_response_handles_missing_d_key():
    assert parse_suggestion_response({}) == []


def test_find_best_match_prefers_exact_title_and_year():
    candidates = [
        ImdbCandidate(imdb_id="tt7235466", title="9-1-1", year=2018, category="tvSeries", rank=478),
        ImdbCandidate(imdb_id="tt10323338", title="9-1-1: Lone Star", year=2020, category="tvSeries", rank=3310),
    ]
    best = find_best_match(candidates, "9-1-1", 2018, ["tv"])
    assert best is not None
    assert best.imdb_id == "tt7235466"


def test_find_best_match_uses_year_to_disambiguate_same_title():
    candidates = [
        ImdbCandidate(imdb_id="tt0091635", title="9\u00bd Weeks", year=1986, category="movie", rank=5061),
        ImdbCandidate(imdb_id="tt9999999", title="9\u00bd Weeks", year=2020, category="movie", rank=999999),
    ]
    best = find_best_match(candidates, "9\u00bd Weeks", 1986, ["movie"])
    assert best is not None
    assert best.imdb_id == "tt0091635"


def test_find_best_match_ignores_non_title_categories():
    candidates = [ImdbCandidate(imdb_id="tt1234567", title="Some Episode", year=2020, category="tvEpisode", rank=1)]
    assert find_best_match(candidates, "Some Episode", 2020, ["tv"]) is None


def test_find_best_match_returns_none_for_unrelated_titles():
    candidates = [ImdbCandidate(imdb_id="tt1234567", title="Completely Different Show", year=2020, category="tvSeries", rank=1)]
    assert find_best_match(candidates, "Breaking Bad", 2008, ["tv"]) is None


def test_find_best_match_returns_none_for_empty_candidates():
    assert find_best_match([], "Breaking Bad", 2008, ["tv"]) is None


def test_find_best_match_trusts_imdb_top_result_for_non_latin_query():
    """IMDb's own titles are usually English/romaji, so a native-script query
    (e.g. Korean/Japanese) can never text-match any candidate - but IMDb's own
    search already resolved it correctly, so we trust its top (most relevant)
    result instead of returning nothing.
    """
    candidates = [
        ImdbCandidate(imdb_id="tt10919420", title="Squid Game", year=2021, category="tvSeries", rank=581),
        ImdbCandidate(imdb_id="tt9999999", title="Some Unrelated Show", year=2021, category="tvSeries", rank=50000),
    ]
    best = find_best_match(candidates, "\uc624\uc9d5\uc5b4 \uac8c\uc784", 2021, ["tv"])
    assert best is not None
    assert best.imdb_id == "tt10919420"


def test_find_best_match_non_latin_fallback_skips_wrong_year():
    candidates = [ImdbCandidate(imdb_id="tt1111111", title="Old Remake", year=1990, category="movie", rank=1)]
    assert find_best_match(candidates, "\u9b3c\u6ec5\u306e\u5203", 2019, ["movie"]) is None


def test_find_best_match_non_latin_fallback_skips_non_title_categories():
    candidates = [ImdbCandidate(imdb_id="tt1111111", title="Some Clip", year=2019, category="video", rank=1)]
    assert find_best_match(candidates, "\u9b3c\u6ec5\u306e\u5203", 2019, ["tv"]) is None


def test_find_best_match_does_not_use_non_latin_fallback_for_latin_titles():
    """A Latin-script query that simply doesn't match anything should still
    return None - the non-Latin fallback must not kick in and pick a
    plausible-looking but wrong candidate.
    """
    candidates = [ImdbCandidate(imdb_id="tt1234567", title="Completely Different Show", year=2020, category="tvSeries", rank=1)]
    assert find_best_match(candidates, "Breaking Bad", 2008, ["tv"]) is None
