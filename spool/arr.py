"""Sonarr / Radarr: after a download finishes, ask the *arr to scan and import the release folder.

Spool and the *arr usually see the downloads volume at different paths, so each client maps
Spool's local path (SPOOL_DOWNLOADS, e.g. /downloads) to the path the *arr sees
(SONARR_DOWNLOADS_PATH / RADARR_DOWNLOADS_PATH, e.g. /data/downloads/spool).
"""
import json
import os
import re
import time
from datetime import date
from difflib import SequenceMatcher
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

from .config import settings

SCAN_COMMAND = {"sonarr": "DownloadedEpisodesScan", "radarr": "DownloadedMoviesScan"}
FINAL_STATES = {"completed", "failed", "aborted", "cancelled", "orphaned"}


class ArrError(Exception):
    pass


class ArrClient:
    def __init__(self, kind: str, url: str, api_key: str, remote_root: str):
        self.kind = kind
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.remote_root = remote_root.rstrip("/") or "/"

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.url}/api/v3{path}",
            data=data,
            method=method,
            headers={"X-Api-Key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 401:
                raise ArrError(f"{self.kind}: API key rejected (401)") from e
            raise ArrError(f"{self.kind}: HTTP {e.code} {detail}") from e
        except urllib.error.URLError as e:
            raise ArrError(f"{self.kind}: cannot reach {self.url} ({e.reason})") from e

    def map_path(self, local: str | Path) -> str:
        rel = Path(local).resolve().relative_to(settings.downloads.resolve())
        return str(PurePosixPath(self.remote_root) / rel.as_posix())

    def test(self) -> dict:
        s = self._req("GET", "/system/status") or {}
        return {"ok": True, "app": s.get("appName", self.kind), "version": s.get("version")}

    def parse(self, title: str) -> dict:
        """Ask the *arr how it would read a release name, without importing anything."""
        r = self._req("GET", f"/parse?title={urllib.parse.quote(title)}") or {}
        if self.kind == "radarr":
            info = r.get("parsedMovieInfo") or {}
            movie = r.get("movie") or {}
            titles = info.get("movieTitles") or [info.get("movieTitle")]
            return {
                "parsed": bool(info),
                "title": titles[0] if titles and titles[0] else None,
                "year": info.get("year") or None,
                "match": f"{movie['title']} ({movie.get('year')})" if movie.get("title") else None,
            }
        info = r.get("parsedEpisodeInfo") or {}
        series = r.get("series") or {}
        return {
            "parsed": bool(info),
            "title": info.get("seriesTitle"),
            "season": info.get("seasonNumber"),
            "episodes": info.get("episodeNumbers") or [],
            "air_date": info.get("airDate"),
            "match": series.get("title"),
            "matched_episodes": [
                {"season": e.get("seasonNumber"), "episode": e.get("episodeNumber"), "title": e.get("title")}
                for e in r.get("episodes") or []
            ],
        }

    def series(self) -> list[dict]:
        return sorted(
            ({"id": x["id"], "title": x["title"], "year": x.get("year")} for x in self._req("GET", "/series") or []),
            key=lambda x: x["title"].lower(),
        )

    def episodes(self, series_id: int) -> list[dict]:
        return self._req("GET", f"/episode?seriesId={int(series_id)}") or []

    def scan(self, local_path: str | Path) -> dict:
        return self._req("POST", "/command", {
            "name": SCAN_COMMAND[self.kind],
            "path": self.map_path(local_path),
            "importMode": settings.arr_import_mode,
        })

    def wait(self, command_id: int, timeout: float = 600) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cmd = self._req("GET", f"/command/{command_id}") or {}
            status = (cmd.get("status") or "").lower()
            if status in FINAL_STATES:
                return status
            time.sleep(2)
        return "timeout"


# ---- matching a video to a Sonarr episode -----------------------------------
STOP = re.compile(r"\b(official|full episode|episode|ep|part|video|hd|4k|1080p|720p)\b")


def _norm(s: str | None, *drop: str | None) -> str:
    s = (s or "").lower()
    for d in drop:
        if d:
            s = s.replace(d.lower(), " ")
    s = STOP.sub(" ", re.sub(r"[^\w\s]", " ", s))
    return re.sub(r"\s+", " ", s).strip()


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if len(b) >= 4 and b in a:            # episode title appears verbatim in the video title
        return 0.95
    ta, tb = set(a.split()), set(b.split())
    overlap = len(ta & tb) / len(tb) if tb else 0.0
    return max(SequenceMatcher(None, a, b).ratio(), overlap * 0.9)


def guess_series(video: dict, series: list[dict]) -> tuple[dict | None, float]:
    """Best library series for a video, judged by channel name and video title."""
    channel, title = _norm(video.get("channel")), _norm(video.get("title"))
    best, best_score = None, 0.0
    for s in series:
        name = _norm(s["title"])
        if not name:
            continue
        score = max(
            SequenceMatcher(None, channel, name).ratio() if channel else 0.0,
            0.9 if re.search(rf"\b{re.escape(name)}\b", title) else 0.0,
            0.9 if channel and re.search(rf"\b{re.escape(name)}\b", channel) else 0.0,
        )
        if score > best_score:
            best, best_score = s, score
    return best, round(best_score, 2)


def match_episodes(video: dict, series: dict, episodes: list[dict], limit: int = 5) -> list[dict]:
    """Rank a series' episodes against a video by air date and title similarity."""
    vtitle = _norm(video.get("title"), series.get("title"), video.get("channel"))
    try:
        vdate = date.fromisoformat(video["upload_date"]) if video.get("upload_date") else None
    except ValueError:
        vdate = None
    out = []
    for e in episodes:
        reasons, date_score = [], 0.0
        if vdate and e.get("airDate"):
            try:
                gap = abs((date.fromisoformat(e["airDate"]) - vdate).days)
            except ValueError:
                gap = 99
            if gap <= 1:                   # 1 day of slack for time zones
                date_score = 1.0 if gap == 0 else 0.85
                reasons.append("air date" if gap == 0 else "air date ±1 day")
        title_score = _similar(vtitle, _norm(e.get("title"), series.get("title")))
        if title_score >= 0.6:
            reasons.append("title")
        score = max(date_score, title_score)
        if date_score and title_score >= 0.6:
            score = min(1.0, score + 0.1)
        if e.get("seasonNumber") == 0:     # specials only when nothing better fits
            score *= 0.9
        if score >= 0.45:
            out.append({
                "season": e.get("seasonNumber"), "episode": e.get("episodeNumber"), "title": e.get("title"),
                "air_date": e.get("airDate"), "score": round(score, 2), "reason": " + ".join(reasons) or "similar title",
            })
    out.sort(key=lambda c: -c["score"])
    return out[:limit]


def release_name(series: dict, season: int, episode: int) -> str:
    return f"{series['title']} - S{season:02d}E{episode:02d}"


def load_clients() -> dict[str, ArrClient]:
    clients = {}
    for kind in ("sonarr", "radarr"):
        url = os.environ.get(f"{kind.upper()}_URL")
        key = os.environ.get(f"{kind.upper()}_API_KEY")
        if url and key:
            remote = os.environ.get(f"{kind.upper()}_DOWNLOADS_PATH", str(settings.downloads))
            clients[kind] = ArrClient(kind, url, key, remote)
    return clients
