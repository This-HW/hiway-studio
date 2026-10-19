FROM python:3.12-slim

# ffmpeg + ffprobe: clip normalization and timeline rendering shell out to
# the system binaries (app/media/ffmpeg_ops.py; FFMPEG_BIN override supported).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Codex CLI: subscription-based image generation for the `cli` provider
# adapter (app/models_lib/adapters/cli.py). The worker shells out to `codex`;
# auth comes from CODEX_HOME mounted from the host (see docker-compose.yml).
# Run `codex login` on the host once; the container reuses it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @openai/codex \
    && rm -rf /var/lib/apt/lists/* ~/.npm

WORKDIR /code

COPY pyproject.toml .
RUN pip install -e .

COPY . .

EXPOSE 8000
