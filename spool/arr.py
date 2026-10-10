"""Sonarr / Radarr: after a download finishes, ask the *arr to scan and import the release folder.

Spool and the *arr usually see the downloads volume at different paths, so each client maps
Spool's local path (SPOOL_DOWNLOADS, e.g. /downloads) to the path the *arr sees
(SONARR_DOWNLOADS_PATH / RADARR_DOWNLOADS_PATH, e.g. /data/downloads/spool).
"""
import json
import os
import time
import urllib.error
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


def load_clients() -> dict[str, ArrClient]:
    clients = {}
    for kind in ("sonarr", "radarr"):
        url = os.environ.get(f"{kind.upper()}_URL")
        key = os.environ.get(f"{kind.upper()}_API_KEY")
        if url and key:
            remote = os.environ.get(f"{kind.upper()}_DOWNLOADS_PATH", str(settings.downloads))
            clients[kind] = ArrClient(kind, url, key, remote)
    return clients
