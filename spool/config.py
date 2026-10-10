"""Runtime settings, all read from environment variables."""
import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.downloads = Path(os.environ.get("SPOOL_DOWNLOADS", "/downloads"))
        self.config = Path(os.environ.get("SPOOL_CONFIG", "/config"))
        self.max_concurrent = max(1, int(os.environ.get("SPOOL_MAX_CONCURRENT", "2")))
        self.output_template = os.environ.get("SPOOL_OUTPUT_TEMPLATE", "%(title)s [%(id)s].%(ext)s")
        self.merge_format = os.environ.get("SPOOL_MERGE_FORMAT", "mkv")
        self.default_preset = os.environ.get("SPOOL_DEFAULT_PRESET", "best")
        self.user = os.environ.get("SPOOL_USER", "admin")
        self.password = os.environ.get("SPOOL_PASSWORD", "")
        # Sonarr / Radarr
        self.arr_import_mode = os.environ.get("SPOOL_ARR_IMPORT_MODE", "Move")  # Move | Copy

    @property
    def db_path(self) -> Path:
        return self.config / "spool.db"

    @property
    def incomplete(self) -> Path:
        return self.downloads / ".incomplete"


settings = Settings()
