"""Spool: a small web UI around yt-dlp."""
import asyncio
import base64
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import yt_dlp.version
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, cookies
from .arr import ArrError, guess_series, load_clients, match_episodes, release_name
from .config import settings
from .db import DB
from .worker import PRESETS, Manager, probe

STATIC = Path(__file__).parent / "static"


class Bus:
    """Fans events out to every connected browser (Server-Sent Events)."""

    def __init__(self):
        self.subs: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, event: dict) -> None:  # callable from any thread
        if self.loop:
            self.loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: dict) -> None:
        for q in list(self.subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


bus = Bus()
settings.downloads.mkdir(parents=True, exist_ok=True)
db = DB(settings.db_path)
arr_clients = load_clients()
manager = Manager(db, bus.publish, arr_clients)


@asynccontextmanager
async def lifespan(_app):
    bus.loop = asyncio.get_running_loop()
    db.requeue_interrupted()
    manager.start()
    yield


app = FastAPI(title="Spool", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if settings.password and request.url.path != "/healthz":
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, settings.user) & secrets.compare_digest(pw, settings.password)
            except Exception:
                ok = False
        if not ok:
            return PlainTextResponse("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Spool"'})
    return await call_next(request)


# ---- models ---------------------------------------------------------------
class AddJobs(BaseModel):
    urls: list[str] = Field(min_length=1)
    preset: str = settings.default_preset
    subfolder: str | None = None
    playlist: bool = False
    arr: str | None = None            # None | "sonarr" | "radarr"
    release_name: str | None = None   # required when arr is set


class MatchReq(BaseModel):
    url: str
    series_id: int | None = None      # None = guess from channel / video title


class CookieText(BaseModel):
    text: str


# ---- jobs -----------------------------------------------------------------
def _job_or_404(job_id: int) -> dict:
    job = db.get(job_id)
    if not job:
        raise HTTPException(404, "No such download")
    return job


def _publish(job: dict) -> dict:
    bus.publish({"type": "job", "job": job})
    return job


@app.get("/api/jobs")
def list_jobs():
    return db.list()


@app.post("/api/jobs", status_code=201)
def add_jobs(body: AddJobs):
    urls = [u.strip() for u in body.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "Add at least one URL")
    for u in urls:
        if not u.lower().startswith(("http://", "https://")):
            raise HTTPException(400, f"Not a http(s) URL: {u}")
    if body.preset not in PRESETS:
        raise HTTPException(400, f"Unknown preset {body.preset}")
    arr = (body.arr or "").lower() or None
    release = (body.release_name or "").strip() or None
    if arr:
        if arr not in ("sonarr", "radarr"):
            raise HTTPException(400, "arr must be sonarr or radarr")
        if arr not in arr_clients:
            raise HTTPException(400, f"{arr.capitalize()} is not configured")
        if not release:
            raise HTTPException(400, f"{arr.capitalize()} needs a release name it can parse, "
                                     "e.g. 'Show Name - S01E05' or 'Movie Name (2024)'")
    if release and len(urls) > 1:
        raise HTTPException(400, "A release name applies to a single URL")
    created = []
    for u in urls:
        job = db.create(url=u, preset=body.preset, subfolder=body.subfolder, playlist=int(body.playlist),
                        arr=arr, release_name=release)
        created.append(_publish(job))
    manager.kick()
    return created


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    return _job_or_404(job_id)


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: int):
    job = _job_or_404(job_id)
    if manager.stop(job_id, "pause"):
        return {"ok": True}
    if job["status"] == "queued":
        return _publish(db.update(job_id, status="paused"))
    return job


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: int):
    job = _job_or_404(job_id)
    if job["status"] in ("paused", "error"):
        job = _publish(db.update(job_id, status="queued", error=None))
        manager.kick()
    return job


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int):
    job = _job_or_404(job_id)
    if manager.is_active(job_id):
        raise HTTPException(409, "Download is running")
    job = _publish(db.update(job_id, status="queued", error=None, progress=0, arr_status=None))
    manager.kick()
    return job


@app.post("/api/jobs/{job_id}/arr")
def resend_to_arr(job_id: int):
    job = _job_or_404(job_id)
    if job["status"] != "done" or not job["arr"]:
        raise HTTPException(400, "Only finished downloads with Sonarr/Radarr set can be sent")
    manager.send_to_arr(job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int, delete_files: bool = False):
    job = _job_or_404(job_id)
    if manager.stop(job_id, "remove+files" if delete_files else "remove"):
        return {"ok": True, "pending": True}
    manager.remove(job, delete_files)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
def job_log(job_id: int):
    _job_or_404(job_id)
    return db.get_log(job_id)


# ---- live updates ---------------------------------------------------------
@app.get("/api/events")
async def events(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    bus.subs.add(q)

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.subs.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- cookies --------------------------------------------------------------
@app.get("/api/cookies")
def get_cookies():
    return cookies.status()


@app.put("/api/cookies")
def put_cookies(body: CookieText):
    try:
        st = cookies.save(body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    bus.publish({"type": "cookies", "cookies": st})
    return st


@app.delete("/api/cookies")
def delete_cookies():
    cookies.delete()
    st = cookies.status()
    bus.publish({"type": "cookies", "cookies": st})
    return st


# ---- integrations / info --------------------------------------------------
@app.post("/api/arr/{kind}/test")
def test_arr(kind: str):
    client = arr_clients.get(kind)
    if not client:
        raise HTTPException(404, f"{kind} is not configured")
    try:
        return client.test()
    except ArrError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 502)


@app.get("/api/arr/{kind}/parse")
def parse_release(kind: str, title: str):
    client = arr_clients.get(kind)
    if not client:
        raise HTTPException(404, f"{kind} is not configured")
    if not title.strip():
        raise HTTPException(400, "Enter a release name to check")
    try:
        return client.parse(title.strip())
    except ArrError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 502)


def _sonarr():
    client = arr_clients.get("sonarr")
    if not client:
        raise HTTPException(404, "sonarr is not configured")
    return client


@app.get("/api/arr/sonarr/series")
def sonarr_series():
    try:
        return _sonarr().series()
    except ArrError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 502)


@app.post("/api/arr/sonarr/match")
def sonarr_match(body: MatchReq):
    """Work out which Sonarr episode a video URL is, from its title / upload date."""
    client = _sonarr()
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Enter the video URL first")
    try:
        video = probe(url)
    except ValueError as e:
        raise HTTPException(422, f"Could not read the video: {e}")
    try:
        library = client.series()
        if body.series_id is not None:
            series = next((s for s in library if s["id"] == body.series_id), None)
            if not series:
                raise HTTPException(404, "That series is no longer in Sonarr")
            series_score = None
        else:
            series, series_score = guess_series(video, library)
            if not series or series_score < 0.6:
                return {"video": video, "series": None, "candidates": [],
                        "error": "Couldn't tell which series this is; pick it from the list"}
        episodes = client.episodes(series["id"])
        candidates = []
        # 1. the site itself reports season/episode numbers
        if video["season"] is not None and video["episode"] is not None:
            hit = next((e for e in episodes if e.get("seasonNumber") == video["season"]
                        and e.get("episodeNumber") == video["episode"]), None)
            if hit:
                candidates.append({"season": hit["seasonNumber"], "episode": hit["episodeNumber"],
                                   "title": hit.get("title"), "air_date": hit.get("airDate"),
                                   "score": 1.0, "reason": "site metadata"})
        # 2. the video title is already a parseable release name (e.g. contains S01E05)
        if not candidates and video["title"]:
            parsed = client.parse(video["title"])
            if parsed.get("match") == series["title"]:
                candidates += [{**e, "air_date": None, "score": 1.0, "reason": "parsed by Sonarr"}
                               for e in parsed["matched_episodes"]]
        # 3. air date / title similarity
        seen = {(c["season"], c["episode"]) for c in candidates}
        candidates += [c for c in match_episodes(video, series, episodes) if (c["season"], c["episode"]) not in seen]
    except ArrError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 502)
    for c in candidates:
        c["release_name"] = release_name(series, c["season"], c["episode"])
    return {"video": video, "series": {**series, "guess_score": series_score}, "candidates": candidates[:5]}


@app.get("/api/info")
def info():
    return {
        "version": __version__,
        "ytdlp_version": yt_dlp.version.__version__,
        "presets": {k: v["label"] for k, v in PRESETS.items()},
        "default_preset": settings.default_preset,
        "max_concurrent": settings.max_concurrent,
        "arr": {k: {"url": c.url, "downloads_path": c.remote_root} for k, c in arr_clients.items()},
        "arr_import_mode": settings.arr_import_mode,
        "auth": bool(settings.password),
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
