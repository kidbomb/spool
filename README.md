# Spool

A small, Transmission-style web UI for [yt-dlp](https://github.com/yt-dlp/yt-dlp), packaged as a container.
Paste a link, watch it download, optionally hand the result to Sonarr or Radarr.

- Download list with progress, speed, ETA; multi-select pause / resume / remove (optionally with files)
- Live updates (Server-Sent Events), survives restarts, resumes partial downloads
- Quality presets: best, ≤2160p, ≤1080p, ≤720p, MP3, M4A; optional subfolder; playlist/channel mode
- Cookies: upload or paste a `cookies.txt`, or mount one at `/config/cookies.txt`
- Sonarr / Radarr: after a download finishes, Spool asks the app to scan and import it
- Add links by paste (Ctrl+V anywhere), drag-and-drop, or a bookmarklet
- Optional login, PUID/PGID, yt-dlp auto-update on start, light/dark, works on phones

## Quick start

```sh
docker compose up -d --build
# open http://localhost:7765
```

Partial downloads live in `/downloads/.incomplete/<id>/` and move into place when finished, so other
apps never see half-written files.

## Configuration

| Variable | Default | |
|---|---|---|
| `PUID` / `PGID` | `1000` | Owner of downloaded files |
| `SPOOL_PORT` | `7765` | |
| `SPOOL_MAX_CONCURRENT` | `2` | Downloads running at once |
| `SPOOL_DEFAULT_PRESET` | `best` | `best`, `2160p`, `1080p`, `720p`, `audio-mp3`, `audio-m4a` |
| `SPOOL_OUTPUT_TEMPLATE` | `%(title)s [%(id)s].%(ext)s` | yt-dlp [output template](https://github.com/yt-dlp/yt-dlp#output-template) |
| `SPOOL_MERGE_FORMAT` | `mkv` | Container used when video and audio are merged |
| `SPOOL_PASSWORD` | *(empty)* | Set to require a login. Do this if Spool is reachable beyond your LAN: it holds your cookies |
| `SPOOL_USER` | `admin` | |
| `SPOOL_AUTO_UPDATE` | `true` | Upgrade yt-dlp on container start |
| `SPOOL_YTDLP_CHANNEL` | `stable` | `nightly` installs pre-releases (useful when a site just broke) |
| `SONARR_URL`, `SONARR_API_KEY`, `SONARR_DOWNLOADS_PATH` | | See below |
| `RADARR_URL`, `RADARR_API_KEY`, `RADARR_DOWNLOADS_PATH` | | See below |
| `SPOOL_ARR_IMPORT_MODE` | `Move` | `Move` or `Copy` |

Volumes: `/config` (database, cookies) and `/downloads`.

## Cookies

Needed for age-restricted, members-only or private videos, and for sites that block anonymous downloads.

1. Install a cookies.txt exporter such as **Get cookies.txt LOCALLY** in your browser.
2. Log in to the site, export cookies in **Netscape** format.
3. In Spool: **Settings → Upload cookies.txt** (or **Paste instead…**). You can also drop the file at `config/cookies.txt`.

Spool validates the file, stores it with mode 600, shows only the cookie count and domains, and gives each
download its own temporary copy (yt-dlp rewrites cookie files as it runs).
Tip for YouTube: export from a private/incognito window and close it afterwards, so the session isn't rotated.

## Sonarr and Radarr

When adding a download, choose **Send to → Sonarr** or **Radarr** and give a **release name** the app can parse:

- Sonarr: `Show Name - S01E05` (or `Show Name - 2024-03-15` for daily shows)
- Radarr: `Movie Name (2024)`

Spool saves the file as `<release name>/<release name>.<ext>` and, when it finishes, calls the app's
`DownloadedEpisodesScan` / `DownloadedMoviesScan` command on that folder. The row shows the outcome
(`imported`, or `not imported` with a hint to use Manual Import). You can resend from the details pane.

Requirements:

- The series or movie must already exist in Sonarr/Radarr.
- Both containers must see the same files. `*_DOWNLOADS_PATH` is the path **inside the Sonarr/Radarr
  container** that corresponds to Spool's `/downloads`. Example: host `/srv/media/downloads/spool` mounted as
  `/downloads` in Spool and as `/data/downloads/spool` in Sonarr → `SONARR_DOWNLOADS_PATH=/data/downloads/spool`.
- Use **Settings → Test** to check the URL and API key (Sonarr → Settings → General → API Key).

## API

| Method | Path | |
|---|---|---|
| `GET` | `/api/jobs` | List downloads |
| `POST` | `/api/jobs` | `{"urls": [...], "preset", "subfolder", "playlist", "arr", "release_name"}` |
| `GET` | `/api/jobs/{id}` | One download |
| `POST` | `/api/jobs/{id}/pause` · `/resume` · `/retry` · `/arr` | |
| `DELETE` | `/api/jobs/{id}?delete_files=true` | |
| `GET` | `/api/jobs/{id}/log` | yt-dlp output |
| `GET` | `/api/events` | Server-Sent Events stream |
| `GET/PUT/DELETE` | `/api/cookies` | `PUT {"text": "..."}` |
| `GET` | `/healthz` | |

## Development

```sh
pip install -r requirements.txt
SPOOL_DOWNLOADS=./downloads SPOOL_CONFIG=./config uvicorn spool.app:app --reload --port 7765
```

## Roadmap

- Act as a download client for Sonarr/Radarr (so they can send grabs *to* Spool)
- Per-site cookie profiles
- Notifications (ntfy / webhook)
- Download finished files from the browser
- yt-dlp update button with in-place restart
