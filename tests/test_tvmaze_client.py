"""Unit tests for TVmaze response parsing and anime-genre detection.

These tests never hit the network: they only exercise the pure
`parse_show` function and `TvMazeShow.is_anime()` logic.
"""
from __future__ import annotations

from src.tvmaze_client import TvMazeShow, parse_show


def test_parse_show_extracts_expected_fields():
    data = {
        "id": 495, "name": "Naruto", "type": "Animation", "language": "Japanese",
        "genres": ["Action", "Adventure", "Anime", "Fantasy"],
    }
    show = parse_show(data)
    assert show == TvMazeShow(tvmaze_id=495, name="Naruto", show_type="Animation", language="Japanese", genres=["Action", "Adventure", "Anime", "Fantasy"])


def test_parse_show_returns_none_without_a_valid_id():
    assert parse_show({}) is None
    assert parse_show({"id": "not-an-int"}) is None


def test_is_anime_true_when_genre_tag_present():
    show = TvMazeShow(tvmaze_id=1, name="Naruto", show_type="Animation", language="Japanese", genres=["Action", "Anime"])
    assert show.is_anime() is True


def test_is_anime_true_for_animation_plus_japanese_without_explicit_tag():
    show = TvMazeShow(tvmaze_id=2, name="Old Anime", show_type="Animation", language="Japanese", genres=["Action"])
    assert show.is_anime() is True


def test_is_anime_false_for_western_animation():
    show = TvMazeShow(tvmaze_id=3, name="Rick and Morty", show_type="Animation", language="English", genres=["Comedy", "Science-Fiction"])
    assert show.is_anime() is False


def test_is_anime_false_for_live_action_show():
    show = TvMazeShow(tvmaze_id=4, name="One Piece", show_type="Scripted", language="English", genres=["Action", "Adventure", "Fantasy"])
    assert show.is_anime() is False
