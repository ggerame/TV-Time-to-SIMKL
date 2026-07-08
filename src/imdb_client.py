"""Client for IMDb's public title-suggestion endpoint.

SIMKL's own free-text search sometimes fails to find shows/movies whose
titles are mostly numbers or punctuation (e.g. "9-1-1", "The 100", "1899"),
even though the title/year are typed correctly. IMDb's search is far more
reliable for these, so we resolve an IMDb ID from the title first and then
ask SIMKL for the item with that exact IMDb ID - a precise ID lookup instead
of a fuzzy title search.

This uses IMDb's undocumented "suggestion" endpoint (the same one that
powers the search box on imdb.com). It has no official API contract, so
every call here is best-effort: any failure or unexpected response simply
yields no candidates, and the caller falls back to SIMKL's own title search.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .simkl_client import normalize_title

DEFAULT_BASE_URL = "https://v3.sg.media-imdb.com/suggestion/x"
DEFAULT_TIMEOUT_MS = 8000
DEFAULT_MIN_DELAY_MS = 50

#: Maps IMDb's "qid" title category to the "tv" / "movie" domain used elsewhere
#: in this app. Categories that aren't a show or a movie (episodes, videos,
#: games, ...) are intentionally left out and treated as non-candidates.
CATEGORY_TO_TYPE = {
    "movie": "movie",
    "tvMovie": "movie",
    "short": "movie",
    "tvShort": "movie",
    "tvSeries": "tv",
    "tvMiniSeries": "tv",
    "tvSpecial": "tv",
}

#: Minimum combined score (see `find_best_match`) required to trust an IMDb match.
MIN_MATCH_SCORE = 80


@dataclass
class ImdbCandidate:
    """One IMDb title-suggestion result."""

    imdb_id: str
    title: str
    year: Optional[int]
    category: str  # raw IMDb "qid", e.g. "tvSeries", "movie"
    rank: Optional[int]  # lower is more popular; used only as a tiebreaker
    #: True when this candidate was picked by trusting IMDb's own search
    #: ranking rather than a confident text match (see `find_best_match`'s
    #: non-Latin fallback) - callers should flag such matches for review.
    needs_review: bool = False


def parse_suggestion_response(data: dict[str, Any]) -> list[ImdbCandidate]:
    """Turn a raw IMDb suggestion JSON response into a list of candidates."""
    candidates: list[ImdbCandidate] = []
    for item in data.get("d") or []:
        imdb_id = str(item.get("id") or "")
        if not imdb_id.startswith("tt"):
            continue  # skip name/company suggestions, keep titles only
        candidates.append(ImdbCandidate(
            imdb_id=imdb_id,
            title=str(item.get("l") or ""),
            year=item.get("y"),
            category=str(item.get("qid") or ""),
            rank=item.get("rank"),
        ))
    return candidates


def _has_latin_letters(text: str) -> bool:
    return any("a" <= ch <= "z" for ch in text)


def find_best_match(
    candidates: list[ImdbCandidate], title: str, year: Optional[int], preferred_types: list[str],
) -> Optional[ImdbCandidate]:
    """Pick the most likely candidate for a given title/year, or None if unsure.

    Requires at least an exact (normalized) or containing title match; year
    proximity and IMDb's own popularity rank are used to disambiguate
    between multiple same-titled entries (e.g. remakes, foreign versions).

    IMDb's search is smart about resolving titles written in a non-Latin
    script (Kanji, Hangul, etc.) to the right entry, but its candidate
    titles are usually in English/romaji - so a title written in, say,
    Korean can never match any candidate by text comparison alone, even
    though IMDb's own top result is exactly right. For queries with no
    Latin letters at all, we fall back to trusting IMDb's own top-ranked
    candidate (its response is already in relevance order for that query),
    as long as its category is usable and it isn't wildly off on year.
    """
    normalized_query = normalize_title(title)
    if not normalized_query:
        return None

    scored: list[tuple[float, int, ImdbCandidate]] = []
    for candidate in candidates:
        mapped_type = CATEGORY_TO_TYPE.get(candidate.category)
        if mapped_type is None:
            continue

        normalized_candidate = normalize_title(candidate.title)
        if normalized_candidate == normalized_query:
            title_score = 100.0
        elif normalized_query in normalized_candidate or normalized_candidate in normalized_query:
            title_score = 80.0
        else:
            continue

        year_score = 0.0
        if year and candidate.year:
            diff = abs(year - candidate.year)
            year_score = 20.0 if diff == 0 else 10.0 if diff == 1 else 0.0 if diff <= 2 else -50.0

        type_score = 5.0 if mapped_type in preferred_types else 0.0
        total = title_score + year_score + type_score
        rank = candidate.rank if candidate.rank is not None else 10**9
        scored.append((total, rank, candidate))

    if scored:
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        best_score, _rank, best = scored[0]
        if best_score >= MIN_MATCH_SCORE:
            return best

    if candidates and not _has_latin_letters(normalized_query):
        for candidate in candidates:  # already in IMDb's own relevance order for this query
            mapped_type = CATEGORY_TO_TYPE.get(candidate.category)
            if mapped_type is None:
                continue
            if year and candidate.year and abs(year - candidate.year) > 2:
                continue
            return replace(candidate, needs_review=True)

    return None


class ImdbClient:
    """Minimal async client for IMDb's title-suggestion endpoint."""

    def __init__(
        self, *, base_url: str = DEFAULT_BASE_URL, timeout_ms: int = DEFAULT_TIMEOUT_MS, min_delay_ms: int = DEFAULT_MIN_DELAY_MS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_delay_ms = min_delay_ms
        self._last_request_at = 0.0
        self._http = httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; tvtime-to-simkl-pyapp/1.0)",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "ImdbClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def _wait_turn(self) -> None:
        now = time.monotonic() * 1000
        wait_for = self._last_request_at + self.min_delay_ms - now
        if wait_for > 0:
            await asyncio.sleep(wait_for / 1000)
        self._last_request_at = time.monotonic() * 1000

    async def search(self, query: str) -> list[ImdbCandidate]:
        """Search IMDb titles by free text. Returns an empty list on any failure."""
        text = query.strip()
        if not text:
            return []

        await self._wait_turn()
        url = f"{self.base_url}/{quote(text)}.json"
        try:
            response = await self._http.get(url)
            if response.status_code != 200:
                return []
            return parse_suggestion_response(response.json())
        except (httpx.HTTPError, ValueError):
            # Network hiccups or unexpected/non-JSON responses: treat as "no candidates"
            # so the caller can fall back to SIMKL's own title search.
            return []
