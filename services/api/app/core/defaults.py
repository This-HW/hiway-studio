"""Single home for every `[가정]` default (design report §13, confirmed by HW 2026-09-30).

Rule: no magic numbers in business code. Import from here instead.
Public layer-1 defaults; env (config.py) and DB override them.
"""
from __future__ import annotations

# --- #1 retry: max 3 retries, exponential backoff 5s -> 10s -> 20s + jitter ---
RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_BASE_SEC: float = 5.0
RETRY_JITTER_SEC: float = 2.0

# --- #2 queue saturation ---
QUEUE_MAX_PENDING: int = 100
QUEUE_FULL_RETRY_AFTER_SEC: int = 60

# Frame batch size cap ([assumption] #F-06: default 1, max 4).
FRAME_BATCH_MAX: int = 4

# Still-image generation size. Unlike video (which has a per-clip resolution),
# still generations have no studio-level resolution parameter yet, so the
# worker fills the provider payload with this default (the ComfyUI seed
# template needs concrete {width}/{height}). 512 keeps SD1.5-class models in
# their trained regime.
IMAGE_DEFAULT_WIDTH: int = 512
IMAGE_DEFAULT_HEIGHT: int = 512

# --- #3 worker concurrency per queue ---
WORKER_CONCURRENCY: dict[str, int] = {"image": 4, "video": 2, "audio": 2, "post": 2}

# --- #4 output normalization (default preset; per-preset values come from export_presets) ---
OUTPUT_WIDTH: int = 1920
OUTPUT_HEIGHT: int = 1080
OUTPUT_FPS: int = 24
OUTPUT_VIDEO_CODEC: str = "h264"
OUTPUT_AUDIO_CODEC: str = "aac"
OUTPUT_AUDIO_SAMPLE_RATE: int = 48000
OUTPUT_LETTERBOX_ON_MISMATCH: bool = True

# --- #5 budget ---
BUDGET_WARN_RATIO: float = 0.8
BUDGET_BLOCK_RATIO: float = 1.0

# --- #6 subtitle QC thresholds ---
SUBTITLE_CUE_MIN_SEC: float = 1.0
SUBTITLE_CUE_MAX_SEC: float = 7.0
SUBTITLE_CPS_WARN: int = 20

# --- #7 continuity QC thresholds ---
QC_SSIM_FIRST_FRAME: float = 0.90
QC_SSIM_BETWEEN_SHOTS: float = 0.85
# Render postflight: share of a cue's characters the burned-in frame OCR must
# recover for the subtitle to count as visible (plan D5/D8, proposed).
RENDER_BURNIN_OCR_MIN_MATCH: float = 0.6

# --- #8 audio QC thresholds ---
QC_AUDIO_PEAK_DBTP: float = -1.0
QC_AUDIO_LUFS_TARGET: float = -16.0
QC_AUDIO_LUFS_TOLERANCE: float = 3.0

# --- #9 lip-sync tolerance ---
LIPSYNC_MAX_SPEED_ADJUST: float = 0.10
LIPSYNC_WARN_DIFF_SEC: float = 0.5

# --- #12 content QC (P2, advisory — ADR-0010) ---
# Vision endpoint config lives in env (see app/services/vision_client.py):
#   QC_VISION_BASE_URL / QC_VISION_API_KEY / QC_VISION_MODEL.
# Empty base_url/model = no vision model connected -> both checks report
# stub=true (warn). Any OpenAI-compatible chat-completions endpoint works
# (OpenAI, Ollama /v1, vLLM, LM Studio, ...).
# Per-request timeout for vision calls (env QC_VISION_TIMEOUT_SEC overrides).
QC_VISION_TIMEOUT_SEC: float = 60.0
# Frames sent to the vision model are downscaled to this max dimension
# (keeps base64 payloads small; the model judges composition, not pixels).
QC_VISION_FRAME_MAX_DIM: int = 768
# Minimum content score (0..1) to pass when a vision model is connected.
# Below this -> warn (advisory: these checks can never emit fail).
QC_STORY_FIDELITY_WARN_SCORE: float = 0.70
QC_PERFORMANCE_WARN_SCORE: float = 0.70

# --- #10 reference image cap per shot (model capability wins when stricter) ---
SHOT_MAX_REFERENCES: int = 4

# --- #11 prompt length cap ---
PROMPT_MAX_CHARS: int = 2000

# Global negative prompt, combined with per-character negatives (§7.1 #9).
NEGATIVE_PROMPT_DEFAULT: str = (
    "low quality, blurry, distorted, watermark, text artifacts, extra limbs"
)

# --- #12 OCR ---
OCR_ENGINE: str = "paddleocr"
OCR_CONFIDENCE_HIGH: float = 0.85
OCR_CONFIDENCE_LOW: float = 0.5
OCR_VIDEO_SAMPLE_INTERVAL_SEC: float = 1.0

# --- #13 storage formats ---
FORMAT_IMAGE_GEN: str = "png"
FORMAT_THUMBNAIL: str = "jpg"
THUMBNAIL_JPEG_QUALITY: int = 85
FORMAT_TTS: str = "wav"
FORMAT_BGM: str = "mp3"

# --- #14 backup (ADR-0001: incremental instead of daily full snapshots) ---
BACKUP_FILE_SNAPSHOT_GENERATIONS: int = 30  # rsync --link-dest hardlink snapshots
BACKUP_DB_DUMP_RETENTION_DAYS: int = 14

# --- #15 idempotency (plan D2: DB table idempotency_records) ---
# A key is remembered per (method, actual request path) for this long; a
# replay returns the stored response (no budget re-check, no new job).
IDEMPOTENCY_TTL_SEC: int = 86400
IDEMPOTENCY_HEADER: str = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX_LEN: int = 255

# --- provider polling (design report §3.4 [가정]) ---
POLL_INTERVAL_START_SEC: float = 2.0
POLL_INTERVAL_MAX_SEC: float = 10.0
POLL_MAX_ITERATIONS: int = 600

# --- provider HTTP bounds (ADR-0014, plan B7 policy table) ---
# One monotonic deadline per provider job, from submit to the end of the
# result download. Every request timeout is capped by the time left; at the
# deadline the provider job is canceled best-effort and the job fails
# (after submit the outcome is uncertain -> PROVIDER_UNCERTAIN, plan D7).
# 6000s ~= the old polling sleep budget (600 polls, 2s -> 10s backoff).
HTTP_JOB_TOTAL_DEADLINE_SEC: float = 6000.0
# Result download caps per media kind, enforced while streaming (a lying or
# missing Content-Length cannot bypass them). [proposed]
HTTP_RESULT_MAX_BYTES_IMAGE: int = 64 * 1024 * 1024
HTTP_RESULT_MAX_BYTES_VIDEO: int = 2 * 1024 * 1024 * 1024
HTTP_RESULT_MAX_BYTES_AUDIO: int = 256 * 1024 * 1024
# Cap of a JSON provider response (upload/submit/status/result), enforced
# while streaming. A response that carries inline output bytes also gets the
# base64 size of the media cap above (B7-r2 M2). [proposed]
HTTP_JSON_MAX_BYTES: int = 8 * 1024 * 1024
# Default per-request timeouts (a seed's capabilities.http may override them;
# all are still capped by the remaining job deadline).
HTTP_SUBMIT_TIMEOUT_SEC: float = 30.0
HTTP_POLL_TIMEOUT_SEC: float = 15.0
HTTP_DOWNLOAD_TIMEOUT_SEC: float = 120.0
HTTP_UPLOAD_TIMEOUT_SEC: float = 60.0
HTTP_CANCEL_TIMEOUT_SEC: float = 10.0
# Provider response text quoted in error messages/logs (after secret redaction).
HTTP_ERROR_SNIPPET_CHARS: int = 300
# Still-image generation without an explicit seed draws one from [0, 2**31)
# at enqueue and persists it (reproducible runs, ADR-0014).
IMAGE_SEED_MAX_EXCLUSIVE: int = 2**31
# Conditioning weight of a resolved reference image in the provider input
# contract (one weight for every role until per-role strength is tuned).
REFERENCE_DEFAULT_WEIGHT: float = 1.0

# --- job execution lease / reconcile (ADR-0013, plan D4/D7 [proposed]) ---
# A claimed execution owns its row until lease_expires_at; the worker's
# heartbeat extends it. Reconcile (API lifespan loop + worker_ready) recovers
# expired leases and re-dispatches queued rows the broker may have lost.
LEASE_TTL_SEC: int = 300
HEARTBEAT_INTERVAL_SEC: int = 30
RECONCILE_INTERVAL_SEC: int = 60
RECONCILE_REDISPATCH_AFTER_SEC: int = 120  # queued + no claim -> re-send (min gap)
# P1 orchestration ledgers (lora_train / style_extract) still queued after
# this long are failed by reconcile instead of re-sent forever.
RECONCILE_P1_STUCK_AFTER_SEC: int = 1800
# Layer-2 override: RECONCILE_ENABLED env. Unset -> on, except with the
# in-memory SSE backend (tests / tools/demo.py run without a broker).
RECONCILE_ENABLED: bool = True

# --- render execution / postflight (ADR-0013, plan D8 [proposed]) ---
# Burn-in OCR is a publish gate only when the rendered glyph is at least this
# tall (px at the output height, same scaling as the ASS writer). Measured in
# the API image (PaddleOCR): 16/20/24/32px Hangul+Latin read back at 1.0,
# 9-10px read 0.0 and 14px was unstable. Smaller glyphs are recorded as
# "unverified" in render_meta; the pixel-presence check still applies.
RENDER_BURNIN_OCR_MIN_GLYPH_PX: int = 16
# Pixel-presence check (every burn-in, any glyph size): at each cue midpoint
# the subtitle band of the output must differ from the subtitle-free
# reference by more than RENDER_BURNIN_PIXEL_DELTA (0-255 luma) in at least
# RENDER_BURNIN_MIN_CHANGED_PX pixels. Measured: 8-9px visible cues change
# 0.6-1.4% of the band (hundreds of px); alpha-FF (transparent) cues change 0.
RENDER_BURNIN_PIXEL_DELTA: int = 64
RENDER_BURNIN_MIN_CHANGED_PX: int = 16
# Track-specific presence (B4b-fix, B4B-H2): the pixels counted above are only
# those inside the cue's OWN glyph footprint (the cue drawn alone in its
# track's style, fill + outline, on a blank canvas; luma > MASK_LUMA), minus
# every other track's footprint at that time. At least
# max(MIN_CHANGED_PX, MASK_MIN_FRACTION x footprint px) must change.
RENDER_BURNIN_MASK_LUMA: int = 64
RENDER_BURNIN_MASK_MIN_FRACTION: float = 0.05

# --- uploads (design report §8.3 [가정]) ---
UPLOAD_MAX_BYTES: int = 20 * 1024 * 1024
UPLOAD_ALLOWED_EXTS: tuple[str, ...] = ("png", "jpg", "jpeg", "webp", "mp4", "mov", "wav", "mp3")

# --- CORS: browser origins allowed to call the API (the web UI).
# 1-person local-first: the local web app. Override via CORS_ORIGINS env.
CORS_ALLOWED_ORIGINS: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")

# --- font candidates (thumbnail text, OCR fixtures, CJK burn-in check) ---
# First existing path wins. Linux paths match the Docker image (fonts-noto-cjk);
# macOS paths cover local development. Entries may be .ttc collections.
CJK_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)
LATIN_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
