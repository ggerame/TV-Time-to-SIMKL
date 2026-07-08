"""Asynchronous SIMKL API client used to match TV Time titles to SIMKL IDs.

It implements:

- Rate limiting (a minimum delay between requests).
- Retry with exponential backoff on HTTP 429 / 5xx / timeouts.
- Title/year based search with a simple scoring heuristic to decide whether
  a search result is a confident match.
- Lookups by SIMKL ID, IMDb ID and TVDB ID (via SIMKL's ``/redirect`` and
  ``/search/id`` endpoints).
"""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

import httpx

if TYPE_CHECKING:
    from .records import MediaRecord

DEFAULT_APP_NAME = "tvtime-to-simkl-pyapp"
DEFAULT_APP_VERSION = "1.0.0"
DEFAULT_BASE_URL = "https://api.simkl.com"

#: Maps our internal SIMKL "type" name to its API path segment.
TYPE_ENDPOINTS = {"movie": "/movies", "tv": "/tv", "anime": "/anime"}

_ARTICLE_RE = re.compile(r"^(the|a|an)\s+")
#: Strips punctuation/symbols while keeping letters and digits from *any* script
#: (Kanji, Hangul, Cyrillic, etc.) - Python's `\w` is Unicode-aware by default
#: for `str` patterns, unlike a hand-rolled `[a-z0-9]` class which would wipe
#: out non-Latin titles entirely (e.g. Korean/Japanese-only titles).
_NON_WORD_RE = re.compile(r"[^\w ]+", re.UNICODE)
_MULTI_SPACE_RE = re.compile(r"\s+")
_LOCATION_RE = re.compile(r"/(tv|anime|movies?|movie)/(\d+)", re.IGNORECASE)


@dataclass
class LookupResult:
    """Outcome of any SIMKL lookup (search, redirect, ID, or external ID)."""

    status: str  # "found" | "not_found"
    source: str = "none"
    reason: str = ""
    simkl_id: Optional[int] = None
    simkl_type: Optional[str] = None
    title: str = ""
    year: Optional[int] = None
    imdb_id: str = ""
    tvdb_id: str = ""
    confidence: Optional[int] = None
    url: str = ""
    type_verified: bool = False
    type_verified_by: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    field_errors: dict[str, str] = field(default_factory=dict)
    #: True when this match was accepted on a heuristic (e.g. trusting IMDb's
    #: top result for a title with no Latin letters) rather than a confident
    #: text/ID match, so the review UI should flag it for a human to double-check.
    needs_review: bool = False


def normalize_title(value: Any) -> str:
    """Heavily normalize a title for fuzzy matching (accents, articles, punctuation).

    Keeps letters/digits from any script (Kanji, Hangul, Cyrillic, ...), not
    just ASCII, so non-Latin titles still normalize to something distinctive
    instead of an empty string (which would make every such title collide).
    """
    text = str(value or "")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")  # strip accents
    text = text.lower()
    text = text.replace("&", " and ")
    text = text.replace("'", "")
    text = _NON_WORD_RE.sub(" ", text)
    text = _ARTICLE_RE.sub("", text.strip())
    text = _MULTI_SPACE_RE.sub(" ", text).strip()
    return text


#: Matches an IMDb title ID anywhere in a string, so a pasted IMDb URL
#: (e.g. "https://www.imdb.com/title/tt5688996/" or the locale-prefixed
#: "https://www.imdb.com/it/title/tt5688996/" you get from Google results)
#: still yields a clean ID instead of being rejected outright.
_IMDB_ID_RE = re.compile(r"(tt\d{5,12})")


def clean_imdb_id(value: Any) -> str:
    """Extract a clean ``tt1234567``-style IMDb ID from a bare ID or a full IMDb URL."""
    text = str(value or "").strip().lower()
    if re.fullmatch(r"tt\d{5,12}", text):
        return text
    match = _IMDB_ID_RE.search(text)
    return match.group(1) if match else ""


def clean_numeric_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d+", text) else ""


def normalize_simkl_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("movie", "movies"):
        return "movie"
    if text == "anime":
        return "anime"
    return "tv"


def parse_simkl_location(location: str) -> tuple[Optional[int], Optional[str]]:
    """Parse a SIMKL ``/redirect`` response's Location header."""
    match = _LOCATION_RE.search(location or "")
    if not match:
        return None, None
    return int(match.group(2)), normalize_simkl_type(match.group(1))


def get_title(item: dict[str, Any]) -> str:
    for key in ("title", "name", "en_title", "original_title"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def get_year(item: dict[str, Any]) -> Optional[int]:
    year = item.get("year")
    if isinstance(year, int):
        return year
    for key in ("release_date", "first_aired", "date"):
        value = str(item.get(key) or "")
        match = re.match(r"^(\d{4})", value)
        if match:
            return int(match.group(1))
    return None


def get_simkl_id(item: dict[str, Any]) -> Optional[int]:
    ids = item.get("ids") or {}
    for value in (ids.get("simkl"), ids.get("simkl_id"), item.get("simkl_id"), item.get("id")):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def get_imdb_id(item: dict[str, Any]) -> str:
    ids = item.get("ids") or {}
    for value in (ids.get("imdb"), ids.get("imdb_id"), item.get("imdb_id"), item.get("imdb")):
        cleaned = clean_imdb_id(value)
        if cleaned:
            return cleaned
    return ""


def get_tvdb_id(item: dict[str, Any]) -> str:
    ids = item.get("ids") or {}
    for value in (ids.get("tvdb"), ids.get("tvdb_id"), item.get("tvdb_id"), item.get("tvdb")):
        cleaned = clean_numeric_id(value)
        if cleaned:
            return cleaned
    return ""


def get_item_simkl_type(item: dict[str, Any], fallback: str) -> str:
    for key in ("type", "media_type", "kind"):
        value = item.get(key)
        if value:
            return normalize_simkl_type(value)
    return normalize_simkl_type(fallback)


def build_search_query(record: "MediaRecord") -> str:
    return f"{record.title} {record.year}".strip() if record.year else record.title


def query_types_for_record(record: "MediaRecord") -> list[str]:
    """Return the SIMKL type(s) to try, preferring the record's known type."""
    if record.source_type == "movie":
        return ["movie"]
    if record.source_type == "anime":
        return ["anime", "tv"]
    return ["tv", "anime"]


def score_candidate(record: "MediaRecord", item: dict[str, Any], simkl_type: str) -> float:
    """Score how well a search result matches a media record (0-100)."""
    record_title = normalize_title(record.title)
    item_title = normalize_title(get_title(item))

    if record_title and item_title == record_title:
        title_score = 78.0
    elif record_title and item_title and (record_title in item_title or item_title in record_title):
        title_score = 62.0
    else:
        record_words = set(record_title.split())
        item_words = set(item_title.split())
        max_words = max(len(record_words), len(item_words), 1)
        overlap = len(record_words & item_words)
        title_score = (overlap / max_words) * 60.0

    item_year = get_year(item)
    if record.year and item_year:
        diff = abs(record.year - item_year)
        year_score = 20.0 if diff == 0 else 12.0 if diff == 1 else 6.0 if diff <= 2 else 0.0
    else:
        year_score = 5.0

    preferred_types = query_types_for_record(record)
    type_score = 2.0 if simkl_type in preferred_types else 0.0

    return title_score + year_score + type_score


class SimklApiError(Exception):
    """Raised when a SIMKL request fails after all retries are exhausted."""


class SimklClient:
    """Thin async HTTP client for the parts of the SIMKL API this tool needs."""

    def __init__(
        self,
        client_id: str,
        *,
        app_name: str = DEFAULT_APP_NAME,
        app_version: str = DEFAULT_APP_VERSION,
        base_url: str = DEFAULT_BASE_URL,
        min_delay_ms: int = 110,
        timeout_ms: int = 20000,
        on_retry: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        if not client_id:
            raise ValueError("Missing SIMKL client_id.")
        self.client_id = client_id
        self.app_name = app_name
        self.app_version = app_version
        self.base_url = base_url.rstrip("/")
        self.min_delay_ms = min_delay_ms
        self.timeout_ms = timeout_ms
        self.on_retry = on_retry
        self._last_request_at = 0.0
        self._id_cache: dict[int, LookupResult] = {}
        self._http = httpx.AsyncClient(
            timeout=None if timeout_ms <= 0 else timeout_ms / 1000,
            headers={"User-Agent": f"{app_name}/{app_version}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "SimklClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    # -- low level request handling ---------------------------------------

    async def _wait_turn(self) -> None:
        now = time.monotonic() * 1000
        wait_for = self._last_request_at + self.min_delay_ms - now
        if wait_for > 0:
            await asyncio.sleep(wait_for / 1000)
        self._last_request_at = time.monotonic() * 1000

    async def _request(
        self, path: str, params: dict[str, Any], *, follow_redirects: bool = True, accept_json: bool = True,
    ) -> httpx.Response:
        query = {
            **{k: v for k, v in params.items() if v not in (None, "")},
            "client_id": self.client_id,
            "app-name": self.app_name,
            "app-version": self.app_version,
        }

        delay_seconds = 1.0
        for attempt in range(1, 6):
            await self._wait_turn()
            try:
                response = await self._http.get(
                    f"{self.base_url}{path}",
                    params=query,
                    follow_redirects=follow_redirects,
                    headers={"Accept": "application/json" if accept_json else "*/*"},
                )
            except httpx.TimeoutException as exc:
                raise SimklApiError(f"SIMKL timeout at {path}") from exc

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 5:
                    raise SimklApiError(f"SIMKL request to {path} failed with status {response.status_code}")
                if self.on_retry:
                    self.on_retry({"status": response.status_code, "path": path, "attempt": attempt, "delay_ms": delay_seconds * 1000})
                await asyncio.sleep(delay_seconds)
                delay_seconds *= 2
                continue

            return response

        raise SimklApiError(f"SIMKL request to {path} failed after retries")

    # -- search / redirect --------------------------------------------------

    async def search_by_type(self, record: "MediaRecord", simkl_type: str) -> list[dict[str, Any]]:
        response = await self._request(f"/search/{simkl_type}", {"q": build_search_query(record), "extended": "full"})
        if response.status_code != 200:
            return []
        data = response.json()
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) and "error" not in data else []
        candidates = []
        for item in rows:
            resolved_type = get_item_simkl_type(item, simkl_type)
            candidates.append({
                "simkl_id": get_simkl_id(item),
                "simkl_type": resolved_type,
                "title": get_title(item),
                "year": get_year(item),
                "url": item.get("url") or item.get("simkl_url") or "",
                "score": score_candidate(record, item, resolved_type),
            })
        return candidates

    async def resolve_by_redirect(self, record: "MediaRecord", simkl_type: str) -> LookupResult:
        params = {"to": "simkl", "type": simkl_type, "title": record.title}
        if record.year:
            params["year"] = str(record.year)
        response = await self._request("/redirect", params, follow_redirects=False, accept_json=False)
        location = response.headers.get("location", "")
        simkl_id, parsed_type = parse_simkl_location(location)
        if not simkl_id:
            return LookupResult(status="not_found", source="redirect")

        details = await self.lookup_by_id(simkl_id, [parsed_type or simkl_type], record)
        if details.status == "found":
            details.source = "redirect"
            details.confidence = 96
            details.url = location or details.url
            return details
        return LookupResult(
            status="found", source="redirect", simkl_id=simkl_id, simkl_type=parsed_type or simkl_type,
            confidence=88, url=location,
        )

    async def lookup_by_id(self, simkl_id: Any, preferred_types: list[str], context_record: "MediaRecord | None" = None) -> LookupResult:
        try:
            resolved_id = int(str(simkl_id).strip())
        except (TypeError, ValueError):
            return LookupResult(status="not_found", reason="invalid_id")
        if resolved_id <= 0:
            return LookupResult(status="not_found", reason="invalid_id")

        cached = self._id_cache.get(resolved_id)
        if cached:
            return cached

        types = list(dict.fromkeys([*(preferred_types or []), "tv", "movie", "anime"]))
        for simkl_type in types:
            endpoint = TYPE_ENDPOINTS.get(simkl_type)
            if not endpoint:
                continue
            response = await self._request(f"{endpoint}/{resolved_id}", {"extended": "full"})
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                continue
            item = response.json()
            if not isinstance(item, dict) or item.get("error") or not (get_simkl_id(item) or get_title(item)):
                continue

            resolved_type = get_item_simkl_type(item, simkl_type)
            confidence = 100
            if context_record is not None:
                confidence = min(100, max(70, round(score_candidate(context_record, item, resolved_type))))

            result = LookupResult(
                status="found", source="id", simkl_id=get_simkl_id(item) or resolved_id, simkl_type=resolved_type,
                title=get_title(item), year=get_year(item), imdb_id=get_imdb_id(item), tvdb_id=get_tvdb_id(item),
                confidence=confidence, url=item.get("url") or item.get("simkl_url") or "",
                type_verified=True, type_verified_by="api_type",
            )
            self._id_cache[resolved_id] = result
            return result

        return LookupResult(status="not_found", reason="id_not_found")

    async def lookup_by_external_id(
        self, kind: str, value: str, preferred_types: list[str], context_record: "MediaRecord | None" = None,
    ) -> LookupResult:
        redirected = await self._lookup_external_id_by_redirect(kind, value, preferred_types, context_record)
        if redirected.status == "found":
            return redirected
        return await self._lookup_external_id_by_search(kind, value, preferred_types, context_record)

    async def _lookup_external_id_by_redirect(
        self, kind: str, value: str, preferred_types: list[str], context_record: "MediaRecord | None",
    ) -> LookupResult:
        params: dict[str, Any] = {kind: value, "to": "simkl"}
        if context_record is not None:
            record_types = query_types_for_record(context_record)
            if record_types:
                params["type"] = record_types[0]

        response = await self._request("/redirect", params, follow_redirects=False, accept_json=False)
        location = response.headers.get("location", "")
        simkl_id, parsed_type = parse_simkl_location(location)
        if not simkl_id:
            return LookupResult(status="not_found", reason=f"{kind}_redirect_not_found")

        detail_types = list(dict.fromkeys([t for t in (parsed_type, *preferred_types) if t]))
        canonical = await self.lookup_by_id(simkl_id, detail_types, context_record)
        if canonical.status == "found":
            canonical.source = kind
            canonical.url = location or canonical.url
            canonical.type_verified = True
            canonical.type_verified_by = "external_redirect"
            return canonical

        return LookupResult(
            status="found", source=kind, simkl_id=simkl_id, simkl_type=parsed_type or normalize_simkl_type(kind),
            imdb_id=value if kind == "imdb" else "", tvdb_id=value if kind == "tvdb" else "",
            confidence=100, url=location, type_verified=True, type_verified_by="external_redirect",
        )

    async def _lookup_external_id_by_search(
        self, kind: str, value: str, preferred_types: list[str], context_record: "MediaRecord | None",
    ) -> LookupResult:
        response = await self._request("/search/id", {kind: value})
        if response.status_code != 200:
            return LookupResult(status="not_found", reason=f"{kind}_not_found")

        data = response.json()
        rows = data if isinstance(data, list) else []
        candidates = []
        for item in rows:
            resolved_type = get_item_simkl_type(item, "tv")
            candidates.append({
                "simkl_id": get_simkl_id(item), "simkl_type": resolved_type, "title": get_title(item),
                "year": get_year(item), "imdb_id": get_imdb_id(item), "tvdb_id": get_tvdb_id(item),
                "score": score_candidate(context_record, item, resolved_type) if context_record else 100,
            })
        candidates = [c for c in candidates if c["simkl_id"]]
        candidates.sort(key=lambda c: (c["simkl_type"] in (preferred_types or []), c["score"]), reverse=True)

        if not candidates:
            return LookupResult(status="not_found", reason=f"{kind}_not_found")

        best = candidates[0]
        canonical = await self.lookup_by_id(best["simkl_id"], [best["simkl_type"]], context_record)
        if canonical.status == "found":
            canonical.source = kind
            return canonical

        return LookupResult(
            status="found", source=kind, simkl_id=best["simkl_id"], simkl_type=best["simkl_type"],
            title=best["title"], year=best["year"], imdb_id=best["imdb_id"] or (value if kind == "imdb" else ""),
            tvdb_id=best["tvdb_id"] or (value if kind == "tvdb" else ""), confidence=100,
            type_verified=True, type_verified_by="search_id",
        )

    async def lookup_by_external_ids(
        self, imdb_id: str, tvdb_id: str, preferred_types: list[str], context_record: "MediaRecord | None" = None,
    ) -> LookupResult:
        lookups: list[LookupResult] = []
        if imdb_id:
            lookups.append(await self.lookup_by_external_id("imdb", imdb_id, preferred_types, context_record))
        if tvdb_id:
            lookups.append(await self.lookup_by_external_id("tvdb", tvdb_id, preferred_types, context_record))

        found = [lookup for lookup in lookups if lookup.status == "found" and lookup.simkl_id]
        if not found:
            return LookupResult(status="not_found", reason="external_ids_not_found")

        first = found[0]
        mismatch = next((lookup for lookup in found if lookup.simkl_id != first.simkl_id), None)
        if mismatch:
            field_errors: dict[str, str] = {}
            if imdb_id:
                field_errors["imdb_id"] = "IMDb ID points to a different SIMKL item."
            if tvdb_id:
                field_errors["tvdb_id"] = "TVDB ID points to a different SIMKL item."
            return LookupResult(status="not_found", reason="external_ids_mismatch", field_errors=field_errors)

        return first

    # -- high level enrichment ----------------------------------------------

    async def enrich_media_record(self, record: "MediaRecord") -> LookupResult:
        """Try to find the best SIMKL match for a record with no known IDs."""
        query_types = query_types_for_record(record)

        for simkl_type in query_types:
            redirected = await self.resolve_by_redirect(record, simkl_type)
            if redirected.status == "found":
                return redirected

        candidates: list[dict[str, Any]] = []
        for simkl_type in query_types:
            candidates.extend(await self.search_by_type(record, simkl_type))

        candidates = [c for c in candidates if c["simkl_id"]]
        candidates.sort(key=lambda c: c["score"], reverse=True)

        best = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        if best and best["score"] >= 85 and (not second or best["score"] - second["score"] >= 8):
            canonical = await self.lookup_by_id(best["simkl_id"], [best["simkl_type"]], record)
            if canonical.status == "found":
                canonical.source = "search"
                canonical.confidence = min(100, round(best["score"]))
                canonical.candidates = candidates[:5]
                return canonical
            return LookupResult(
                status="found", source="search", simkl_id=best["simkl_id"], simkl_type=best["simkl_type"],
                title=best["title"], year=best["year"], confidence=min(100, round(best["score"])),
                url=best["url"], candidates=candidates[:5],
            )

        return LookupResult(
            status="not_found",
            source="search" if candidates else "none",
            reason="ambiguous_or_low_confidence" if candidates else "no_match",
            candidates=candidates[:5],
        )
