"""Cookie store: one Netscape-format cookies.txt in /config, never echoed back over the API."""
import os
import shutil
import uuid
from pathlib import Path

from .config import settings

HEADER = "# Netscape HTTP Cookie File"


def cookie_path() -> Path:
    return settings.config / "cookies.txt"


def parse(text: str) -> tuple[str, int, list[str]]:
    """Validate and normalise cookies.txt content. Returns (normalised_text, entry_count, domains)."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith(("{", "[")):
        raise ValueError("This looks like JSON. Export cookies in Netscape cookies.txt format instead.")
    lines, domains, bad = [], set(), 0
    for line in text.split("\n"):
        s = line.strip()
        if not s or (s.startswith("#") and not s.startswith("#HttpOnly_")):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            bad += 1
            continue
        lines.append(line)
        domains.add(fields[0].removeprefix("#HttpOnly_").lstrip("."))
    if not lines:
        raise ValueError(
            "No valid cookie lines found. Each line needs 7 tab-separated fields (Netscape cookies.txt format)."
        )
    return HEADER + "\n\n" + "\n".join(lines) + "\n", len(lines), sorted(domains)


def save(text: str) -> dict:
    normalised, _, _ = parse(text)
    p = cookie_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(normalised)
    os.replace(tmp, p)
    return status()


def status() -> dict:
    p = cookie_path()
    if not p.exists():
        return {"present": False}
    try:
        _, count, domains = parse(p.read_text(errors="replace"))
    except ValueError as e:
        return {"present": True, "valid": False, "error": str(e), "updated_at": p.stat().st_mtime}
    return {"present": True, "valid": True, "entries": count, "domains": domains, "updated_at": p.stat().st_mtime}


def delete() -> None:
    cookie_path().unlink(missing_ok=True)


def temp_copy() -> Path | None:
    """yt-dlp rewrites the cookie file it is given, so every job gets its own copy."""
    src = cookie_path()
    if not src.exists():
        return None
    d = settings.config / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"cookies-{uuid.uuid4().hex}.txt"
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    return dst
