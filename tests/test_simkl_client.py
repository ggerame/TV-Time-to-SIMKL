"""Unit tests for SIMKL matching/scoring helpers that don't require network access."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from src.simkl_client import SimklClient, clean_imdb_id, normalize_title, parse_simkl_location, score_candidate


@dataclass
class FakeRecord:
    title: str
    year: int | None
    source_type: str = "show"


def test_normalize_title_strips_articles_and_punctuation():
    assert normalize_title("The Walking Dead") == "walking dead"
    assert normalize_title("Am\u00e9lie") == "amelie"
    assert normalize_title("Tom & Jerry") == "tom and jerry"


def test_normalize_title_preserves_non_latin_scripts():
    """Regression test: titles written purely in Kanji/Hangul/etc. must not
    collapse to an empty string, or every such title would collide (and, for
    shows with no year, silently merge into the same record).
    """
    assert normalize_title("\u9b3c\u6ec5\u306e\u5203") == "\u9b3c\u6ec5\u306e\u5203"
    assert normalize_title("\uc9c4\uACA9\uc758 \uac70\uc778") != ""
    assert normalize_title("\uc9c4\uACA9\uc758 \uac70\uc778") != normalize_title("\uc624\uc9d5\uc5b4 \uac8c\uc784")
    assert normalize_title("Attack on Titan: \u9032\u6483\u306e\u5de8\u4eba") == "attack on titan \u9032\u6483\u306e\u5de8\u4eba"


def test_clean_imdb_id_accepts_bare_id():
    assert clean_imdb_id("tt5688996") == "tt5688996"
    assert clean_imdb_id("TT5688996") == "tt5688996"
    assert clean_imdb_id("  tt5688996  ") == "tt5688996"


def test_clean_imdb_id_accepts_full_imdb_urls():
    """Regression test: pasting a full IMDb URL copied from a Google result
    (with or without a locale segment like "/it/") must still extract the ID
    instead of being silently rejected.
    """
    assert clean_imdb_id("https://www.imdb.com/title/tt5688996/") == "tt5688996"
    assert clean_imdb_id("https://www.imdb.com/it/title/tt5688996/") == "tt5688996"
    assert clean_imdb_id("https://www.imdb.com/de/title/tt5688996/?ref_=fn_al_tt_1") == "tt5688996"
    assert clean_imdb_id("www.imdb.com/title/tt5688996") == "tt5688996"


def test_clean_imdb_id_rejects_garbage():
    assert clean_imdb_id("not an id at all") == ""
    assert clean_imdb_id("") == ""
    assert clean_imdb_id(None) == ""


def test_parse_simkl_location():
    simkl_id, simkl_type = parse_simkl_location("https://simkl.com/tv/12345")
    assert simkl_id == 12345
    assert simkl_type == "tv"


def test_parse_simkl_location_no_match():
    simkl_id, simkl_type = parse_simkl_location("")
    assert simkl_id is None
    assert simkl_type is None


def test_score_candidate_exact_match_scores_highly():
    record = FakeRecord(title="Breaking Bad", year=2008)
    item = {"title": "Breaking Bad", "year": 2008, "type": "tv"}
    score = score_candidate(record, item, "tv")
    assert score >= 95


def test_score_candidate_different_title_scores_low():
    record = FakeRecord(title="Breaking Bad", year=2008)
    item = {"title": "Better Call Saul", "year": 2015, "type": "tv"}
    score = score_candidate(record, item, "tv")
    assert score < 40


def test_pin_and_authenticated_sync_requests():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/pin":
            return httpx.Response(200, json={
                "result": "OK", "user_code": "ABCDE", "verification_uri": "https://simkl.com/pin",
            })
        if request.url.path == "/oauth/pin/ABCDE":
            return httpx.Response(200, json={"result": "OK", "access_token": "secret-token"})
        return httpx.Response(201, json={"added": {}, "not_found": {"shows": [], "movies": []}})

    async def scenario():
        client = SimklClient(
            "client-123", base_url="https://api.simkl.test", min_delay_ms=0,
            transport=httpx.MockTransport(handler),
        )
        try:
            pin = await client.request_pin()
            token = await client.check_pin(pin["user_code"])
            await client.post_user_data(
                "/sync/history", {"shows": [{"ids": {"simkl": 1}}]}, token,
                params={"skip_auto_watching": "yes"},
            )
        finally:
            await client.aclose()

    asyncio.run(scenario())

    sync_request = requests[-1]
    assert sync_request.method == "POST"
    assert sync_request.headers["Authorization"] == "Bearer secret-token"
    assert sync_request.headers["simkl-api-key"] == "client-123"
    assert sync_request.url.params["client_id"] == "client-123"
    assert sync_request.url.params["skip_auto_watching"] == "yes"


def test_authenticated_sync_debug_log_shows_serialized_body_without_credentials(caplog):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "empty_field"})

    async def scenario():
        client = SimklClient(
            "client-123", base_url="https://api.simkl.test", min_delay_ms=0,
            transport=httpx.MockTransport(handler),
        )
        try:
            try:
                await client.post_user_data(
                    "/sync/add-to-list",
                    {"shows": [{"to": "dropped", "ids": {"simkl": 1}}]},
                    "secret-token",
                )
            except Exception:
                pass
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG, logger="tvtime_simkl.simkl_client"):
        asyncio.run(scenario())

    log_text = caplog.text
    assert "payload_summary={'shows': 1, 'shows_to': ['dropped']}" in log_text
    assert 'body={"shows":[{"to":"dropped","ids":{"simkl":1}}]}' in log_text
    assert "status=400" in log_text
    assert "secret-token" not in log_text
    assert "client-123" not in log_text
