FROM python:3.12-slim

# Build context is the REPO ROOT (docker build -f services/api/Dockerfile .).
# The image mirrors the repo layout (/repo/services/api, /repo/packages/contracts,
# /repo/infra/seeds, /repo/tools) so repo-relative lookups keep working.
#
# ffmpeg + ffprobe: clip normalization and timeline rendering shell out to
# the system binaries (app/media/ffmpeg_ops.py; FFMPEG_BIN override supported).
# fonts-noto-cjk + fontconfig: libass burn-in subtitles and thumbnail text need
# a CJK font, otherwise Korean renders as tofu boxes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# Codex CLI: subscription-based image generation for the `cli` provider
# adapter (app/models_lib/adapters/cli.py). The worker shells out to `codex`;
# auth comes from CODEX_HOME mounted from the host (see docker-compose.yml).
# Run `codex login` on the host once; the container reuses it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @openai/codex \
    && rm -rf /var/lib/apt/lists/* ~/.npm

WORKDIR /repo/services/api

# Korean OCR recognition model for QC text checks and the render burn-in
# postflight: fetched (SHA-256 verified by app/media/ocr.py) at BUILD time so
# a container without network can still verify burned-in subtitles instead of
# failing every postflight as "not verifiable". ocr.py is stdlib-only at
# import, so this layer only rebuilds when ocr.py changes.
ENV OCR_MODELS_DIR=/opt/ai-studio/ocr
COPY services/api/app/media/ocr.py /tmp/ocr_fetch/ocr.py
RUN python -c "import sys; sys.path.insert(0, '/tmp/ocr_fetch'); import ocr; print(ocr._ensure_rec_model(ocr._models_dir()))" \
    && rm -rf /tmp/ocr_fetch

# Editable install needs the package sources, so copy them before installing.
COPY services/api/ /repo/services/api/
RUN pip install -e ".[test]"

COPY packages/contracts/ /repo/packages/contracts/
COPY infra/seeds/ /repo/infra/seeds/
COPY tools/ /repo/tools/

EXPOSE 8000
