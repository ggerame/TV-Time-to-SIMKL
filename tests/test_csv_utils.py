"""Unit tests for CSV parsing helpers."""
from __future__ import annotations

from src.csv_utils import (
    as_integer,
    clean_title,
    extract_trailing_number,
    is_valid_episode,
    normalize_date,
    normalize_key,
    parse_csv,
    year_from_date,
)


def test_parse_csv_basic():
    text = "a,b,c\n1,2,3\n4,5,6\n"
    result = parse_csv(text)
    assert result.headers == ["a", "b", "c"]
    assert result.rows == [{"a": "1", "b": "2", "c": "3"}, {"a": "4", "b": "5", "c": "6"}]
    assert result.warnings == []


def test_parse_csv_strips_bom():
    text = "\ufeffa,b\n1,2\n"
    result = parse_csv(text)
    assert result.headers == ["a", "b"]


def test_parse_csv_pads_short_rows_with_warning():
    text = "a,b,c\n1,2\n"
    result = parse_csv(text)
    assert result.rows == [{"a": "1", "b": "2", "c": ""}]
    assert len(result.warnings) == 1
    assert "padded" in result.warnings[0].reason


def test_parse_csv_truncates_long_rows_with_warning():
    text = "a,b\n1,2,3,4\n"
    result = parse_csv(text)
    assert result.rows[0]["a"] == "1"
    assert result.rows[0]["b"] == "2"
    assert result.rows[0]["_extra"] == ["3", "4"]
    assert "truncated" in result.warnings[0].reason


def test_clean_title_collapses_whitespace():
    assert clean_title("  Breaking   Bad  ") == "Breaking Bad"


def test_normalize_key_lowercases():
    assert normalize_key("Breaking Bad") == "breaking bad"


def test_as_integer():
    assert as_integer("42") == 42
    assert as_integer("-3") == -3
    assert as_integer("abc") is None
    assert as_integer("") is None


def test_is_valid_episode():
    assert is_valid_episode("Title", 1, 2, "2020-01-01T00:00:00Z") is True
    assert is_valid_episode("", 1, 2, "2020-01-01T00:00:00Z") is False
    assert is_valid_episode("Title", None, 2, "2020-01-01T00:00:00Z") is False
    assert is_valid_episode("Title", 1, 0, "2020-01-01T00:00:00Z") is False


def test_normalize_date_variants():
    assert normalize_date("2020-01-15 10:30:00") == "2020-01-15T10:30:00Z"
    assert normalize_date("2020-01-15") == "2020-01-15T00:00:00Z"
    assert normalize_date("") is None


def test_year_from_date():
    assert year_from_date("2015-06-01") == 2015
    assert year_from_date("") is None


def test_extract_trailing_number():
    assert extract_trailing_number("rewatch-episode-3") == 3
    assert extract_trailing_number("no-number") is None
