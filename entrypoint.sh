#!/bin/sh
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Sites change often; keep yt-dlp current. SPOOL_YTDLP_CHANNEL=nightly installs pre-releases.
if [ "${SPOOL_AUTO_UPDATE:-true}" = "true" ]; then
  PRE=""
  [ "${SPOOL_YTDLP_CHANNEL:-stable}" = "nightly" ] && PRE="--pre"
  echo "[spool] updating yt-dlp (${SPOOL_YTDLP_CHANNEL:-stable})..."
  pip install --no-cache-dir --quiet --upgrade $PRE "yt-dlp[default]" \
    || echo "[spool] yt-dlp update failed, continuing with the installed version"
fi
echo "[spool] yt-dlp $(python -c 'import yt_dlp.version as v; print(v.__version__)')"

mkdir -p "$SPOOL_CONFIG" "$SPOOL_DOWNLOADS"

set -- uvicorn spool.app:app --host 0.0.0.0 --port "${SPOOL_PORT:-7765}" --proxy-headers --no-access-log

if [ "$(id -u)" = "0" ]; then
  # Run as PUID:PGID so downloaded files are owned by the host user, not root.
  chown -R "$PUID:$PGID" "$SPOOL_CONFIG"
  chown "$PUID:$PGID" "$SPOOL_DOWNLOADS" 2>/dev/null || true
  export HOME="$SPOOL_CONFIG"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
