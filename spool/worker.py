"""Download queue: a dispatcher thread hands queued jobs to worker threads running yt-dlp."""
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from . import cookies
from .arr import ArrClient, ArrError
from .config import settings
from .db import DB

PRESETS = {
    "best":      {"label": "Best available",  "format": "bv*+ba/b"},
    "2160p":     {"label": "Up to 2160p",     "format": "bv*[height<=2160]+ba/b[height<=2160]/b"},
    "1080p":     {"label": "Up to 1080p",     "format": "bv*[height<=1080]+ba/b[height<=1080]/b"},
    "720p":      {"label": "Up to 720p",      "format": "bv*[height<=720]+ba/b[height<=720]/b"},
    "audio-mp3": {"label": "Audio only (MP3)", "format": "ba/b", "audio": "mp3"},
    "audio-m4a": {"label": "Audio only (M4A)", "format": "ba/b", "audio": "m4a"},
}

MEDIA_EXT = {".mkv", ".mp4", ".webm", ".m4v", ".mov", ".avi", ".mp3", ".m4a", ".opus", ".flac", ".ogg"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def safe_relpath(sub: str | None) -> Path | None:
    if not sub:
        return None
    parts = [p for p in Path(sub.replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
    return Path(*parts) if parts else None


def safe_name(name: str) -> str:
    name = re.sub(r'[\x00-\x1f/\\]', " ", name).strip().lstrip(".")
    return re.sub(r"\s+", " ", name)[:200]


def inside_downloads(p: Path) -> bool:
    try:
        rp, root = p.resolve(), settings.downloads.resolve()
        return rp != root and rp.is_relative_to(root)
    except OSError:
        return False


class JobLogger:
    def __init__(self, mgr: "Manager", job_id: int):
        self.mgr, self.job_id = mgr, job_id

    def _log(self, msg: str) -> None:
        msg = ANSI.sub("", msg)
        if msg.startswith("[debug]") or msg.startswith("[download] ") and "%" in msg:
            return
        self.mgr.db.append_log(self.job_id, msg)

    def debug(self, msg): self._log(msg)
    def info(self, msg): self._log(msg)
    def warning(self, msg): self._log(f"WARNING: {msg}")
    def error(self, msg): self._log(msg)


class Manager:
    def __init__(self, db: DB, publish: Callable[[dict], None], arr: dict[str, ArrClient]):
        self.db, self.publish, self.arr = db, publish, arr
        self.cond = threading.Condition()
        self.active: set[int] = set()
        self.cancel: dict[int, str] = {}  # job id -> "pause" | "remove" | "remove+files"

    # ---- public API -------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._dispatch, name="spool-dispatch", daemon=True).start()

    def kick(self) -> None:
        with self.cond:
            self.cond.notify_all()

    def is_active(self, job_id: int) -> bool:
        return job_id in self.active

    def stop(self, job_id: int, reason: str) -> bool:
        """Ask a running job to stop. Returns False if it isn't running."""
        with self.cond:
            if job_id not in self.active:
                return False
            self.cancel[job_id] = reason
            return True

    def remove(self, job: dict, delete_files: bool) -> None:
        if delete_files:
            for f in job["files"]:
                p = Path(f)
                if inside_downloads(p):
                    p.unlink(missing_ok=True)
            if job["release_name"] and job["output_path"]:
                rd = Path(job["output_path"])
                if inside_downloads(rd) and rd.is_dir():
                    shutil.rmtree(rd, ignore_errors=True)
        shutil.rmtree(settings.incomplete / str(job["id"]), ignore_errors=True)
        self.db.delete(job["id"])
        self.publish({"type": "removed", "id": job["id"]})

    def send_to_arr(self, job_id: int) -> None:
        threading.Thread(target=self._arr_import, args=(job_id,), daemon=True).start()

    # ---- internals --------------------------------------------------------
    def _set(self, job_id: int, **fields) -> dict | None:
        job = self.db.update(job_id, **fields)
        if job:
            self.publish({"type": "job", "job": job})
        return job

    def _dispatch(self) -> None:
        while True:
            with self.cond:
                job = None
                while job is None:
                    if len(self.active) < settings.max_concurrent:
                        job = self.db.next_queued(list(self.active))
                    if job is None:
                        self.cond.wait(timeout=5)
                self.active.add(job["id"])
            threading.Thread(target=self._run, args=(job,), name=f"spool-job-{job['id']}", daemon=True).start()

    def _run(self, job: dict) -> None:
        try:
            self._download(job)
        except Exception as e:  # never let a job kill its thread silently
            self._set(job["id"], status="error", error=f"Internal error: {e}", speed=None, eta=None)
        finally:
            with self.cond:
                self.active.discard(job["id"])
                self.cancel.pop(job["id"], None)
                self.cond.notify_all()

    def _paths(self, job: dict) -> tuple[Path, str, Path | None]:
        home = settings.downloads
        sub = safe_relpath(job["subfolder"])
        if sub:
            home = home / sub
        if job["release_name"]:
            name = safe_name(job["release_name"])
            release_dir = home / name
            esc = name.replace("%", "%%")
            tmpl = f"{esc} - %(playlist_index)s - %(title)s.%(ext)s" if job["playlist"] else f"{esc}.%(ext)s"
            return release_dir, tmpl, release_dir
        return home, settings.output_template, None

    def _download(self, job: dict) -> None:
        jid = job["id"]
        preset = PRESETS.get(job["preset"] or "", PRESETS["best"])
        home, outtmpl, release_dir = self._paths(job)
        tmp_dir = settings.incomplete / str(jid)
        home.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        self._set(jid, status="downloading", error=None, speed=None, eta=None)
        self.db.append_log(jid, f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting ({preset['label']}) ---")

        produced: set[str] = set()
        state = {"last": 0.0, "titled": bool(job["title"])}

        def check_cancel():
            reason = self.cancel.get(jid)
            if reason:
                raise DownloadCancelled(reason)

        def progress_hook(d):
            check_cancel()
            info = d.get("info_dict") or {}
            fields = {}
            if not state["titled"] and info.get("title"):
                state["titled"] = True
                fields.update(title=info.get("title"), thumbnail=info.get("thumbnail"))
            st = d.get("status")
            if st == "downloading":
                now = time.monotonic()
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                state["done"], state["total"] = done, total
                if now - state["last"] < 0.5 and not fields:
                    return
                state["last"] = now
                fields.update(
                    progress=round(done / total * 100, 1) if total else None,
                    speed=d.get("speed"), eta=d.get("eta"), size=total, downloaded=done,
                )
            elif st == "finished":
                total = d.get("total_bytes") or d.get("downloaded_bytes")
                fields.update(progress=100.0, speed=None, eta=None, size=total, downloaded=total)
            if fields:
                self._set(jid, **fields)

        def pp_hook(d):
            check_cancel()
            info = d.get("info_dict") or {}
            if d.get("status") == "started" and d.get("postprocessor") != "MoveFiles":
                self._set(jid, status="processing", speed=None, eta=None)
            if d.get("status") == "finished" and info.get("filepath"):
                produced.add(info["filepath"])

        opts = {
            "format": preset["format"],
            "outtmpl": {"default": outtmpl},
            "paths": {"home": str(home), "temp": str(tmp_dir)},
            "noplaylist": not job["playlist"],
            "ignoreerrors": "only_download" if job["playlist"] else False,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [pp_hook],
            "logger": JobLogger(self, jid),
            "noprogress": True,
            "continuedl": True,
            "retries": 10,
            "fragment_retries": 10,
            "merge_output_format": settings.merge_format,
        }
        if preset.get("audio"):
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": preset["audio"], "preferredquality": "0"}]

        cookie_file = cookies.temp_copy()
        if cookie_file:
            opts["cookiefile"] = str(cookie_file)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job["url"], download=True)
            check_cancel()
            files = sorted({f for f in produced | set(_collect(info)) if Path(f).exists()})
            if not files:
                raise RuntimeError("yt-dlp finished but no output file was found")
            output = release_dir or (Path(files[0]) if len(files) == 1 else home)
            total = sum(Path(f).stat().st_size for f in files)
            self._set(jid, status="done", progress=100.0, speed=None, eta=None, files=files,
                      size=total, downloaded=total,
                      output_path=str(output), finished_at=time.time(),
                      title=job["title"] or (info or {}).get("title"),
                      arr_status="queued" if job["arr"] else None)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if job["arr"]:
                self.send_to_arr(jid)
        except DownloadCancelled:
            reason = self.cancel.get(jid, "pause")
            if reason.startswith("remove"):
                self.remove(self.db.get(jid) or job, delete_files=reason == "remove+files")
            else:
                extra = {}
                if state.get("total"):
                    extra = dict(downloaded=state["done"], size=state["total"],
                                 progress=round(state["done"] / state["total"] * 100, 1))
                self._set(jid, status="paused", speed=None, eta=None, **extra)
        except Exception as e:
            msg = ANSI.sub("", str(e)).removeprefix("ERROR: ")
            self.db.append_log(jid, f"ERROR: {msg}")
            self._set(jid, status="error", error=msg, speed=None, eta=None)
        finally:
            if cookie_file:
                cookie_file.unlink(missing_ok=True)

    def _arr_import(self, job_id: int) -> None:
        job = self.db.get(job_id)
        if not job or not job["arr"]:
            return
        client = self.arr.get(job["arr"])
        if not client:
            self._set(job_id, arr_status=f"{job['arr']} is not configured")
            return
        target = Path(job["output_path"])
        try:
            self._set(job_id, arr_status="scanning")
            cmd = client.scan(target)
            self.db.append_log(job_id, f"{client.kind}: requested scan of {client.map_path(target)} (command {cmd.get('id')})")
            result = client.wait(cmd["id"])
        except (ArrError, KeyError, TypeError) as e:
            self.db.append_log(job_id, f"ERROR: {e}")
            self._set(job_id, arr_status=f"error: {e}")
            return
        if result != "completed":
            self._set(job_id, arr_status=f"scan {result}")
            return
        leftover = [f for f in target.rglob("*") if f.suffix.lower() in MEDIA_EXT] if target.is_dir() else (
            [target] if target.exists() else [])
        if settings.arr_import_mode.lower() == "move" and leftover:
            self._set(job_id, arr_status=f"not imported (check {client.kind} → Activity → Manual Import)")
        else:
            self._set(job_id, arr_status="imported" if settings.arr_import_mode.lower() == "move" else "scan completed")


def _collect(info) -> list[str]:
    out: list[str] = []
    if not isinstance(info, dict):
        return out
    for rd in info.get("requested_downloads") or []:
        fp = rd.get("filepath") or rd.get("filename")
        if fp:
            out.append(fp)
    for e in info.get("entries") or []:
        out += _collect(e)
    return out
