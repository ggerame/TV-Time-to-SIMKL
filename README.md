# Watch History Bridge (TV Time to SIMKL, Python / NiceGUI)

Convert a [TV Time](https://www.tvtime.com/) GDPR data export into a
[SIMKL](https://simkl.com/) JSON and CSV import backup, with a web-based review step
so you can check (and fix) the SIMKL/IMDb/TVDB IDs before downloading the
final file.

Built with [NiceGUI](https://nicegui.io/) for the web interface and a local
SQLite database for caching confirmed IDs and resuming jobs.

## Features

- Web UI: upload a TV Time GDPR export ZIP, review matches, download a ready-to-import ZIP.
- Two export formats: a SIMKL backup ZIP (`SimklBackup.json`) or a [SIMKL bulk-import CSV](https://simkl.com/apps/import) - plus a raw CSV export of the review table itself for offline review.
- Optional prefill of IMDb/TVDB IDs from a `TV Time Out by Refract` export ZIP.
- IMDb-first matching: resolves an IMDb ID from the title via IMDb's search first, then asks SIMKL for that exact IMDb ID. This fixes SIMKL's free-text search often failing on numeric/punctuated titles (e.g. "9-1-1", "The 100", "1899"). Falls back to SIMKL's own title/year search (via `/redirect` and search endpoints, with confidence scoring) when IMDb has no confident match.
- Anime detection via TVmaze: since the TV Time export has no genre data, TVmaze's genre tags (looked up by the resolved IMDb ID) are used to try SIMKL's "anime" catalog before "tv" for shows TVmaze tags as anime.
- Editable review table (SIMKL ID, IMDb ID, TVDB ID, type) with status coloring (found / changed / not found), search and filters.
- Local SQLite cache of confirmed SIMKL IDs, shared across future conversions, plus job persistence so a job can be resumed by ID after a restart.
- Options to include/exclude plan-to-watch entries, rewatches, and to filter the export by TV shows / movies / anime.

## Project structure

```text
app.py                          NiceGUI web application (entry point)
src/
  config.py                     Environment/.env configuration
  csv_utils.py                  CSV parsing + field normalization helpers
  converter.py                  TV Time CSV -> SIMKL backup JSON conversion
  zip_utils.py                  ZIP read/write helpers (TV Time export + SIMKL backup)
  simkl_client.py               Async SIMKL API client (search, redirect, ID lookups, scoring)
  imdb_client.py                IMDb title-search client used to resolve an IMDb ID before matching on SIMKL
  tvmaze_client.py              TVmaze client used to detect anime via genre tags (by IMDb ID)
  tv_time_out.py                Optional "TV Time Out by Refract" export parser
  records.py                    Per-title "media record" model, enrichment application, export
  pipeline.py                   Orchestrates the whole conversion + enrichment flow
  sqlite_store.py               Local SQLite-backed ID cache and job store
  job_manager.py                In-memory job registry (+ SQLite mirroring)
tests/                          Unit tests (pytest)
```

## Requirements

- Python 3.10+
- A SIMKL API `client_id` (see below)

No external database server is required: confirmed IDs and jobs are cached
in a local SQLite file, created automatically on first use.

## Setup

Always use a virtual environment for this project.

```bash
python3 -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set SIMKL_CLIENT_ID
```

## Running the app

```bash
source .venv/bin/activate
python app.py
```

Open the URL printed in the terminal, normally <http://127.0.0.1:8080/>.

You can also set the SIMKL client ID directly in the web UI's "SIMKL
client_id" field instead of putting it in `.env` - useful for trying the app
without a persistent configuration.

## Create a SIMKL app

This project only needs a SIMKL `client_id` for catalog/search requests. It
does not use OAuth or an access token.

1. Go to <https://simkl.com/settings/developer/> and create a new app.
2. Suggested details:
   - Name: `Watch History Bridge`
   - Redirect URI: leave empty if allowed, otherwise use your local/public app URL (e.g. `http://127.0.0.1:8080/`).
3. Copy the `client_id` into `.env`:

   ```env
   SIMKL_CLIENT_ID=your_client_id
   ```

## Input files

### TV Time GDPR export (required)

1. Go to <https://gdpr.tvtime.com/gdpr/self-service> and log in.
2. Request your GDPR data export and wait for TV Time to prepare it (this can take hours).
3. Download the ZIP once ready and upload it in the app.

### TV Time Out by Refract export (optional, recommended)

This ZIP is optional but improves matching accuracy by prefilling IMDb/TVDB
IDs before SIMKL lookups happen.

1. Install/open the Chrome extension [TV Time Out by Refract](https://chromewebstore.google.com/detail/tv-time-out-by-refract/pmejpdpjbkjklfceogdkolmgclldogbi).
2. Export the same TV Time account, format `Both`, with `Bundle as ZIP` enabled.
3. Upload the resulting ZIP in the app's second upload field.

## Reviewing results

After conversion, each unique show/movie appears as one row in the review
table:

- **Green** row: a SIMKL ID has been matched.
- **Red** row: no matched SIMKL ID yet.
- **Yellow** row: the SIMKL/IMDb/TVDB ID or type was edited and hasn't been re-checked yet.

You can:

- Edit the `SIMKL ID`, `IMDb ID`, `TVDB ID`, and `Type` cells directly in the table.
- Click the 🔍 icon on a row to search that title on IMDb (via Google), and the 🗑 icon to drop a title from the export entirely.
- Click **Re-check edited rows** to re-check edited rows against the SIMKL API.
- Click **Remember these matches** to cache confirmed IDs locally, so future imports need less manual work.
- Filter rows by status (All / Matched / Edited / Unmatched) and search by title or ID.
- Click **Export view to CSV** to download whatever is currently visible in the table (same filter/search) for offline review.
- Choose which categories (TV shows / Movies / Anime) to include in the export.
- Click **Download SIMKL backup** to download the final `SimklBackup-<timestamp>.zip`, containing `SimklBackup.json` - SIMKL's JSON import format.
- Click **Download SIMKL CSV** to instead download a `SimklImport-<timestamp>.csv` in [SIMKL's own bulk-import CSV format](https://simkl.com/apps/import) (`simkl_id, TVDB_ID, TMDB, IMDB_ID, MAL_ID, Type, Title, Year, LastEpWatched, Watchlist, WatchedDate, Rating, Memo`) - a simpler alternative some tools/workflows expect instead of the JSON backup. This format has no concept of rewatches or per-episode detail, so if a title has both a regular watch and a rewatch entry, only the regular one is included; `TMDB`, `MAL_ID`, `Rating` and `Memo` are always blank since TV Time exports don't carry that data.

## Jobs

Each processed export gets an import ID, shown in the review panel. Completed
imports are mirrored to the local SQLite database, so one can be reopened
later (paste the ID into the "Reopen a saved import" field) even after
a server restart.

## Local database

```env
DB_PATH=data/tvtime_simkl.sqlite3
```

A single SQLite file (created automatically, along with its parent folder,
on first use) holds two tables:

- `id_mappings`: confirmed SIMKL/IMDb/TVDB IDs per (type, title, year), reused on future conversions.
- `jobs`: completed jobs, so a job can be resumed by ID later.

There is no server to install or configure - the database is just a file on
disk. Back it up (or delete it to start fresh) like any other file.

## Matching strategy

For each show/movie without a known ID, the app:

1. Searches IMDb by title (via IMDb's public title-suggestion endpoint) and picks the best candidate by normalized title, year proximity, and category (show vs movie). Titles are normalized in a script-aware way (accents/punctuation stripped, but letters from any alphabet - Kanji, Hangul, Cyrillic, etc. - are kept), and a title written entirely in a non-Latin script trusts IMDb's own top search result, since IMDb's candidate titles are usually English/romanized and could otherwise never text-match a native-script query.
2. For shows, looks that IMDb ID up on [TVmaze](https://www.tvmaze.com/api) (free, no API key) to check its genre tags. The TV Time export has no genre information at all, so this is the only signal the app has for telling anime apart from a regular TV show; when TVmaze tags a show `Anime`, SIMKL's `anime` catalog is tried before its `tv` catalog.
3. If IMDb returns a confident match, asks SIMKL for the item with that exact IMDb ID (`lookupSource` shows as `imdb_search` in the review table).
4. If SIMKL doesn't recognize that IMDb ID, retries SIMKL's title search using IMDb's own title/year (`imdb_title`) - useful when TV Time's title carries a subtitle IMDb/SIMKL don't use (e.g. "El Camino: A Breaking Bad Movie" is just "El Camino" on IMDb).
5. If IMDb had no confident match at all, falls back to SIMKL's own title/year search using the original TV Time title, with confidence scoring.

Both the IMDb and TVmaze steps use best-effort, resilient clients: any network error or unexpected response is treated as "no data" for that step, so a change or outage on either side never breaks a conversion - it just falls back to the next step.

Matches accepted through the non-Latin fallback in step 1 aren't a confident
text match (there was nothing to text-match against), so they're applied but
shown as **pending** (yellow) in the review table instead of found (green),
with a note in the Details column - the SIMKL/IMDb IDs are already filled
in, but it's worth a quick look to confirm IMDb picked the right title.

## API rate limiting

```env
SIMKL_API_DELAY_MS=110
SIMKL_API_TIMEOUT_MS=20000
```

Increase `SIMKL_API_DELAY_MS` if you see `429 Too Many Requests` errors.
Requests are retried with exponential backoff on `429`/`5xx` responses.

## Running tests

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

## Known limitations / notes

- Anime is never auto-detected purely from the raw TV Time export (TV Time
  does not identify anime offline at all); TVmaze's genre tags help bias
  matching toward SIMKL's anime catalog once an IMDb ID is resolved, but a
  row can still be reclassified manually in the review table if needed.
- TV Time ratings are counted in the summary but not imported, since SIMKL
  backups have no equivalent field for them.
- IMDb matching relies on an undocumented, unofficial endpoint that could
  change or disappear without notice. The app is resilient to that (it just
  falls back to SIMKL's own search), but exact match rates may vary if IMDb
  changes their API.
- CSV parsing and conversion run synchronously in the request handler. For
  the CSV sizes a typical personal TV Time export produces this is fast, but
  very large exports (hundreds of thousands of rows) may briefly block the
  UI for other connected users while processing.
- Job data (including the full SIMKL backup) is kept in memory per server
  process while the app is running; the SQLite mirror lets a completed job be
  resumed after a restart, but an in-progress conversion is lost if the
  server stops before it finishes.

## Privacy

Do not commit real TV Time exports, generated `SimklBackup*` files, the local
SQLite database, or `.env` files containing real credentials - `.gitignore`
already excludes them.

## License

[MIT](LICENSE)
