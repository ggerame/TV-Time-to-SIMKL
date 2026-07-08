"""ZIP archive helpers.

TV Time GDPR exports and the final SIMKL backup are both plain ZIP files.
This module is a thin, well-documented wrapper around Python's standard
:mod:`zipfile` module, covering everything needed to read a TV Time export
and write a SIMKL backup ZIP (CRC32, DEFLATE, central directory, etc.).
"""
from __future__ import annotations

import io
import zipfile
from typing import Iterable

#: Name SIMKL expects for the backup JSON file *inside* the generated ZIP.
SIMKL_INTERNAL_JSON_NAME = "SimklBackup.json"


def read_zip_text_files(zip_bytes: bytes, wanted_filenames: Iterable[str]) -> dict[str, str]:
    """Read specific files out of a ZIP archive as UTF-8 text.

    Matching is done on the file's base name, case-insensitively, so the
    function tolerates TV Time exports that place the CSV files inside a
    sub-folder. Files that are not present in the archive are simply absent
    from the returned dictionary.

    :param zip_bytes: raw bytes of the uploaded ZIP file.
    :param wanted_filenames: base file names to look for (e.g. ``"seen_episode.csv"``).
    :return: mapping of *requested* filename -> decoded text content.
    """
    wanted_lower = {name.lower(): name for name in wanted_filenames}
    found: dict[str, str] = {}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            base_name = info.filename.rsplit("/", 1)[-1].lower()
            wanted_name = wanted_lower.get(base_name)
            if not wanted_name or wanted_name in found:
                continue
            with archive.open(info) as fh:
                found[wanted_name] = fh.read().decode("utf-8", errors="replace")

    return found


def read_zip_entries(zip_bytes: bytes) -> dict[str, bytes]:
    """Read *every* file in a ZIP archive into memory, keyed by full path."""
    result: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            with archive.open(info) as fh:
                result[info.filename] = fh.read()
    return result


def create_zip_bytes(filename: str, content: bytes) -> bytes:
    """Create an in-memory ZIP archive containing a single file."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, content)
    return buffer.getvalue()


def create_simkl_backup_zip(json_text: str) -> bytes:
    """Create the final SIMKL import ZIP containing ``SimklBackup.json``."""
    return create_zip_bytes(SIMKL_INTERNAL_JSON_NAME, json_text.encode("utf-8"))
