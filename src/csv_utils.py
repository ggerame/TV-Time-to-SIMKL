"""CSV parsing helpers for TV Time GDPR export files.

TV Time exports are plain CSV files, but real-world exports occasionally have
rows with a different number of fields than the header (extra/missing
commas). ``parse_csv`` is deliberately lenient: it pads/truncates malformed
rows instead of raising, and collects a warning for each affected row so the
caller can surface it in a report instead of silently losing history.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class CsvWarning:
    """A single row-level issue found while parsing a CSV file."""

    row: int
    reason: str


@dataclass
class ParsedCsv:
    """Result of parsing a CSV file: headers, row dictionaries and warnings."""

    headers: list[str]
    rows: list[dict[str, Any]]
    warnings: list[CsvWarning] = field(default_factory=list)


def parse_csv(text: str) -> ParsedCsv:
    """Parse CSV text into row dictionaries, tolerating malformed rows.

    - Strips a leading UTF-8 BOM if present.
    - Treats the first non-empty line as the header row.
    - Pads rows with fewer fields than the header with empty strings.
    - Truncates rows with more fields than the header, keeping the extra
      values under the ``_extra`` key and recording a warning.
    - Skips fully empty trailing rows.
    """
    if not text:
        return ParsedCsv(headers=[], rows=[])

    if text.startswith("\ufeff"):
        text = text[1:]

    reader = csv.reader(io.StringIO(text))
    raw_rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not raw_rows:
        return ParsedCsv(headers=[], rows=[])

    headers = [cell.strip() for cell in raw_rows[0]]
    rows: list[dict[str, Any]] = []
    warnings: list[CsvWarning] = []

    for row_number, raw_row in enumerate(raw_rows[1:], start=2):
        row = list(raw_row)
        expected = len(headers)
        actual = len(row)

        if actual < expected:
            row = row + [""] * (expected - actual)
            warnings.append(CsvWarning(
                row=row_number,
                reason=f"CSV row has {actual} fields, expected {expected} (padded)",
            ))
        elif actual > expected:
            extra = row[expected:]
            row = row[:expected]
            warnings.append(CsvWarning(
                row=row_number,
                reason=f"CSV row has {actual} fields, expected {expected} (truncated)",
            ))
            record = dict(zip(headers, row))
            record["_extra"] = extra
            rows.append(record)
            continue

        rows.append(dict(zip(headers, row)))

    return ParsedCsv(headers=headers, rows=rows, warnings=warnings)


def clean_title(value: Any) -> str:
    """Trim whitespace and collapse repeated spaces in a title."""
    text = str(value or "").strip()
    return re.sub(r"\s+", " ", text)


def normalize_key(value: Any) -> str:
    """Lowercase a cleaned title so it can be used as a grouping key."""
    return clean_title(value).lower()


def as_integer(value: Any) -> int | None:
    """Parse a value as an integer, returning None if it is not numeric."""
    text = str(value if value is not None else "").strip()
    if not re.fullmatch(r"-?\d+", text):
        return None
    return int(text)


def first_integer(*values: Any) -> int | None:
    """Return the first value that parses as an integer (any sign)."""
    for value in values:
        parsed = as_integer(value)
        if parsed is not None:
            return parsed
    return None


def first_positive_integer(*values: Any) -> int | None:
    """Return the first value that parses as an integer >= 1."""
    for value in values:
        parsed = as_integer(value)
        if parsed is not None and parsed >= 1:
            return parsed
    return None


def is_valid_episode(title: str, season: int | None, episode: int | None, watched_at: str | None) -> bool:
    """Check that a parsed episode row has the minimum required data."""
    return (
        bool(title)
        and season is not None
        and season >= 0
        and episode is not None
        and episode >= 1
        and bool(watched_at)
    )


_DATE_PATTERNS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
)


def normalize_date(value: Any) -> str | None:
    """Normalize a loosely-formatted TV Time date string to ISO 8601 (UTC)."""
    text = str(value or "").strip()
    if not text:
        return None

    # Already has an explicit timezone offset or 'Z'.
    iso_candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass

    for pattern in _DATE_PATTERNS:
        try:
            parsed = datetime.strptime(text, pattern)
            return parsed.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    return None


def year_from_date(value: Any) -> int | None:
    """Extract a leading 4-digit year from a date-like string."""
    match = re.match(r"^\s*(\d{4})", str(value or ""))
    return int(match.group(1)) if match else None


def extract_trailing_number(value: Any) -> int | None:
    """Extract a trailing integer suffix from a string (e.g. 'rewatch-3' -> 3)."""
    match = re.search(r"(\d+)\s*$", str(value or ""))
    return int(match.group(1)) if match else None
