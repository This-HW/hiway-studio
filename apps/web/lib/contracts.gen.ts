// GENERATED FILE — DO NOT EDIT.
// Source: packages/contracts/{errors,sse_events,enums}.json
// Regenerate: make gen-types (packages/contracts/tools/gen_types.py).
// CI/G5 runs `gen_types.py --check`, which fails when this file is stale.

// --- error codes (errors.json; canonical: services/api/app/core/errors.py) ---

export const ERROR_CODES = [
  "VALIDATION_ERROR",
  "NOT_FOUND",
  "APPROVAL_REQUIRED",
  "QC_FAILED",
  "RENDER_LOCKED",
  "STYLE_KIT_LOCKED",
  "SUBTITLE_OVERLAP",
  "PROMPT_TOO_LONG",
  "MODERATION_BLOCKED",
  "BUDGET_EXCEEDED",
  "QUEUE_FULL",
  "RATE_LIMITED",
  "PROVIDER_ERROR",
  "MODEL_UNAVAILABLE",
  "LORA_TOO_FEW_IMAGES",
  "DUPLICATE_KEYFRAME",
  "PROVIDER_UNCERTAIN",
  "IDEMPOTENCY_CONFLICT",
] as const;
export type ErrorCode = (typeof ERROR_CODES)[number];

export const ERROR_SPEC: Record<ErrorCode, { http: number; retryable: boolean }> = {
  VALIDATION_ERROR: { http: 400, retryable: false },
  NOT_FOUND: { http: 404, retryable: false },
  APPROVAL_REQUIRED: { http: 409, retryable: false },
  QC_FAILED: { http: 409, retryable: false },
  RENDER_LOCKED: { http: 409, retryable: true },
  STYLE_KIT_LOCKED: { http: 409, retryable: false },
  SUBTITLE_OVERLAP: { http: 422, retryable: false },
  PROMPT_TOO_LONG: { http: 422, retryable: false },
  MODERATION_BLOCKED: { http: 422, retryable: false },
  BUDGET_EXCEEDED: { http: 403, retryable: false },
  QUEUE_FULL: { http: 429, retryable: true },
  RATE_LIMITED: { http: 429, retryable: true },
  PROVIDER_ERROR: { http: 502, retryable: true },
  MODEL_UNAVAILABLE: { http: 503, retryable: true },
  LORA_TOO_FEW_IMAGES: { http: 422, retryable: false },
  DUPLICATE_KEYFRAME: { http: 422, retryable: false },
  PROVIDER_UNCERTAIN: { http: 502, retryable: false },
  IDEMPOTENCY_CONFLICT: { http: 409, retryable: false },
};

// --- SSE events (sse_events.json) ---

export const SSE_EVENT_NAMES = [
  "cost.alert",
  "job.canceled",
  "job.completed",
  "job.failed",
  "job.progress",
  "qc.completed",
  "render.completed",
] as const;
export type SseEventName = (typeof SSE_EVENT_NAMES)[number];

/** Named constants for the SSE event names. */
export const SSE_EVENT = {
  COST_ALERT: "cost.alert",
  JOB_CANCELED: "job.canceled",
  JOB_COMPLETED: "job.completed",
  JOB_FAILED: "job.failed",
  JOB_PROGRESS: "job.progress",
  QC_COMPLETED: "qc.completed",
  RENDER_COMPLETED: "render.completed",
} as const satisfies Record<string, SseEventName>;

/** Payload of the `cost.alert` SSE event. */
export interface CostAlertPayload {
  budget_usd: number;
  project_id: string;
  spent_usd: number;
  threshold_pct: number;
  usage_pct: number;
}

/** Payload of the `job.canceled` SSE event. */
export interface JobCanceledPayload {
  job_id: string;
  job_type?: string;
  reason?: string;
}

/** Payload of the `job.completed` SSE event. */
export interface JobCompletedPayload {
  job_id: string;
  job_type: string;
  output_count: number;
  output_path?: string;
  status: string;
}

/** Payload of the `job.failed` SSE event. */
export interface JobFailedPayload {
  error_code: string;
  error_message: string;
  job_id: string;
  job_type: string;
  max_retries: number;
  retry_count: number;
  retryable: boolean;
}

/** Payload of the `job.progress` SSE event. */
export interface JobProgressPayload {
  eta_sec?: number;
  job_id: string;
  job_type: string;
  progress_pct: number;
  stage: string;
  status: string;
}

/** Payload of the `qc.completed` SSE event. */
export interface QcCompletedPayload {
  fail_count: number;
  overall: string;
  qc_report_id: string;
  timeline_id: string;
  warn_count: number;
}

/** Payload of the `render.completed` SSE event. */
export interface RenderCompletedPayload {
  output_path: string;
  status: string;
  timeline_id: string;
}

export interface SseEventPayloads {
  "cost.alert": CostAlertPayload;
  "job.canceled": JobCanceledPayload;
  "job.completed": JobCompletedPayload;
  "job.failed": JobFailedPayload;
  "job.progress": JobProgressPayload;
  "qc.completed": QcCompletedPayload;
  "render.completed": RenderCompletedPayload;
}

// --- enums (enums.json) ---

export const ASPECT_RATIO = [
  "16:9",
  "9:16",
  "1:1",
  "2.39:1",
  "4:5",
] as const;
export type AspectRatio = (typeof ASPECT_RATIO)[number];

export const GENERATION_STATUS = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "canceled",
] as const;
export type GenerationStatus = (typeof GENERATION_STATUS)[number];

export const JOB_TYPE = [
  "text_to_image",
  "image_to_image",
  "inpaint",
  "outpaint",
  "upscale",
  "text_to_video",
  "image_to_video",
  "video_extend",
  "tts",
  "lora_train",
  "multishot",
  "thumbnail",
  "style_extract",
] as const;
export type JobType = (typeof JOB_TYPE)[number];

export const MODALITY = [
  "image",
  "video",
  "text",
  "tts",
  "audio_music",
  "audio_sfx",
] as const;
export type Modality = (typeof MODALITY)[number];

export const MODEL_STATUS = [
  "active",
  "disabled",
  "deprecated",
] as const;
export type ModelStatus = (typeof MODEL_STATUS)[number];

export const QC_OVERALL = [
  "pass",
  "warn",
  "fail",
] as const;
export type QcOverall = (typeof QC_OVERALL)[number];

export const CHARACTER_ROLE = [
  "protagonist",
  "antagonist",
  "supporting",
  "extra",
] as const;
export type CharacterRole = (typeof CHARACTER_ROLE)[number];

export const CONSISTENCY_METHOD = [
  "reference_image",
  "lora",
  "embedding",
  "faceid",
  "pulid",
  "seed_locked",
  "hybrid",
] as const;
export type ConsistencyMethod = (typeof CONSISTENCY_METHOD)[number];

export const REFERENCE_ROLE = [
  "master",
  "turnaround_front",
  "turnaround_three_quarter",
  "turnaround_profile",
  "turnaround_back",
  "expression_neutral",
  "expression_happy",
  "expression_sad",
  "expression_angry",
  "expression_surprised",
  "expression_determined",
  "expression_vulnerable",
  "outfit_default",
  "outfit_alt",
  "pose_action",
  "detail_face",
  "detail_hands",
  "detail_prop",
] as const;
export type ReferenceRole = (typeof REFERENCE_ROLE)[number];

export const REFERENCE_SOURCE = [
  "generated",
  "uploaded",
  "sheet_derived",
] as const;
export type ReferenceSource = (typeof REFERENCE_SOURCE)[number];

export const ASSET_KIND = [
  "location",
  "prop",
  "background",
  "reference",
  "audio",
] as const;
export type AssetKind = (typeof ASSET_KIND)[number];

export const EPISODE_STATUS = [
  "draft",
  "in_progress",
  "done",
] as const;
export type EpisodeStatus = (typeof EPISODE_STATUS)[number];

export const LLM_PURPOSE = [
  "synopsis",
  "beats",
  "broll_suggest",
  "translation",
  "dialogue_polish",
  "other",
] as const;
export type LlmPurpose = (typeof LLM_PURPOSE)[number];

export const SHOT_TYPE = [
  "establishing",
  "wide",
  "medium",
  "close_up",
  "extreme_close_up",
  "over_the_shoulder",
  "pov",
  "insert",
  "reaction",
  "aerial",
  "dutch",
] as const;
export type ShotType = (typeof SHOT_TYPE)[number];

export const CAMERA_ANGLE = [
  "eye_level",
  "high",
  "low",
  "birds_eye",
  "worms_eye",
  "dutch",
  "ots",
] as const;
export type CameraAngle = (typeof CAMERA_ANGLE)[number];

export const CAMERA_MOVEMENT = [
  "static",
  "pan",
  "tilt",
  "dolly_in",
  "dolly_out",
  "truck",
  "crane",
  "orbit",
  "orbit_360",
  "zoom_in",
  "zoom_out",
  "crash_zoom_in",
  "crash_zoom_out",
  "dolly_zoom_in",
  "dolly_zoom_out",
  "whip_pan",
  "jib_up",
  "jib_down",
  "snorricam",
  "hyperlapse",
  "handheld",
  "fpv",
] as const;
export type CameraMovement = (typeof CAMERA_MOVEMENT)[number];

export const SHOT_STATUS = [
  "draft",
  "frame_generating",
  "frame_done",
  "frame_approved",
  "video_generating",
  "video_done",
  "failed",
] as const;
export type ShotStatus = (typeof SHOT_STATUS)[number];

export const SHOT_REFERENCE_PURPOSE = [
  "primary",
  "expression",
  "outfit",
  "location",
  "continuity",
] as const;
export type ShotReferencePurpose = (typeof SHOT_REFERENCE_PURPOSE)[number];

export const JOB_STATUS = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "canceled",
] as const;
export type JobStatus = (typeof JOB_STATUS)[number];

export const SCRIPT_FORMAT = [
  "txt",
  "fountain",
  "fdx",
] as const;
export type ScriptFormat = (typeof SCRIPT_FORMAT)[number];

export const VIDEO_MODE = [
  "i2v",
  "t2v",
] as const;
export type VideoMode = (typeof VIDEO_MODE)[number];

export const VIDEO_STATUS = [
  "draft",
  "generating",
  "done",
  "failed",
] as const;
export type VideoStatus = (typeof VIDEO_STATUS)[number];

export const RENDER_STATUS = [
  "draft",
  "rendering",
  "done",
  "failed",
] as const;
export type RenderStatus = (typeof RENDER_STATUS)[number];

export const SUBTITLE_MODE = [
  "none",
  "single",
  "dual",
] as const;
export type SubtitleMode = (typeof SUBTITLE_MODE)[number];

export const SUBTITLE_POSITION = [
  "top",
  "middle",
  "bottom",
] as const;
export type SubtitlePosition = (typeof SUBTITLE_POSITION)[number];

export const AUDIO_KIND = [
  "dialogue",
  "narration",
  "bgm",
  "sfx",
] as const;
export type AudioKind = (typeof AUDIO_KIND)[number];

export const QC_TARGET = [
  "video",
  "timeline",
] as const;
export type QcTarget = (typeof QC_TARGET)[number];

export const QC_RESULT = [
  "pass",
  "warn",
  "fail",
] as const;
export type QcResult = (typeof QC_RESULT)[number];

export const BIBLE_CATEGORY = [
  "character",
  "faction",
  "location",
  "rule",
  "event",
  "item",
] as const;
export type BibleCategory = (typeof BIBLE_CATEGORY)[number];
