# syntax=docker/dockerfile:1
# The Mnema server image: ./install.sh server runs it with compose.yaml, data and settings in
# the /data volume.
# CPU only: torch, torchaudio and torchvision come from the PyTorch CPU index instead of the
# locked PyPI wheels, which on x86_64 pull several GB of CUDA libraries.

ARG PYTHON_IMAGE=python:3.12-slim-trixie

FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM ${PYTHON_IMAGE} AS deps
# git: GigaAM is a git dependency
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY mnema/extras.py mnema/extras.py
# The locked set of the server role (mnema/extras.py) without the project itself: its own layer
# of the image, unchanged while uv.lock is. The torch family and its CUDA wheels are swapped for
# CPU builds of the same versions.
RUN --mount=type=cache,target=/root/.cache/uv <<'EOF'
set -eu
extras=""
for extra in $(python -I mnema/extras.py server); do extras="$extras --extra $extra"; done
uv export --frozen --no-dev $extras --no-emit-project --no-hashes > /tmp/locked.txt
skip=""
for name in $(grep -oE '^(torch|torchaudio|torchvision|triton|nvidia-[a-z0-9-]+)==' /tmp/locked.txt | tr -d '='); do
  skip="$skip --no-install-package $name"
done
# torchcodec (decoding in pyannote, Whisper's diarization, unused) has x86_64 wheels only
if [ "$(uname -m)" != x86_64 ]; then skip="$skip --no-install-package torchcodec"; fi
uv sync --frozen --no-dev --no-install-project $extras $skip
uv pip install --python /app/.venv/bin/python --no-deps \
  --index-url https://download.pytorch.org/whl/cpu \
  $(grep -oE '^(torch|torchaudio|torchvision)==[^ ;]+' /tmp/locked.txt)
echo "$extras $skip" > /tmp/sync-options
EOF

FROM deps AS project
COPY mnema mnema
# The project from the same lock; its own files (RECORD of the wheel) go to /layer, the image's
# small last layer.
RUN --mount=type=cache,target=/root/.cache/uv <<'EOF'
set -eu
# inexact: an exact sync would remove the CPU torch the lock does not know; reinstall: uv keys
# its cached build of the project on pyproject.toml, not on the code
uv sync --frozen --no-dev --no-editable --inexact --reinstall-package mnema $(cat /tmp/sync-options)
/app/.venv/bin/python -I - <<'PY'
import csv, pathlib, shutil
from importlib.metadata import distribution

dist = distribution("mnema")
for row in csv.reader(dist.read_text("RECORD").splitlines()):
    source = (pathlib.Path(dist.locate_file(row[0]))).resolve()
    target = pathlib.Path("/layer") / source.relative_to("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
PY
EOF

FROM ${PYTHON_IMAGE}
# ffmpeg for media, libgomp for torch on CPU, iproute2 for the host's address at install
# (./install.sh server), tini as PID 1 reaps the processes of finished jobs
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 iproute2 tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --home-dir /data --no-create-home --shell /usr/sbin/nologin mnema \
    && mkdir /data \
    && chown mnema:mnema /data
COPY --from=deps /app/.venv /app/.venv
COPY --from=project /layer/ /
# the engines import with the CPU torch
RUN /app/.venv/bin/python -B -c "import torch, torchaudio, whisperx, gigaam, mnema.server; \
assert not torch.cuda.is_available()"
# Everything that must survive a new container lives in /data: settings (config/mnema), the
# server folder (library, .mnema: jobs, devices, uploads) and model weights (cache). Any user
# may run the image (compose.yaml runs it as the owner of the data folder).
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/data \
    XDG_CONFIG_HOME=/data/config \
    XDG_CACHE_HOME=/data/cache \
    HF_HOME=/data/cache/huggingface \
    MNEMA_SERVER_DIR=/data \
    MNEMA_LIBRARY=true \
    MNEMA_CONTAINER=true \
    PYTHONUNBUFFERED=1
USER mnema
WORKDIR /data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --start-interval=2s --retries=3 \
    CMD ["python", "-m", "mnema.install_setup", "health"]
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "mnema.install_setup", "container"]
LABEL org.opencontainers.image.source="https://github.com/fozt/mnema" \
      org.opencontainers.image.description="Mnema server: transcription, titles and summaries of meeting recordings" \
      org.opencontainers.image.licenses="MIT"
