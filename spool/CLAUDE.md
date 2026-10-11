# Spool – notes for Claude Code

Spool is a Transmission-style web UI for yt-dlp, shipped as a container. Default port **7765**
(chosen to avoid 8080 = qBittorrent/SABnzbd and the usual *arr ports).

## Layout
- `spool/app.py` – FastAPI routes, basic-auth middleware, SSE event bus (`/api/events`)
- `spool/worker.py` – dispatcher thread + one thread per job; yt-dlp used as a library with
  progress/postprocessor hooks; presets; pause = raise `DownloadCancelled` from a hook, resume relies
  on `continuedl` with per-job temp dir `/downloads/.incomplete/<id>`
- `spool/db.py` – SQLite (`/config/spool.db`), single `jobs` table, lock-guarded
- `spool/cookies.py` – Netscape cookies.txt validation/storage; per-job temp copy (yt-dlp rewrites it);
  contents never returned by the API
- `spool/arr.py` – Sonarr/Radarr: `DownloadedEpisodesScan` / `DownloadedMoviesScan` on the release
  folder, path mapped via `*_DOWNLOADS_PATH`; status polled and shown as `arr_status`;
  `/api/arr/{kind}/parse` wraps the *arr `/parse` endpoint for the add-dialog "Check" button;
  `/api/arr/sonarr/match` probes the URL (`worker.probe`, no download) and ranks episodes: site
  season/episode metadata → Sonarr `/parse` of the video title → air date ±1 day / fuzzy title (`match_episodes`)
- `spool/static/` – no-build frontend (index.html, app.js, style.css), light/dark, mobile layout
- `Dockerfile`, `entrypoint.sh` (yt-dlp auto-update, PUID/PGID via setpriv), `docker-compose.yml`

## Run locally
```sh
pip install -r requirements.txt
SPOOL_DOWNLOADS=./downloads SPOOL_CONFIG=./config uvicorn spool.app:app --reload --port 7765
```

## Status
Tested: download, pause/resume (byte-range), resume after restart, remove (+files) while running,
MP3/M4A, cookies upload/validation, auth, path sanitising, Sonarr flow against a fake Sonarr,
entrypoint privilege drop. NOT yet tested: `docker build` (registry was blocked when written),
real Sonarr/Radarr, real YouTube (Deno/EJS runtime).

## Next ideas (README roadmap)
1. Act as a Sonarr/Radarr download client (so they can send grabs to Spool)
2. Per-site cookie profiles
3. Notifications (ntfy/webhook)
4. Download finished files from the browser
5. yt-dlp update button with in-place restart