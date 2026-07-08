"""NiceGUI web application: TV Time to SIMKL import file converter.

Run with:

    python app.py

This single-file UI module wires together the pipeline in ``src/`` into an
interactive review workflow:

1. Upload a TV Time GDPR export ZIP (and optionally a "TV Time Out by
   Refract" ZIP) and convert it to the SIMKL backup JSON shape.
2. Automatically look up each show/movie on SIMKL to attach a verified ID.
3. Review the results in an editable grid, fix anything SIMKL could not
   confidently match, and re-validate those edits.
4. Cache confirmed IDs in a local SQLite database for future conversions.
5. Download the final SIMKL import ZIP.
"""
from __future__ import annotations

import csv
import io
import time
from typing import Any, Optional
from urllib.parse import quote

from nicegui import ui

from src.config import CONFIG
from src.job_manager import JobManager
from src.pipeline import Job, create_job
from src.records import (
    build_download,
    build_simkl_csv_export,
    clean_id,
    normalize_record_type,
    validate_manual_records,
)
from src.simkl_client import SimklClient, clean_imdb_id, clean_numeric_id
from src.sqlite_store import SqliteStore

#: Maps a record's internal visual status to the label shown in the grid.
STATUS_LABELS = {"found": "Matched", "pending": "Edited", "not_found": "Unmatched"}

#: Minimum time between progress-bar UI updates, to avoid flooding the
#: websocket connection when processing thousands of CSV rows/records.
PROGRESS_THROTTLE_SECONDS = 0.1

store = SqliteStore(CONFIG.db_path)
job_manager = JobManager(store)


def make_progress_bar() -> ui.linear_progress:
    """A thick progress bar that shows a rounded percentage instead of a raw 0-1 float.

    NiceGUI's built-in value label just binds to the raw float (e.g.
    "0.8640533778148457"), so this replaces it with a properly formatted one.
    Passing ``show_value=False`` makes NiceGUI default to a thin 4px bar (meant
    for when there's no label at all), so the height must be set explicitly
    here or the percentage text we add doesn't fit/show.
    """
    bar = ui.linear_progress(value=0, show_value=False, size="26px").props("instant-feedback")
    with bar:
        ui.label().classes("absolute-center text-white text-sm font-medium") \
            .bind_text_from(bar, "value", backward=lambda value: f"{round(value * 100)}%")
    return bar


def make_throttled_progress(phase_label: ui.label, progress_bar: ui.linear_progress):
    """Build a progress callback that updates the UI at most a few times per second."""
    state = {"last_update": 0.0}

    def report(phase: str, done: int, total: int) -> None:
        now = time.monotonic()
        is_final = total > 0 and done >= total
        if not is_final and now - state["last_update"] < PROGRESS_THROTTLE_SECONDS:
            return
        state["last_update"] = now
        fraction = (done / total) if total else 0.0
        phase_label.set_text(f"{phase} ({done}/{total})")
        progress_bar.set_value(fraction)

    return report


def google_search_url(title: str, year: Optional[int]) -> str:
    # "site:imdb.com" keeps the results scoped to IMDb pages, so the top hit
    # is normally the exact title page - its URL can be pasted straight into
    # the IMDb ID column (which also accepts a full link, not just the bare ID).
    query = f"{title} {year} site:imdb.com" if year else f"{title} site:imdb.com"
    return f"https://www.google.com/search?q={quote(query)}"


def build_grid_rows(job: Job, status_filter: str, search_text: str) -> list[dict[str, Any]]:
    """Turn the current records into AG Grid row dictionaries, applying filter/search."""
    query = search_text.strip().lower()
    rows: list[dict[str, Any]] = []

    for record in job.records:
        if record.excluded:
            continue

        visual_status = record.visual_status()
        if status_filter != "all" and status_filter != visual_status:
            continue

        if query:
            haystack = " ".join([
                record.title.lower(), record.input_simkl_id, record.input_imdb_id.lower(),
                record.input_tvdb_id, record.simkl_title.lower(),
            ])
            if query not in haystack:
                continue

        details = f"occurrences: {record.occurrences} · episodes: {record.watched_episodes} · rewatches: {record.rewatch_entries}"
        if record.confidence is not None:
            details += f" · confidence: {record.confidence}%"
        if record.reason:
            details += f" · {record.reason}"

        rows.append({
            "id": record.id,
            "status_label": STATUS_LABELS[visual_status],
            "google": (
                f'<a href="{google_search_url(record.title, record.year)}" target="_blank" '
                'rel="noopener" style="text-decoration:none">\U0001f50d</a>'
            ),
            "delete": "🗑",
            "type": record.simkl_type,
            "title": record.title,
            "year": record.year or "",
            "simkl_id": record.input_simkl_id,
            "imdb_id": record.input_imdb_id,
            "tvdb_id": record.input_tvdb_id,
            "simkl_title": record.simkl_title,
            "details": details,
        })

    return rows


#: Columns included in the CSV export, in order (the grid's "google"/"delete"
#: action columns are UI-only and make no sense in an exported file).
CSV_EXPORT_FIELDS = ["status_label", "type", "title", "year", "simkl_id", "imdb_id", "tvdb_id", "simkl_title", "details"]


def build_export_csv(job: Job, status_filter: str, search_text: str) -> bytes:
    """Export whatever is currently visible in the grid (same filter + search) to CSV."""
    rows = build_grid_rows(job, status_filter, search_text)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")  # BOM so Excel opens accented titles correctly


#: Positional index of the "google" column in build_grid_options()'s columnDefs,
#: needed because AG Grid's html_columns option addresses columns by index.
GOOGLE_COLUMN_INDEX = 1


def build_grid_options() -> dict[str, Any]:
    """Static AG Grid configuration shared by every job's review table."""
    action_column = {
        "sortable": False, "filter": False, "editable": False, "resizable": False,
        "width": 50, "pinned": "left", "cellStyle": {"cursor": "pointer", "textAlign": "center"},
    }
    return {
        "columnDefs": [
            {"headerName": "Status", "field": "status_label", "width": 110, "pinned": "left"},
            {"headerName": "🔍", "field": "google", "headerTooltip": "Search this title on IMDb (via Google)", **action_column},
            {"headerName": "🗑", "field": "delete", "headerTooltip": "Remove this title from the export", **action_column},
            {
                "headerName": "Type", "field": "type", "width": 100, "editable": True,
                "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": ["tv", "movie", "anime"]},
            },
            {"headerName": "TV Time title", "field": "title", "flex": 2, "minWidth": 220},
            {"headerName": "Year", "field": "year", "width": 90},
            {"headerName": "SIMKL ID", "field": "simkl_id", "editable": True, "width": 130},
            {
                "headerName": "IMDb ID / link", "field": "imdb_id", "editable": True, "width": 150,
                "headerTooltip": "Paste an IMDb ID (tt1234567) or a full IMDb URL - the link is parsed automatically",
            },
            {"headerName": "TVDB ID", "field": "tvdb_id", "editable": True, "width": 130},
            {"headerName": "SIMKL title", "field": "simkl_title", "flex": 2, "minWidth": 200},
            {"headerName": "Details", "field": "details", "flex": 2, "minWidth": 260},
        ],
        "rowData": [],
        "stopEditingWhenCellsLoseFocus": True,
        "rowClassRules": {
            "bg-green-50": "data.status_label == 'Matched'",
            "bg-red-50": "data.status_label == 'Unmatched'",
            "bg-yellow-50": "data.status_label == 'Edited'",
        },
        ":getRowId": "(params) => params.data.id",
    }


def summary_text(job: Job) -> str:
    summary = job.summary
    return (
        f"{summary.get('media_records', 0)} unique titles · "
        f"{summary.get('simkl_found', 0)} matched on SIMKL · "
        f"{summary.get('simkl_not_found', 0)} need review · "
        f"{summary.get('shows', 0)} shows ({summary.get('show_episodes', 0)} episodes) · "
        f"{summary.get('movies_completed', 0)} movies · "
        f"{summary.get('shows_plan_to_watch', 0) + summary.get('movies_plan_to_watch', 0)} plan-to-watch"
    )


@ui.page("/")
def index() -> None:
    ui.query("body").classes("bg-slate-50")

    state: dict[str, Any] = {
        "tvtime_bytes": None,
        "tvtime_out_bytes": None,
        "job": None,
        "status_filter": "all",
        "search_text": "",
    }

    with ui.column().classes("w-full max-w-3xl mx-auto gap-4 p-4"):
        ui.label("Watch History Bridge").classes("text-2xl font-bold")
        ui.label(
            "Turn a TV Time data export into a SIMKL-ready backup, and fix any mismatched titles before you download it.",
        ).classes("text-gray-600 mb-2")

        # -- Upload & options ---------------------------------------------------
        with ui.card().classes("w-full gap-3"):
            ui.label("Step 1 · Import your watch history").classes("text-lg font-semibold")

            async def handle_tvtime_upload(event) -> None:
                state["tvtime_bytes"] = await event.file.read()
                event.sender.reset()  # clear the queued file preview so the card doesn't grow
                tvtime_file_label.set_text(f"\u2713 {event.file.name}")
                ui.notify(f"Loaded {event.file.name}")

            async def handle_tvtime_out_upload(event) -> None:
                state["tvtime_out_bytes"] = await event.file.read()
                event.sender.reset()
                tvtime_out_file_label.set_text(f"\u2713 {event.file.name}")
                ui.notify(f"Loaded {event.file.name}")

            with ui.row().classes("w-full gap-6 flex-wrap items-start").style("min-height: 120px"):
                with ui.column().classes("gap-1"):
                    ui.upload(label="TV Time export (.zip, required)", on_upload=handle_tvtime_upload, auto_upload=True) \
                        .props("accept=.zip flat bordered").classes("max-w-sm")
                    tvtime_file_label = ui.label("No file selected yet").classes("text-xs text-gray-500")
                with ui.column().classes("gap-1"):
                    ui.upload(label="TV Time Out extra data (.zip, optional)", on_upload=handle_tvtime_out_upload, auto_upload=True) \
                        .props("accept=.zip flat bordered").classes("max-w-sm")
                    tvtime_out_file_label = ui.label("Optional - none selected").classes("text-xs text-gray-500")

            client_id_input = ui.input("SIMKL client_id", value=CONFIG.simkl_client_id).classes("w-full max-w-md")
            with ui.row().classes("gap-6"):
                include_plan_checkbox = ui.checkbox("Include plan-to-watch titles", value=True)
                include_rewatch_checkbox = ui.checkbox("Include re-watch history", value=True)

            phase_label = ui.label("").classes("text-sm text-gray-500")
            progress_bar = make_progress_bar()
            progress_bar.visible = False

            async def handle_convert() -> None:
                if not state["tvtime_bytes"]:
                    ui.notify("Please choose a TV Time export ZIP first.", type="warning")
                    return

                convert_button.disable()
                progress_bar.visible = True
                progress_bar.set_value(0)
                phase_label.set_text("starting…")
                report_progress = make_throttled_progress(phase_label, progress_bar)

                try:
                    job = await create_job(
                        tvtime_zip_bytes=state["tvtime_bytes"],
                        tvtime_out_zip_bytes=state["tvtime_out_bytes"],
                        client_id=client_id_input.value.strip(),
                        include_plan_to_watch=include_plan_checkbox.value,
                        include_rewatches=include_rewatch_checkbox.value,
                        config=CONFIG,
                        store=store,
                        progress=report_progress,
                    )
                except Exception as exc:  # noqa: BLE001 - surface any failure to the user
                    ui.notify(f"Conversion failed: {exc}", type="negative")
                    convert_button.enable()
                    progress_bar.visible = False
                    return

                state["job"] = job
                job_manager.add(job)
                await job_manager.persist(job)

                phase_label.set_text("done")
                progress_bar.set_value(1)
                convert_button.enable()
                render_review.refresh(job)
                ui.notify("Import complete - review the matches below.", type="positive")

            convert_button = ui.button("Start import", on_click=handle_convert).props("color=primary")

        # -- Reopen a saved import ------------------------------------------------
        with ui.card().classes("w-full gap-3"):
            ui.label("Reopen a saved import").classes("text-lg font-semibold")
            with ui.row().classes("items-center gap-2"):
                job_id_input = ui.input("Import ID").classes("w-96")

                async def handle_resume() -> None:
                    job_id = job_id_input.value.strip()
                    if not job_id:
                        return
                    job = await job_manager.get(job_id)
                    if job is None:
                        ui.notify("No import found with that ID.", type="warning")
                        return
                    state["job"] = job
                    render_review.refresh(job)
                    ui.notify("Import reopened.", type="positive")

                ui.button("Reopen", on_click=handle_resume)

    # -- Review table -------------------------------------------------------------
    @ui.refreshable
    def render_review(job: Optional[Job]) -> None:
        if job is None:
            return

        with ui.card().classes("w-full gap-3"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Step 2 · Review your matches").classes("text-lg font-semibold")
                with ui.row().classes("items-center gap-2"):
                    ui.label("Import ID:").classes("text-sm text-gray-500")
                    ui.label(job.id).classes("text-sm font-mono")

            ui.label(summary_text(job)).classes("text-sm text-gray-700")

            with ui.row().classes("items-center gap-2 flex-wrap"):
                filter_toggle = ui.toggle(
                    {"all": "All", "found": "Matched", "pending": "Edited", "not_found": "Unmatched"},
                    value=state["status_filter"],
                )
                search_input = ui.input("Search by title or ID").classes("w-64")
                include_tv_checkbox = ui.checkbox("TV shows", value=True)
                include_movies_checkbox = ui.checkbox("Movies", value=True)
                include_anime_checkbox = ui.checkbox("Anime", value=True)

                def handle_export_csv() -> None:
                    csv_bytes = build_export_csv(job, state["status_filter"], state["search_text"])
                    ui.download(csv_bytes, f"watch-history-review-{state['status_filter']}.csv")

                ui.button("Export view to CSV", icon="download", on_click=handle_export_csv).props("outline")

            grid = ui.aggrid(build_grid_options(), html_columns=[GOOGLE_COLUMN_INDEX]).classes("w-full").style("height: 480px")
            grid.options["rowData"] = build_grid_rows(job, state["status_filter"], state["search_text"])

            async def sync_grid_edits_into_records() -> None:
                """Copy whatever is currently in the grid's editable cells into the
                matching records, without judging whether anything "changed".

                Must run before any full rowData rebuild (filter/search change,
                row deletion, ...): AG Grid keeps in-progress edits client-side
                only, so replacing rowData wholesale would otherwise silently
                discard edits in every row the user hasn't explicitly re-checked
                or downloaded yet.
                """
                edited_rows = await grid.get_client_data()
                by_id = job.records_by_id()
                for row in edited_rows:
                    record = by_id.get(row["id"])
                    if record is None:
                        continue
                    record.input_simkl_id = clean_id(row.get("simkl_id", ""))
                    record.input_imdb_id = clean_imdb_id(row.get("imdb_id", ""))
                    record.input_tvdb_id = clean_numeric_id(row.get("tvdb_id", ""))
                    record.simkl_type = normalize_record_type(row.get("type") or record.simkl_type)

            async def refresh_rows() -> None:
                await sync_grid_edits_into_records()
                state["status_filter"] = filter_toggle.value
                state["search_text"] = search_input.value or ""
                grid.options["rowData"] = build_grid_rows(job, state["status_filter"], state["search_text"])
                grid.update()

            filter_toggle.on_value_change(lambda _: refresh_rows())
            search_input.on("keyup", lambda _: refresh_rows())

            async def handle_cell_clicked(event) -> None:
                """Handle clicks on the per-row � (remove) action cell.

                (The 🔍 Google-search column is a real ``<a target="_blank">`` link
                instead - opening a new tab from a server round-trip like this one
                gets silently blocked by the browser's popup blocker.)

                NiceGUI's AG Grid wrapper re-emits a sanitized subset of the native
                cell-click event (see ``aggrid.js``'s ``handle_event``): the clicked
                column is exposed as ``colId``, not the raw ``column``/``colDef``
                objects (which aren't safely serializable to send to the server).
                """
                payload = event.args or {}
                if payload.get("colId") != "delete":
                    return
                row_id = (payload.get("data") or {}).get("id")
                record = job.records_by_id().get(row_id) if row_id else None
                if record is None:
                    return

                record.excluded = True
                await refresh_rows()
                await job_manager.persist(job)
                ui.notify(f"Removed \"{record.title}\" from this export.", type="info")

            grid.on("cellClicked", handle_cell_clicked, ["data", "colId"])

            validation_phase_label = ui.label("").classes("text-sm text-gray-500")
            validation_progress_bar = make_progress_bar()
            validation_progress_bar.visible = False

            async def collect_pending_edits() -> list[dict[str, str]]:
                """Pull whatever is currently in the grid (including in-progress edits)."""
                edited_rows = await grid.get_client_data()
                by_id = job.records_by_id()
                updates = []
                for row in edited_rows:
                    record = by_id.get(row["id"])
                    if record is None:
                        continue
                    simkl_id = clean_id(row.get("simkl_id", ""))
                    imdb_id = clean_imdb_id(row.get("imdb_id", ""))
                    tvdb_id = clean_numeric_id(row.get("tvdb_id", ""))
                    simkl_type = normalize_record_type(row.get("type") or record.simkl_type)
                    changed = (
                        simkl_id != (record.input_simkl_id or "")
                        or imdb_id != (record.input_imdb_id or "")
                        or tvdb_id != (record.input_tvdb_id or "")
                        or simkl_type != record.simkl_type
                    )
                    if changed:
                        updates.append({
                            "id": row["id"], "simkl_id": simkl_id, "imdb_id": imdb_id,
                            "tvdb_id": tvdb_id, "simkl_type": simkl_type,
                        })
                return updates

            async def handle_validate_changed() -> None:
                client_id = client_id_input.value.strip()
                if not client_id:
                    ui.notify("Enter a SIMKL client_id above before re-checking.", type="warning")
                    return

                updates = await collect_pending_edits()
                if not updates:
                    ui.notify("Nothing to re-check - no rows were edited.")
                    return

                validate_button.disable()
                validation_progress_bar.visible = True
                validation_progress_bar.set_value(0)
                report_progress = make_throttled_progress(validation_phase_label, validation_progress_bar)
                try:
                    async with SimklClient(
                        client_id, min_delay_ms=CONFIG.simkl_api_delay_ms, timeout_ms=CONFIG.simkl_api_timeout_ms,
                    ) as client:
                        await validate_manual_records(job.records_by_id(), updates, client, progress=report_progress)
                except Exception as exc:  # noqa: BLE001
                    ui.notify(f"Validation failed: {exc}", type="negative")
                finally:
                    validate_button.enable()
                    validation_progress_bar.visible = False

                job.summary["simkl_found"] = len([r for r in job.records if r.status == "found"])
                job.summary["simkl_not_found"] = len([r for r in job.records if r.status != "found"])
                await job_manager.persist(job)
                render_review.refresh(job)
                ui.notify(f"Re-checked {len(updates)} edited row(s).", type="positive")

            async def handle_save_to_db() -> None:
                result = await store.save_mappings(job.records)
                ui.notify(f"Remembered {result['saved']} confirmed match(es) for next time.", type="positive")

            async def apply_pending_edits_and_maybe_remember() -> None:
                """Apply any in-progress grid edits to the records, then optionally
                cache confirmed matches locally before an export is generated.
                """
                updates = await collect_pending_edits()
                by_id = job.records_by_id()
                for update in updates:
                    record = by_id[update["id"]]
                    record.input_simkl_id = update["simkl_id"]
                    record.input_imdb_id = update["imdb_id"]
                    record.input_tvdb_id = update["tvdb_id"]
                    record.simkl_type = update["simkl_type"]

                with ui.dialog() as dialog, ui.card():
                    ui.label("Remember these confirmed matches before downloading?")
                    with ui.row():
                        ui.button("Yes", on_click=lambda: dialog.submit(True))
                        ui.button("No", on_click=lambda: dialog.submit(False))
                if await dialog:
                    await handle_save_to_db()

            async def handle_generate_zip() -> None:
                await apply_pending_edits_and_maybe_remember()

                filename, zip_bytes = build_download(
                    job.backup, job.records,
                    include_tv=include_tv_checkbox.value,
                    include_movies=include_movies_checkbox.value,
                    include_anime=include_anime_checkbox.value,
                )
                ui.download(zip_bytes, filename)
                await job_manager.persist(job)
                render_review.refresh(job)

            async def handle_generate_simkl_csv() -> None:
                await apply_pending_edits_and_maybe_remember()

                filename, csv_bytes = build_simkl_csv_export(
                    job.backup, job.records,
                    include_tv=include_tv_checkbox.value,
                    include_movies=include_movies_checkbox.value,
                    include_anime=include_anime_checkbox.value,
                )
                ui.download(csv_bytes, filename)
                await job_manager.persist(job)
                render_review.refresh(job)

            with ui.row().classes("gap-2"):
                validate_button = ui.button("Re-check edited rows", on_click=handle_validate_changed)
                ui.button("Remember these matches", on_click=handle_save_to_db)
                ui.button("Download SIMKL backup", on_click=handle_generate_zip).props("color=primary")
                ui.button("Download SIMKL CSV", on_click=handle_generate_simkl_csv).props("outline") \
                    .tooltip("SIMKL's own bulk-import CSV format (simkl.com/apps/import) - a simpler alternative to the JSON backup")


            if job.report_rows:
                with ui.expansion(f"Rows that need attention ({len(job.report_rows)} skipped/adjusted)").classes("w-full"):
                    issue_rows = [
                        {"source": r["source"], "row": r["row"], "type": r["type"], "reason": r["reason"], "action": r["action"]}
                        for r in job.report_rows
                    ]
                    ui.aggrid({
                        "columnDefs": [
                            {"headerName": "Source file", "field": "source", "flex": 1},
                            {"headerName": "Row", "field": "row", "width": 90},
                            {"headerName": "Type", "field": "type", "flex": 1},
                            {"headerName": "Reason", "field": "reason", "flex": 2},
                            {"headerName": "Action", "field": "action", "flex": 1},
                        ],
                        "rowData": issue_rows,
                    }).classes("w-full").style("height: 240px")

            if job.notes:
                with ui.expansion("Notes").classes("w-full"):
                    for note in job.notes:
                        ui.label(f"• {note}").classes("text-sm text-gray-600")

    with ui.column().classes("w-full max-w-6xl mx-auto gap-4 p-4"):
        render_review(state["job"])


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="Watch History Bridge", host=CONFIG.host, port=CONFIG.port, reload=False)
