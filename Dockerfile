FROM python:3.12-slim

# Deno: yt-dlp needs a JavaScript runtime for YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SPOOL_DOWNLOADS=/downloads \
    SPOOL_CONFIG=/config \
    SPOOL_PORT=7765 \
    PUID=1000 \
    PGID=1000

RUN python -m venv /opt/venv
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY spool /app/spool
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app
VOLUME ["/config", "/downloads"]
EXPOSE 7765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"SPOOL_PORT\"]}/healthz', timeout=4)"

ENTRYPOINT ["/entrypoint.sh"]
