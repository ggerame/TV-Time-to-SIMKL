"""Client for TVmaze's free, keyless public API.

Used to improve anime detection: the TV Time export gives us no genre
information at all, and IMDb's title-suggestion endpoint (see
:mod:`imdb_client`) doesn't return genres either. TVmaze does, including an
explicit ``"Anime"`` genre tag, and lets us look a show up by its exact IMDb
ID (``/lookup/shows?imdb=...``) - no fuzzy title search involved, so once we
already have a confident IMDb match, the genre lookup is just as precise.

This is only useful for TV shows: TVmaze doesn't index movies.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = "https://api.tvmaze.com"
DEFAULT_TIMEOUT_MS = 8000
DEFAULT_MIN_DELAY_MS = 50


@dataclass
class TvMazeShow:
    """The subset of a TVmaze show record this app cares about."""

    tvmaze_id: int
    name: str
    show_type: str  # TVmaze's own "type", e.g. "Animation", "Scripted"
    language: str
    genres: list[str] = field(default_factory=list)

    def is_anime(self) -> bool:
        """Whether TVmaze classifies this show as anime.

        Primarily trusts TVmaze's own "Anime" genre tag; falls back to the
        Animation-type-plus-Japanese-language combination for older entries
        that predate that genre tag.
        """
        genres_lower = {genre.lower() for genre in self.genres}
        if "anime" in genres_lower:
            return True
        return self.show_type == "Animation" and self.language == "Japanese"


def parse_show(data: dict[str, Any]) -> Optional[TvMazeShow]:
    """Parse a TVmaze show JSON object, or return None if it's not usable."""
    show_id = data.get("id")
    if not isinstance(show_id, int):
        return None
    return TvMazeShow(
        tvmaze_id=show_id,
        name=str(data.get("name") or ""),
        show_type=str(data.get("type") or ""),
        language=str(data.get("language") or ""),
        genres=[str(genre) for genre in (data.get("genres") or [])],
    )


class TvMazeClient:
    """Minimal async client for the parts of the TVmaze API this app uses."""

    def __init__(
        self, *, base_url: str = DEFAULT_BASE_URL, timeout_ms: int = DEFAULT_TIMEOUT_MS, min_delay_ms: int = DEFAULT_MIN_DELAY_MS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_delay_ms = min_delay_ms
        self._last_request_at = 0.0
        self._http = httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "TvMazeClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def _wait_turn(self) -> None:
        now = time.monotonic() * 1000
        wait_for = self._last_request_at + self.min_delay_ms - now
        if wait_for > 0:
            await asyncio.sleep(wait_for / 1000)
        self._last_request_at = time.monotonic() * 1000

    async def lookup_by_imdb(self, imdb_id: str) -> Optional[TvMazeShow]:
        """Look up a show by its exact IMDb ID. Returns None on any failure or miss."""
        if not imdb_id:
            return None

        await self._wait_turn()
        try:
            response = await self._http.get(f"{self.base_url}/lookup/shows", params={"imdb": imdb_id})
            if response.status_code != 200:
                return None
            return parse_show(response.json())
        except (httpx.HTTPError, ValueError):
            # Network hiccups or unexpected/non-JSON responses: treat as "no data"
            # so the caller can continue without the genre hint.
            return None
