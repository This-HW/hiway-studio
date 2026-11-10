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
| `upload` | **Optional.** Pre-submit multipart file upload (e.g. ComfyUI `/upload/image` for img2img). One block or a **list** (e.g. image + mask). Each block is skipped when the payload has no file for its `file_source`. The response's stored filename lands in a `{store_as}` variable for the submit body. |
| `submit` | POST the generation request (`body_template`, `{..}`-rendered). Response's `job_id_path` becomes the provider job id. |
| `status` | Poll `url_template` (`{job_id}` available). `state_map` translates the provider's states to `queued/running/succeeded/failed`. A missing state or a value outside `state_map` is an **error** (ADR-0014); opt in explicitly with `missing_state` / `unknown_state` (`"queued"`/`"running"`) — ComfyUI's `/history` is `{}` until the prompt finishes, so its seeds set `missing_state: "queued"`. |
| `result` | Where the finished artifact lives: `output_url_path` (string, list, or list of dicts rendered through `output_url_template`), `output_bytes_path` (inline base64), or a separate `url` endpoint (e.g. fal.ai queue). |
| `cancel` | **Optional.** Best-effort cancel endpoint; absent = no-op. |

**`{base_url}`** — scheme://host:port, derived from the submit URL — is
available in the `status`, `result`, and `upload` URL templates. Moving a
provider to another machine means changing the submit URL only (usually one
`env:` line in `.env`).

The full schema with examples lives in the adapter module docstring
(`services/api/app/models_lib/adapters/http_polling.py`) — read that first.

**Provider input contract (ADR-0014).** Every still-image job sends the same
payload keys. `prompt` comes from the generation's `prompt_final` column; the
other inputs are resolved once at enqueue and stored in `params.snapshot`
(thumbnail jobs have no snapshot and use the legacy per-row fields, mapped to
the same keys): `negative_prompt` (`""` when none), `seed`
(always concrete and persisted), `width`/`height`, and — when present — the
LOCAL file paths `init_image` (editing source), `mask`, `reference_image`
(+ `reference_weight`; the full ordered list is `references`). A template
uses them as placeholders; `{<key>_data_uri}` inlines a file as a base64
data URI. Optional keys used as an exact placeholder are dropped from the
body when absent. An editing input (`init_image`/`mask`) the templates do
not consume is rejected before any HTTP call.

**Capabilities.** Declare what the model does next to `http`:
`"tasks": ["text_to_image", "inpaint", ...]` (job types; editing tasks —
inpaint/outpaint/upscale/image_to_image — are never assumed) and
`"inputs": ["negative_prompt", "seed", "init_image", "mask",
"reference_images"]`. Unsupported tasks/editing inputs are a 400 before any
job row or budget reservation. A model without `reference_images` still runs
frames text-only; the snapshot records `references_applied: false` and a
`reference_unsupported` warning.

**Bounds.** One monotonic deadline per provider job
(`HTTP_JOB_TOTAL_DEADLINE_SEC`, submit → end of download) bounds the calls:
every response (JSON and error bodies included) is streamed with the
deadline re-checked per chunk and, on the real transport, per socket read
and per partial send — a provider dripping bytes, or reading a large
request body slowly, cannot stretch a call past it. Known gaps (ADR-0014,
review B7R3): DNS resolution is not bounded; a host with several addresses
gets the remaining time once per connect attempt, so failing connects can
add up past the deadline; TLS-in-TLS proxies may overshoot by one 16 KiB
chunk; the socket bounds rely on httpx/httpcore internals (regressions are
caught by the slow-drip / slow-reader tests). Cancel
has its own short cap (`HTTP_CANCEL_TIMEOUT_SEC`). On the deadline the
adapter calls `cancel` (best effort) and the job fails as
`PROVIDER_UNCERTAIN` (never auto-resubmitted). JSON responses are capped at
`HTTP_JSON_MAX_BYTES` (+ the base64 size of the media cap when they carry
inline output; inline base64 is length-checked before decoding). The result
download is streamed under a per-media byte cap (`HTTP_RESULT_MAX_BYTES_IMAGE/
_VIDEO/_AUDIO`, Content-Length is not trusted) and must have a media
Content-Type (or a generic binary one) and matching magic bytes — an
HTML/JSON `200` is rejected — and an image must be ONE decodable still
frame (Pillow; multi-frame GIF/APNG/WebP are rejected; the image worker
re-checks every adapter's result before publishing). Requests send
`Accept-Encoding: identity`; a compressed response is refused (its
decompression could not be bounded). Redirects are followed by the adapter
itself, at most 5 hops, each redirect body closed unread. Every provider
text or metadata value quoted in a message (error snippets, a `200` body's
`error_path` value, state values, provider job ids, CLI stderr) is
secret-redacted BEFORE it is length-capped. All values live in
`services/api/app/core/defaults.py` (provider HTTP section).

**Job handles.** `submit` returns a local handle
`<remote id>@<model id>#<nonce>`, which the worker persists as
`provider_job_id`; status/result/cancel URLs still receive the provider's
own id as `{job_id}`. Two endpoints that hand out the same remote id never
share state, and a later process can poll/fetch/cancel a persisted handle
(the config is rebuilt from the registry). Rows persisted before this change
hold the bare remote id and stay unknown to a fresh process, as before.

## 2. Adding a new provider (procedure)

1. Copy an existing seed (e.g. `infra/seeds/models/comfyui.json`) to a new
   file in `infra/seeds/models/`.
2. Set `model_id`, `provider_id: "http_polling"`, `modality`, `pricing`,
   and fill the `capabilities.http` phases for the provider's REST dialect.
   Declare `capabilities.tasks` (and `inputs` if the templates do not make
   them obvious) — an editing task (inpaint/outpaint/upscale/image_to_image)
   is rejected unless declared.
3. Put secrets behind `env:` references; add the variable slots to
   `.env.example` (never commit real keys).
4. Keep `status: "disabled"` until the provider is verified, then flip to
   `"active"`.
5. `make seed` loads it (upsert by `model_id`); the router picks it up
   automatically. The registry reads the DB row first and falls back to the
   seed JSON only for models with no row, so after editing a seed — or after
   upgrading an existing install — run `make seed` again. Until then the old
   DB rows win (e.g. before the
   ADR-0014 upgrade is re-seeded, the stub image models do not declare their
   editing tasks, so inpaint/outpaint/upscale requests are a 400).
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
then change `COMFYUI_URL` and re-run a generation. ComfyUI is not part of
`docker-compose.yml`; it always runs as a separate process (GPL-3.0,
see `docs/OSS_PLAN.md`). The seed workflow
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
cancel), but each phase has its OWN `headers` block — the submit headers are
not inherited, so repeat the credential in every phase the proxy protects.
Limitation: the artifact download (ComfyUI `GET /view` built from
`output_url_template`) is sent WITHOUT these headers, so a proxy that also
guards `/view` makes downloads fail with 401. Until a download-specific
credential exists, keep `/view` reachable only from the API host (e.g. the
proxy allows it from that address) instead of forwarding auth headers to
arbitrary artifact URLs. Add `COMFYUI_TOKEN=` to `.env.example` when you use it.

**Input images (img2img):** the `comfyui.i2i-sd15` seed shows the pattern —
the `upload` block POSTs `{init_image}` to `{base_url}/upload/image`
(multipart field `image`, form fields `overwrite=false`, `type=input`) and
`{uploaded_image}` feeds the workflow's `LoadImage` node. The uploaded
filename is unique per submit (`hs-<token>-<basename>`) and the workflow uses
the `name` (+ non-empty `subfolder`, `subfolder_response_path`) the server
returns, so two jobs whose inputs share a basename never read each other's
file. Jobs without `init_image` skip the upload step.

**Inpaint:** `comfyui.inpaint-sd15` uploads two files (an `upload` list):
`{init_image}` → `{uploaded_image}` (`LoadImage`) and `{mask}` →
`{uploaded_mask}` (`LoadImageMask`, red channel), then
`VAEEncodeForInpaint`. Not verified against a real ComfyUI yet (seed stays
`disabled`).

**Negative prompt:** the ComfyUI seeds append `{negative_prompt}` to their
fixed quality negatives, so per-character/global negatives reach the
workflow.

## 3a. CLI providers (codex)

Each CLI job runs in its own directory (`-C <job dir>` under the seed's
`workdir`) and the instruction names an explicit output file there
(`{output_path}` in `prompt_template`, else appended). The PNG at that path
is the result (or a PNG the CLI wrote inside that per-job directory). The
shared `$CODEX_HOME/generated_images` folder is never used: nothing there
proves which job produced a file, so a CLI that ignores the per-job path
fails the job with a clear `PROVIDER_ERROR` instead of guessing. The CLI's
stdout/stderr logs live in the job directory too. `negative_prompt`
goes into the instruction (`{negative}` or an appended "Avoid: ..." line).
The cli backend rejects `init_image`/`mask`. Real codex behavior with the
explicit path is not verified yet.

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
