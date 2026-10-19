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

# --- #15 idempotency ---
IDEMPOTENCY_WINDOW_SEC: int = 60
IDEMPOTENCY_HEADER: str = "Idempotency-Key"

# --- provider polling (design report §3.4 [가정]) ---
POLL_INTERVAL_START_SEC: float = 2.0
POLL_INTERVAL_MAX_SEC: float = 10.0
POLL_MAX_ITERATIONS: int = 600

# --- uploads (design report §8.3 [가정]) ---
UPLOAD_MAX_BYTES: int = 20 * 1024 * 1024
UPLOAD_ALLOWED_EXTS: tuple[str, ...] = ("png", "jpg", "jpeg", "webp", "mp4", "mov", "wav", "mp3")

# --- CORS: browser origins allowed to call the API (the web UI).
# 1-person local-first: the local web app. Override via CORS_ORIGINS env.
CORS_ALLOWED_ORIGINS: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
