# ---------------------------------------------------------------------------
# One image, everything in it. FFmpeg and the Piper voice are baked in rather
# than fetched at boot, because a container that downloads a 60 MB model on
# every restart will eventually start during an outage and fail to come up.
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    PIPER_VOICE_DIR=/app/voices

# ffmpeg for rendering; espeak-ng is Piper's phonemiser; the fonts are what
# the thumbnail engine draws with and it refuses to run without one.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      espeak-ng \
      fonts-liberation \
      fonts-dejavu-core \
      curl \
      tini \
      gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# The narration voice. Pinned by URL so a rebuild produces the same voice --
# a documentary series that changes narrator between episodes is worse than
# one that never updates its model.
# ---------------------------------------------------------------------------
ARG PIPER_VOICE=en_GB-alan-medium
ARG VOICE_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium
RUN mkdir -p /app/voices && \
    curl -fsSL -o "/app/voices/${PIPER_VOICE}.onnx" \
         "${VOICE_BASE}/${PIPER_VOICE}.onnx" && \
    curl -fsSL -o "/app/voices/${PIPER_VOICE}.onnx.json" \
         "${VOICE_BASE}/${PIPER_VOICE}.onnx.json" && \
    test -s "/app/voices/${PIPER_VOICE}.onnx"

COPY echoes ./echoes
COPY assets ./assets

COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# Writable state. On Railway this path must be a mounted volume, or every
# redeploy discards work in progress. The doctor command reports whether it
# actually looks like a volume.
#
# Note there is no USER directive. The container starts as root so that
# entrypoint.sh can take ownership of a freshly mounted volume -- which
# arrives owned by root and would otherwise be unwritable -- and the
# entrypoint then drops to this user with gosu. The application never runs
# as root.
RUN mkdir -p /app/data/media /app/data/work /app/data/cache && \
    useradd -r -u 10001 -m echoes && \
    chown -R echoes:echoes /app && \
    chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080

# tini reaps the ffmpeg children a render spawns; without it a long run
# accumulates zombies until the container runs out of processes.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/healthz" || exit 1

CMD ["sh", "-c", "python -m echoes.cli migrate && exec uvicorn echoes.api.app:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive 75"]
