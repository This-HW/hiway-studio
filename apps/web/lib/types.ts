// Backend DTO shapes for Phase 1 screens.
//
// Source of truth is the API spec (§8.1) mirrored in packages/contracts.
// This file is a hand-written stand-in until `make gen-types` lands;
// keep it in sync with the contracts package when the backend evolves.

export type AspectRatio = "16:9" | "9:16" | "1:1" | "2.39:1" | "4:5";

export type CharacterScope = "project" | "global" | "all";

export interface CategoryPack {
  code: string;
  name: string;
  description?: string;
  defaults?: Record<string, unknown>;
}

export interface ModelInfo {
  model_id: string;
  name?: string;
  modality: "text" | "image" | "video" | "tts" | "audio_music" | "audio_sfx";
  provider?: string;
  status?: string;
}

export interface DefaultModels {
  text_model_id?: string;
  image_model_id?: string;
  video_model_id?: string;
  tts_model_id?: string;
}

export interface Project {
  id: string;
  title: string;
  aspect_ratio: AspectRatio;
  category_pack_code?: string | null;
  category_pack_name?: string | null;
  default_models?: DefaultModels | null;
  monthly_budget_usd?: number | null;
  is_archived?: boolean;
  shot_count?: number;
  approved_shot_count?: number;
  clip_count?: number;
  monthly_cost_estimated_usd?: number;
  monthly_cost_actual_usd?: number;
  thumbnail_url?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface Story {
  id: string;
  project_id: string;
  logline: string | null;
  synopsis: string | null;
  updated_at?: string;
}

export interface Beat {
  id: string;
  order_index: number;
  title?: string | null;
  summary: string | null;
  scene_ids?: string[];
}

export interface GenerateSynopsisResponse {
  llm_call_id: string;
  synopsis: string;
}

export interface GenerateBeatsResponse {
  llm_call_id: string;
  beats: Beat[];
}

export interface ImportScriptResponse {
  scenes_created: number;
}

export interface Character {
  id: string;
  project_id?: string | null;
  scope?: string;
  name: string;
  role?: string | null;
  description?: string | null;
  appearance_prompt?: string | null;
  negative_prompt?: string | null;
  consistency_method?: string | null;
  version: number;
  /** Not in backend CharacterRead yet; used when present, else placeholder. */
  master_thumbnail_url?: string | null;
  referencing_shots?: number;
  created_at?: string;
  updated_at?: string;
}

/** Matches backend ReferenceRead. Display URL comes from resolveFileUrl(). */
export interface Reference {
  id: string;
  character_id: string;
  image_path: string;
  role: string;
  is_primary: boolean;
  source?: string | null;
  created_at?: string;
}

/** Matches backend WardrobeRead. */
export interface WardrobeItem {
  id: string;
  character_id: string;
  name: string;
  prompt_fragment: string;
  reference_image_id?: string | null;
  is_default?: boolean;
  created_at?: string;
}

/** Matches backend AssetRead. Display URL comes from resolveFileUrl(). */
export interface Asset {
  id: string;
  project_id?: string | null;
  name: string;
  kind: string;
  image_path: string;
  prompt_fragment?: string | null;
  tags?: string[];
  created_at?: string;
  updated_at?: string;
  /** Not in backend AssetRead yet; shown when present. */
  referencing_shots?: number;
}

export interface AssetUploadResponse {
  asset_id: string;
  path: string;
}

export type AssetKind = "background" | "location" | "prop";

/** Backend ConsistencyMethod literal (schemas/character.py). */
export const CONSISTENCY_METHODS = [
  "hybrid",
  "reference_image",
  "lora",
  "embedding",
  "faceid",
  "pulid",
  "seed_locked",
] as const;

export interface JobStarted {
  job_id: string;
  estimated_cost_usd?: number;
}

export interface SheetStarted {
  job_ids: string[];
  estimated_cost_usd?: string;  // wire: Decimal serializes as JSON string
}

// --- SSE payloads (see packages/contracts/sse_events.json) ---

export interface JobProgressPayload {
  job_id: string;
  job_type: string;
  progress_pct: number;
  stage: string;
  status: string;
  eta_sec?: number;
}

export interface JobCompletedPayload {
  job_id: string;
  job_type: string;
  status: string;
  output_count: number;
  output_path?: string;
}

export interface JobFailedPayload {
  job_id: string;
  job_type: string;
  error_code: string;
  error_message: string;
  retry_count: number;
  max_retries: number;
  retryable: boolean;
}

export interface JobInfo {
  job_id: string;
  job_type: string;
  progress_pct: number;
  stage: string;
  status: "queued" | "running" | "completed" | "failed" | "canceled";
  eta_sec?: number;
  error_code?: string;
  error_message?: string;
}

// --- Phase 2: scenes / shots (P0-9) ---

export interface Scene {
  id: string;
  project_id: string;
  order_index: number;
  title: string | null;
  location: string | null;
  time_of_day: string | null;
  mood: string | null;
  lighting: string | null;
  style_kit_id: string | null;
  shots: Shot[];
  created_at: string;
  updated_at: string;
}

export interface Shot {
  id: string;
  scene_id: string;
  order_index: number;
  shot_type: string | null;
  camera_angle: string | null;
  movement: string | null;
  duration_sec: number;
  action: string | null;
  dialogue: string | null;
  emotion_weights: Record<string, number> | null;
  sfx_notes: string | null;
  intended_text: string | null;
  status: string;
  frame_stale: boolean;
  approved_frame_generation_id: string | null;
  approved_at: string | null;
  style_kit_id: string | null;
  /** Board card thumbnail (approved frame first, else latest take). */
  thumbnail: string | null;
  candidate_count: number;
  created_at: string;
  updated_at: string;
}

export interface ShotReference {
  id: string;
  shot_id: string;
  character_id: string | null;
  asset_id: string | null;
  purpose: string;
  reference_detail_id: string | null;
  target_name: string | null;
  target_image: string | null;
  created_at: string;
}

export interface GenerationJob {
  id: string;
  job_type: string;
  status: string;
  model_id: string | null;
  shot_id: string | null;
  is_batch: boolean;
  parent_generation_id: string | null;
  batch_index: number | null;
  progress_pct: number | null;
  error_code: string | null;
  error_message: string | null;
  output_path: string | null;
  output_thumbnail_path: string | null;
  prompt_final: string | null;
  reference_ids: string[] | null;
  estimated_cost_usd: string | null;  // wire: Decimal serializes as JSON string
  actual_cost_usd: string | null;  // wire: Decimal serializes as JSON string
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface BatchDetail {
  batch: GenerationJob;
  children: GenerationJob[];
}

export interface ContinuityInfo {
  available: boolean;
  step: string;
  reason: string;
  summary: string;
  source_shot_id: string | null;
  source_generation_id: string | null;
  source_path: string | null;
}

export interface JobAccepted {
  job_id: string;
  estimated_cost_usd: string;  // wire: Decimal serializes as JSON string
}

export const SHOT_REFERENCE_PURPOSES = [
  "primary",
  "expression",
  "outfit",
  "location",
  "continuity",
] as const;

/** Phase 3: video clip (I2V/T2V) attached to a shot. */
export interface VideoClip {
  id: string;
  shot_id: string;
  generation_id: string | null;
  mode: "i2v" | "t2v";
  motion_prompt: string | null;
  duration_sec: number;
  fps: number;
  resolution: string;
  model_id: string;
  output_path: string;
  status: string;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
}

export interface VideoAccepted {
  job_id: string;
  video_id: string;
  estimated_cost_usd: string;  // wire: Decimal serializes as JSON string
}

/** One entry of a timeline's cut list (F-12 clip_order wire shape). */
export interface TimelineClipEntry {
  shot_id: string;
  video_id: string;
  in_sec: number;
  out_sec: number;
}

export interface Timeline {
  id: string;
  project_id: string;
  name: string;
  clip_order: TimelineClipEntry[];
  subtitle_mode: string;
  subtitle_track_ids: string[] | null;
  output_preset_id: string | null;
  output_path: string | null;
  status: "draft" | "rendering" | "done" | "failed";
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface RenderAccepted {
  job_id: string;
  estimated_cost_usd: string;  // wire: Decimal serializes as JSON string
}

export interface ExportPreset {
  id: string;
  project_id: string | null;
  name: string;
  platform: string | null;
  aspect_ratio: string;
  resolution: string;
  fps: number;
  codec: string;
  audio_codec: string;
  burn_subtitles: boolean;
  builtin: boolean;
}

// Phase 4: subtitles, audio, QC, costs (F-13/F-14/F-15).

export interface SubtitleTrack {
  id: string;
  project_id: string;
  language_code: string;
  label: string | null;
  is_default: boolean;
  created_at: string;
}

export interface SubtitleCue {
  id: string;
  track_id: string;
  start_sec: number;
  end_sec: number;
  text: string;
  style_overrides: Record<string, unknown> | null;
}

export interface SubtitleStyle {
  id: string;
  project_id: string;
  track_id: string | null;
  font_family: string;
  font_size_px: number;
  color: string;
  background_color: string | null;
  background_opacity: number;
  position: string;
  safe_area_margin_pct: number;
  line_break_rule: string;
}

export interface AudioGen {
  id: string;
  project_id: string;
  kind: string;
  output_path: string | null;
  duration_sec: number | null;
  status: string;
}

export interface AudioClip {
  id: string;
  timeline_id: string;
  audio_id: string;
  kind: string;
  start_sec: number;
  duration_sec: number | null;
  gain_db: number;
}

export interface QCCheck {
  code: string;
  status: "pass" | "warn" | "fail";
  details: string;
  measured: unknown;
  threshold: unknown;
  stub: boolean;
}

export interface QCReport {
  id: string;
  project_id: string;
  target_type: string;
  target_id: string;
  overall: "pending" | "pass" | "warn" | "fail";
  checks: QCCheck[];
  timeline_content_version: number;
  created_at: string;
}

export interface CostEstimateItem {
  kind: string;
  model_id: string;
  quantity: number;
  estimated_cost_usd: number;
}

export interface CostSummary {
  project_id: string;
  period_start: string;
  period_end: string;
  by_kind: Record<string, number>;
  total_actual_usd: number;
  total_estimated_usd: number;
  budget_usd: number | null;
  budget_usage_pct: number | null;
}
