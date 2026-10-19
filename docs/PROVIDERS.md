# Providers — adding one, moving one

This project connects to image/video models through a **seed-driven generic
adapter** (`services/api/app/models_lib/adapters/http_polling.py`). Adding a
provider is **one seed JSON file** — never new code. That is the OSS contract:
anyone can add a model or move to a new machine by editing config.

## 1. Anatomy of a seed (`infra/seeds/models/*.json`)

One model row has `capabilities.http` with up to five phases. Every string
may use `env:VAR_NAME` (resolved from `.env` at call time; missing variable =
clear `MODEL_UNAVAILABLE` naming the variable) and `{placeholders}` (filled
from the submit payload; an exactly-one-placeholder value keeps its type).

| Phase    | What it does |
|----------|--------------|
| `upload` | **Optional.** Pre-submit multipart file upload (e.g. ComfyUI `/upload/image` for img2img). Skipped when the payload has no input file. The response's stored filename lands in a `{store_as}` variable for the submit body. |
| `submit` | POST the generation request (`body_template`, `{..}`-rendered). Response's `job_id_path` becomes the provider job id. |
| `status` | Poll `url_template` (`{job_id}` available). `state_map` translates the provider's states to `queued/running/succeeded/failed`. |
| `result` | Where the finished artifact lives: `output_url_path` (string, list, or list of dicts rendered through `output_url_template`), `output_bytes_path` (inline base64), or a separate `url` endpoint (e.g. fal.ai queue). |
| `cancel` | **Optional.** Best-effort cancel endpoint; absent = no-op. |

**`{base_url}`** — scheme://host:port, derived from the submit URL — is
available in the `status`, `result`, and `upload` URL templates. Moving a
provider to another machine means changing the submit URL only (usually one
`env:` line in `.env`).

The full schema with examples lives in the adapter module docstring
(`services/api/app/models_lib/adapters/http_polling.py`) — read that first.

## 2. Adding a new provider (procedure)

1. Copy an existing seed (e.g. `infra/seeds/models/comfyui.json`) to a new
   file in `infra/seeds/models/`.
2. Set `model_id`, `provider_id: "http_polling"`, `modality`, `pricing`,
   and fill the `capabilities.http` phases for the provider's REST dialect.
3. Put secrets behind `env:` references; add the variable slots to
   `.env.example` (never commit real keys).
4. Keep `status: "disabled"` until the provider is verified, then flip to
   `"active"`.
5. `make seed` loads it (upsert by `model_id`); the router picks it up
   automatically.
6. Add a MockTransport test in `services/api/tests/test_http_polling_adapter.py`
   asserting the exact HTTP sequence (upload → submit → poll → result).

No Python file is touched. The adapter interface (`submit/poll/cancel/
estimate/fetch_result`) is fixed by `app/models_lib/base.py`; seeds only
configure the one generic implementation.

## 3. ComfyUI: moving machines

`COMFYUI_URL` in `.env` is the **only** thing that names the machine
(full submit URL, `/prompt` included). Everything else (`/history`,
`/view`, `/upload/image`) follows `{base_url}`.

| Setup | `COMFYUI_URL` |
|-------|---------------|
| Same machine as the studio | `http://127.0.0.1:8188/prompt` |
| Studio in Docker, ComfyUI on the Docker host | `http://host.docker.internal:8188/prompt` (Linux: add `extra_hosts: ["host.docker.internal:host-gateway"]` to compose) |
| Remote GPU machine | Point at a **token proxy**, not ComfyUI directly (see below) |

Checklist when moving: ComfyUI running in API mode (`--listen` if remote),
checkpoint file in `models/checkpoints/` matching the seed's `ckpt_name`,
then change `COMFYUI_URL` and re-run a generation. The seed workflow
(`infra/seeds/models/comfyui.json`) needs no edits.

**Remote access:** do not expose ComfyUI itself to the network. Put a tiny
authenticated reverse proxy in front (e.g. Caddy `basicauth` or a header
check) and add the credential to the seed:

```json
"submit": {
  "url": "env:COMFYUI_URL",
  "headers": {"Authorization": "env:COMFYUI_TOKEN"}
}
```

`headers` supports `env:` on every phase (submit/status/result/upload/
cancel). Add `COMFYUI_TOKEN=` to `.env.example` when you use it.

**Input images (img2img):** the `comfyui.i2i-sd15` seed shows the pattern —
the `upload` block POSTs `{init_image}` to `{base_url}/upload/image`
(multipart field `image`, form fields `overwrite=true`, `type=input`) and
`{uploaded_image}` feeds the workflow's `LoadImage` node. Jobs without
`init_image` skip the upload step.

## 4. External provider candidates

Any submit → poll → fetch REST API fits the adapter. Candidates worth a
seed (verify pricing/quality before use):

- **fal.ai** — queue API (`submit` → `requests/{id}/status` → result
  endpoint). Auth via `env:FAL_API_KEY`.
- **Replicate** — predictions API (same shape). Auth via
  `env:REPLICATE_API_TOKEN`.
- **Higgsfield** — official developer API (`open.higgsfield.ai`): one key,
  50+ image/video models, prepaid USD balance, submit → poll → fetch with a
  `cancel_url`. Same three-call shape as above; a strong candidate when
  local GPU is unavailable. Pricing is per generation in USD — measure
  before relying on it.

## 5. QC vision is separate

Generation providers and the QC vision scorer are **independent
abstractions** on purpose: generation is submit/poll/fetch, vision scoring
is a chat-style vision call. Wiring a vision model for the advisory QC
checks (`story_fidelity`, `performance_quality`) does not go through the
model registry — see `services/api/app/services/vision_client.py`
(generic OpenAI-compatible `POST {base_url}/chat/completions`, configured
purely by env: `QC_VISION_BASE_URL` / `QC_VISION_API_KEY` /
`QC_VISION_MODEL` / `QC_VISION_TIMEOUT_SEC`; any endpoint works — Ollama
`/v1`, vLLM, LM Studio, OpenAI, ...). Unset = the two checks stay
stub+warn; misbehaving endpoints degrade to warn — the render gate is
never blocked.
