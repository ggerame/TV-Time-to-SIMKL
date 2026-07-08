"""Core TV Time -> SIMKL conversion logic.

This module reads the CSV files bundled in a TV Time GDPR export ZIP and
turns them into the JSON structure expected by SIMKL's "import from JSON"
feature (``shows`` / ``anime`` / ``movies``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import csv_utils
from .csv_utils import (
    as_integer,
    clean_title,
    extract_trailing_number,
    first_integer,
    first_positive_integer,
    is_valid_episode,
    normalize_date,
    normalize_key,
    parse_csv,
)
from .zip_utils import read_zip_text_files

#: CSV files that carry watched-episode information, in the order they are
#: processed. "tracking-v2" is TV Time's modern unified export; the others
#: are older/alternate exports kept for backward compatibility.
EPISODE_SOURCES: list[tuple[str, str]] = [
    ("tracking-prod-records-v2.csv", "tracking-v2"),
    ("watched_on_episode.csv", "simple-watch"),
    ("seen_episode.csv", "simple-watch"),
    ("seen_episode_unitarian.csv", "simple-watch"),
]

#: Episode rating files. TV Time ratings have no SIMKL equivalent, so these
#: rows are only counted for the summary/report, never converted.
RATING_SOURCES = ["ratings-3-prod-episode_votes.csv", "ratings-live-votes.csv"]

#: All CSV files the converter looks for inside the uploaded ZIP.
ALL_SOURCE_FILES = sorted({
    *[name for name, _ in EPISODE_SOURCES],
    "rewatched_episode.csv",
    "followed_tv_show.csv",
    "user_tv_show_data.csv",
    "tracking-prod-records.csv",
    *RATING_SOURCES,
})

ProgressCallback = Callable[[str, int, int], None]


def _noop_progress(_phase: str, _done: int, _total: int) -> None:
    return None


@dataclass
class LoadedFile:
    """Parsed representation of one CSV file from the TV Time export."""

    filename: str
    exists: bool
    headers: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[csv_utils.CsvWarning] = field(default_factory=list)


def load_tvtime_data(zip_bytes: bytes) -> dict[str, LoadedFile]:
    """Extract and parse every recognised CSV file from a TV Time export ZIP."""
    texts = read_zip_text_files(zip_bytes, ALL_SOURCE_FILES)
    files: dict[str, LoadedFile] = {}

    for filename in ALL_SOURCE_FILES:
        text = texts.get(filename)
        if text is None:
            files[filename] = LoadedFile(filename=filename, exists=False)
            continue
        parsed = parse_csv(text)
        files[filename] = LoadedFile(
            filename=filename,
            exists=True,
            headers=parsed.headers,
            rows=parsed.rows,
            warnings=parsed.warnings,
        )

    return files


def estimate_work(loaded: dict[str, LoadedFile]) -> int:
    """Rough unit-of-work count used to drive the progress bar."""
    return sum(len(file.rows) for file in loaded.values()) + 1


# --------------------------------------------------------------------------
# Accumulators: in-progress show/movie state keyed by a normalized identity.
# --------------------------------------------------------------------------

@dataclass
class ShowAccumulator:
    title: str
    added_at: Optional[str] = None
    last_watched_at: Optional[str] = None
    rewatch_index: Optional[int] = None
    #: season number -> {episode number -> watched_at}
    seasons: dict[int, dict[int, str]] = field(default_factory=dict)

    @property
    def episode_count(self) -> int:
        return sum(len(episodes) for episodes in self.seasons.values())


@dataclass
class MovieAccumulator:
    title: str
    year: Optional[int] = None
    base_key: str = ""
    added_at: Optional[str] = None
    last_watched_at: Optional[str] = None
    rewatch_index: Optional[int] = None


def _get_show(shows: dict[str, ShowAccumulator], key: str, title: str) -> ShowAccumulator:
    show = shows.get(key)
    if show is None:
        show = ShowAccumulator(title=title)
        shows[key] = show
    return show


def _add_episode(show: ShowAccumulator, season: int, episode: int, watched_at: str) -> None:
    season_map = show.seasons.setdefault(season, {})
    existing = season_map.get(episode)
    # Keep the earliest watch date if the episode was recorded more than once.
    if existing is None or (watched_at and watched_at < existing):
        season_map[episode] = watched_at
    show.last_watched_at = _max_date(show.last_watched_at, watched_at)


def _max_date(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    if not candidate:
        return current
    if not current or candidate > current:
        return candidate
    return current


def _min_date(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    if not candidate:
        return current
    if not current or candidate < current:
        return candidate
    return current


def _movie_base_key(title: str, year: Optional[int]) -> str:
    return f"{normalize_key(title)}::{year or ''}"


def _get_movie(movies: dict[str, MovieAccumulator], key: str, title: str, year: Optional[int], base_key: str) -> MovieAccumulator:
    movie = movies.get(key)
    if movie is None:
        movie = MovieAccumulator(title=title, year=year, base_key=base_key)
        movies[key] = movie
    return movie


def _make_report(source: str, row: int, type_: str, reason: str, action: str) -> dict[str, Any]:
    return {"source": source, "row": row, "type": type_, "reason": reason, "action": action}


def _tracking_season_number(row: dict[str, Any]) -> Optional[int]:
    """Prefer 'season_number' when it parses to >= 0, else fall back to 's_no'."""
    season = as_integer(row.get("season_number"))
    if season is not None and season >= 0:
        return season
    return as_integer(row.get("s_no"))


# --------------------------------------------------------------------------
# Row processors
# --------------------------------------------------------------------------

def _process_tracking_episode_row(
    row: dict[str, Any],
    row_number: int,
    source_file: str,
    shows: dict[str, ShowAccumulator],
    rewatch_shows: dict[str, ShowAccumulator],
    report_rows: list[dict[str, Any]],
    include_rewatches: bool,
) -> None:
    key = str(row.get("key") or "")
    is_watch = key.startswith("watch-episode-")
    is_rewatch = key.startswith("rewatch-episode-")
    if not is_watch and not is_rewatch:
        return
    if is_rewatch and not include_rewatches:
        return

    title = clean_title(row.get("series_name"))
    season = _tracking_season_number(row)
    episode = first_positive_integer(row.get("episode_number"), row.get("ep_no"))
    watched_at = normalize_date(row.get("created_at") or row.get("updated_at"))

    if not is_valid_episode(title, season, episode, watched_at):
        report_rows.append(_make_report(
            source_file, row_number,
            "TV show rewatch episode" if is_rewatch else "TV show episode",
            f"Missing or invalid title/season/episode/date "
            f"(season={row.get('season_number') or row.get('s_no')!r}, "
            f"episode={row.get('episode_number') or row.get('ep_no')!r})",
            "not converted",
        ))
        return

    if is_rewatch:
        rewatch_index = extract_trailing_number(key) or 1
        show = _get_show(rewatch_shows, f"{normalize_key(title)}::rewatch-{rewatch_index}", title)
        show.rewatch_index = rewatch_index
        _add_episode(show, season, episode, watched_at)
        return

    show = _get_show(shows, normalize_key(title), title)
    _add_episode(show, season, episode, watched_at)


def _process_simple_episode_row(
    row: dict[str, Any],
    row_number: int,
    source_file: str,
    shows: dict[str, ShowAccumulator],
    report_rows: list[dict[str, Any]],
) -> None:
    title = clean_title(row.get("tv_show_name"))
    season = as_integer(row.get("episode_season_number"))
    episode = as_integer(row.get("episode_number"))
    watched_at = normalize_date(row.get("created_at") or row.get("updated_at"))

    if not is_valid_episode(title, season, episode, watched_at):
        report_rows.append(_make_report(
            source_file, row_number, "TV show episode",
            f"Missing or invalid title/season/episode/date "
            f"(season={row.get('episode_season_number')!r}, episode={row.get('episode_number')!r})",
            "not converted",
        ))
        return

    show = _get_show(shows, normalize_key(title), title)
    _add_episode(show, season, episode, watched_at)


def _process_legacy_rewatch_row(
    row: dict[str, Any],
    row_number: int,
    source_file: str,
    rewatch_shows: dict[str, ShowAccumulator],
    report_rows: list[dict[str, Any]],
) -> None:
    title = clean_title(row.get("tv_show_name"))
    season = as_integer(row.get("episode_season_number"))
    episode = as_integer(row.get("episode_number"))
    watched_at = normalize_date(row.get("updated_at") or row.get("created_at"))
    rewatch_index = first_positive_integer(row.get("cpt")) or 1

    if not is_valid_episode(title, season, episode, watched_at):
        report_rows.append(_make_report(
            source_file, row_number, "TV show rewatch episode (legacy)",
            f"Missing or invalid title/season/episode/date "
            f"(season={row.get('episode_season_number')!r}, episode={row.get('episode_number')!r})",
            "not converted",
        ))
        return

    key = f"{normalize_key(title)}::legacy-rewatch-{rewatch_index}"
    show = _get_show(rewatch_shows, key, title)
    show.rewatch_index = rewatch_index
    _add_episode(show, season, episode, watched_at)


def _process_followed_show_row(
    row: dict[str, Any],
    row_number: int,
    source_file: str,
    shows: dict[str, ShowAccumulator],
    report_rows: list[dict[str, Any]],
) -> None:
    title = clean_title(row.get("tv_show_name"))
    if not title:
        report_rows.append(_make_report(
            source_file, row_number, "Followed TV show", "Missing show title", "not converted",
        ))
        return

    followed_at = normalize_date(row.get("created_at") or row.get("followed_at") or row.get("updated_at"))
    show = _get_show(shows, normalize_key(title), title)
    show.added_at = _min_date(show.added_at, followed_at)


_MOVIE_ROW_TYPES = {"watch", "rewatch", "towatch", "follow", "rewatch_count"}


def _process_movie_row(
    row: dict[str, Any],
    row_number: int,
    source_file: str,
    movies: dict[str, MovieAccumulator],
    planned_movies: dict[str, MovieAccumulator],
    rewatch_movies: dict[str, MovieAccumulator],
    report_rows: list[dict[str, Any]],
    include_plan_to_watch: bool,
    include_rewatches: bool,
) -> Optional[str]:
    entity_type = str(row.get("entity_type") or "").strip().lower()
    if entity_type and entity_type != "movie":
        return None

    row_type = str(row.get("type") or "").strip().lower()
    if row_type == "rewatch_count":
        return "counter"
    if row_type not in _MOVIE_ROW_TYPES:
        return None

    title = clean_title(row.get("movie_name"))
    year = csv_utils.year_from_date(row.get("release_date"))
    watched_at = normalize_date(row.get("updated_at") or row.get("created_at"))
    base_key = _movie_base_key(title, year)

    if not title:
        report_rows.append(_make_report(source_file, row_number, "Movie", "Missing movie title", "not converted"))
        return None

    if row_type == "watch":
        movie = _get_movie(movies, base_key, title, year, base_key)
        movie.last_watched_at = _max_date(movie.last_watched_at, watched_at)
        return None

    if row_type == "rewatch":
        if not include_rewatches:
            return None
        rewatch_index = first_positive_integer(row.get("rewatch_count")) or 1
        uuid = str(row.get("uuid") or row_number)
        key = f"{base_key}::rewatch-{rewatch_index}-{uuid}"
        movie = _get_movie(rewatch_movies, key, title, year, base_key)
        movie.rewatch_index = rewatch_index
        movie.last_watched_at = _max_date(movie.last_watched_at, watched_at)
        # A rewatch implies the movie was watched at least once.
        base_movie = _get_movie(movies, base_key, title, year, base_key)
        base_movie.last_watched_at = _max_date(base_movie.last_watched_at, watched_at)
        return None

    if row_type in ("towatch", "follow"):
        if not include_plan_to_watch:
            return None
        movie = _get_movie(planned_movies, base_key, title, year, base_key)
        movie.added_at = _min_date(movie.added_at, watched_at)
        return None

    return None


# --------------------------------------------------------------------------
# Accumulator -> SIMKL entry conversion
# --------------------------------------------------------------------------

def _sort_by_title(items):
    return sorted(items, key=lambda item: item.title.casefold())


def _last_watched_label(show: ShowAccumulator) -> Optional[str]:
    if not show.seasons:
        return None
    season = max(show.seasons)
    episode = max(show.seasons[season])
    return f"S{season:02d}E{episode:02d}"


def _to_show_entry(show: ShowAccumulator, status: str, is_rewatch: bool) -> dict[str, Any]:
    seasons = [
        {
            "number": season,
            "episodes": [
                {"number": episode, "watched_at": watched_at}
                for episode, watched_at in sorted(episodes.items())
            ],
        }
        for season, episodes in sorted(show.seasons.items())
    ]

    entry: dict[str, Any] = {
        "added_to_watchlist_at": show.added_at,
        "last_watched_at": show.last_watched_at,
        "user_rated_at": None,
        "user_rating": None,
        "status": status,
        "last_watched": _last_watched_label(show),
        "next_to_watch": None,
        "watched_episodes_count": show.episode_count,
        "total_episodes_count": show.episode_count,
        "not_aired_episodes_count": 0,
        "show": {"title": show.title},
        "is_rewatch": is_rewatch,
        "seasons": seasons,
    }
    if is_rewatch:
        entry["rewatch_status"] = "completed"
        entry["rewatch_id"] = show.rewatch_index
    return entry


def _to_plan_to_watch_show_entry(show: ShowAccumulator) -> dict[str, Any]:
    return {
        "added_to_watchlist_at": show.added_at,
        "last_watched_at": None,
        "user_rated_at": None,
        "user_rating": None,
        "status": "plantowatch",
        "last_watched": None,
        "next_to_watch": None,
        "watched_episodes_count": 0,
        "total_episodes_count": 0,
        "not_aired_episodes_count": 0,
        "show": {"title": show.title},
        "is_rewatch": False,
    }


def _to_movie_entry(movie: MovieAccumulator, status: str, is_rewatch: bool) -> dict[str, Any]:
    completed = status == "completed"
    movie_payload: dict[str, Any] = {"title": movie.title}
    if movie.year:
        movie_payload["year"] = movie.year

    entry: dict[str, Any] = {
        "added_to_watchlist_at": movie.added_at,
        "last_watched_at": movie.last_watched_at if completed else None,
        "user_rated_at": None,
        "user_rating": None,
        "status": status,
        "watched_episodes_count": 1 if completed else 0,
        "total_episodes_count": 1,
        "not_aired_episodes_count": 0,
        "movie": movie_payload,
        "is_rewatch": is_rewatch,
    }
    if is_rewatch:
        entry["rewatch_status"] = "completed"
        entry["rewatch_id"] = movie.rewatch_index
    return entry


def _count_episodes(entries: list[dict[str, Any]]) -> int:
    return sum(entry.get("watched_episodes_count", 0) for entry in entries)


@dataclass
class ConversionOptions:
    include_plan_to_watch: bool = True
    include_rewatches: bool = True


@dataclass
class ConversionResult:
    simkl_backup: dict[str, Any]
    report_rows: list[dict[str, Any]]
    summary: dict[str, Any]
    notes: list[str]


def convert_tvtime_to_simkl_json(
    loaded: dict[str, LoadedFile],
    options: ConversionOptions,
    progress: ProgressCallback = _noop_progress,
) -> ConversionResult:
    """Convert parsed TV Time CSV data into the SIMKL backup JSON structure."""
    shows: dict[str, ShowAccumulator] = {}
    rewatch_shows: dict[str, ShowAccumulator] = {}
    movies: dict[str, MovieAccumulator] = {}
    planned_movies: dict[str, MovieAccumulator] = {}
    rewatch_movies: dict[str, MovieAccumulator] = {}
    report_rows: list[dict[str, Any]] = []

    for file in loaded.values():
        for warning in file.warnings:
            report_rows.append(_make_report(
                file.filename, warning.row, "csv", warning.reason, "parsed with padding/truncation",
            ))

    total = estimate_work(loaded)
    done = 0

    progress("watched episodes", done, total)
    for filename, kind in EPISODE_SOURCES:
        file = loaded[filename]
        for index, row in enumerate(file.rows):
            if kind == "tracking-v2":
                _process_tracking_episode_row(
                    row, index + 2, filename, shows, rewatch_shows, report_rows, options.include_rewatches,
                )
            else:
                _process_simple_episode_row(row, index + 2, filename, shows, report_rows)
            done += 1
        progress("watched episodes", done, total)

    progress("episode rewatches", done, total)
    legacy_rewatch_file = loaded["rewatched_episode.csv"]
    has_tracking_v2_rewatches = any(
        str(row.get("key") or "").startswith("rewatch-episode-")
        for row in loaded["tracking-prod-records-v2.csv"].rows
    )
    ignored_legacy_rewatch_rows = 0
    for index, row in enumerate(legacy_rewatch_file.rows):
        if options.include_rewatches and not has_tracking_v2_rewatches:
            _process_legacy_rewatch_row(row, index + 2, legacy_rewatch_file.filename, rewatch_shows, report_rows)
        elif has_tracking_v2_rewatches:
            ignored_legacy_rewatch_rows += 1
        done += 1
    progress("episode rewatches", done, total)

    progress("followed TV shows", done, total)
    for filename in ("followed_tv_show.csv", "user_tv_show_data.csv"):
        file = loaded[filename]
        for index, row in enumerate(file.rows):
            if options.include_plan_to_watch:
                _process_followed_show_row(row, index + 2, filename, shows, report_rows)
            done += 1
    progress("followed TV shows", done, total)

    progress("movies", done, total)
    movie_file = loaded["tracking-prod-records.csv"]
    ignored_movie_counter_rows = 0
    for index, row in enumerate(movie_file.rows):
        result = _process_movie_row(
            row, index + 2, movie_file.filename, movies, planned_movies, rewatch_movies, report_rows,
            options.include_plan_to_watch, options.include_rewatches,
        )
        if result == "counter":
            ignored_movie_counter_rows += 1
        done += 1
    progress("movies", done, total)

    progress("ratings", done, total)
    unsupported_ratings = 0
    for filename in RATING_SOURCES:
        file = loaded[filename]
        unsupported_ratings += len(file.rows)
        done += len(file.rows)
    progress("ratings", done, total)

    show_entries: list[dict[str, Any]] = []
    for show in _sort_by_title(shows.values()):
        if show.episode_count > 0:
            show_entries.append(_to_show_entry(show, "watching", is_rewatch=False))
        elif options.include_plan_to_watch:
            show_entries.append(_to_plan_to_watch_show_entry(show))

    if options.include_rewatches:
        for show in _sort_by_title(rewatch_shows.values()):
            if show.episode_count > 0:
                show_entries.append(_to_show_entry(show, "watching", is_rewatch=True))

    movie_entries: list[dict[str, Any]] = []
    for movie in _sort_by_title(movies.values()):
        movie_entries.append(_to_movie_entry(movie, "completed", is_rewatch=False))

    if options.include_plan_to_watch:
        watched_movie_keys = {movie.base_key for movie in movies.values()}
        for movie in _sort_by_title(planned_movies.values()):
            if movie.base_key not in watched_movie_keys:
                movie_entries.append(_to_movie_entry(movie, "plantowatch", is_rewatch=False))

    if options.include_rewatches:
        for movie in _sort_by_title(rewatch_movies.values()):
            movie_entries.append(_to_movie_entry(movie, "completed", is_rewatch=True))

    non_rewatch_shows = [entry for entry in show_entries if not entry["is_rewatch"]]
    rewatch_show_entries = [entry for entry in show_entries if entry["is_rewatch"]]

    summary = {
        "shows": len([e for e in non_rewatch_shows if e["status"] != "plantowatch"]),
        "show_episodes": _count_episodes(non_rewatch_shows),
        "show_rewatch_entries": len(rewatch_show_entries),
        "show_rewatch_episodes": _count_episodes(rewatch_show_entries),
        "shows_plan_to_watch": len([e for e in show_entries if e["status"] == "plantowatch"]),
        "anime": 0,
        "movies_completed": len([e for e in movie_entries if not e["is_rewatch"] and e["status"] == "completed"]),
        "movie_rewatch_entries": len([e for e in movie_entries if e["is_rewatch"]]),
        "movies_plan_to_watch": len([e for e in movie_entries if e["status"] == "plantowatch"]),
        "unsupported_ratings": unsupported_ratings,
        "ignored_movie_counter_rows": ignored_movie_counter_rows,
        "ignored_legacy_rewatch_rows": ignored_legacy_rewatch_rows,
        "failed_rows": len([r for r in report_rows if r["action"] in ("not converted", "not applied")]),
        "report_rows": len(report_rows),
    }

    notes = [
        "The main output is JSON/ZIP for SIMKL JSON import.",
        "The ZIP contains SimklBackup.json internally, even though the outer filename has a timestamp.",
        "TV Time ratings are counted in the summary, but they are not added to the JSON or failure report.",
        "The TV Time export does not include public SIMKL/TMDB/TVDB/IMDB IDs in most CSV files used here, "
        "so the JSON uses titles and years when available.",
        "Anime starts empty because the TV Time export does not identify anime reliably offline.",
    ]

    return ConversionResult(
        simkl_backup={"shows": show_entries, "anime": [], "movies": movie_entries},
        report_rows=report_rows,
        summary=summary,
        notes=notes,
    )
