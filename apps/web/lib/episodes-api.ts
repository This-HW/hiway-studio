// Phase-5C API helpers: episodes + subtitle-driven edit analysis.
// api-client.ts is frozen (other agents own it); this module only re-exports
// the generic helpers and adds typed wrappers for the new endpoints.
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
} from "@/lib/api-client";

export interface Episode {
  id: string;
  project_id: string;
  order_index: number;
  title: string | null;
  is_template: boolean;
  template_source_episode_id: string | null;
  status: "draft" | "in_progress" | "done";
  created_at: string;
  updated_at: string;
}

export interface SubtitleTrack {
  id: string;
  project_id: string;
  language_code: string;
  label: string | null;
  is_default: boolean;
  created_at: string;
}

export interface TimelineSummary {
  id: string;
  project_id: string;
  name: string;
}

export interface SubtitleClipAdjustment {
  cue_id: string;
  shot_id: string;
  cue_start_sec: number;
  cue_end_sec: number;
  cue_text_len: number;
  current_dur_sec: number;
  suggested_dur_sec: number;
  fits: boolean;
}

export interface SubtitleEditAnalysis {
  track_id: string;
  timeline_id: string;
  adjustments: SubtitleClipAdjustment[];
  warnings: string[];
}

export function listEpisodes(projectId: string): Promise<Episode[]> {
  return apiGet<Episode[]>(`/api/v1/projects/${projectId}/episodes`);
}

export function createEpisode(
  projectId: string,
  body: { title?: string; is_template?: boolean },
): Promise<Episode> {
  return apiPost<Episode>(`/api/v1/projects/${projectId}/episodes`, body);
}

export function updateEpisode(
  episodeId: string,
  body: Partial<Pick<Episode, "title" | "order_index" | "is_template" | "status">>,
): Promise<Episode> {
  return apiPatch<Episode>(`/api/v1/episodes/${episodeId}`, body);
}

export function deleteEpisode(episodeId: string): Promise<void> {
  return apiDelete<void>(`/api/v1/episodes/${episodeId}`);
}

export function cloneEpisode(
  episodeId: string,
): Promise<{ episode_id: string }> {
  return apiPost<{ episode_id: string }>(
    `/api/v1/episodes/${episodeId}/clone`,
    {},
  );
}

export function listSubtitleTracks(projectId: string): Promise<SubtitleTrack[]> {
  return apiGet<SubtitleTrack[]>(
    `/api/v1/projects/${projectId}/subtitle-tracks`,
  );
}

export function listTimelines(projectId: string): Promise<TimelineSummary[]> {
  return apiGet<TimelineSummary[]>(
    `/api/v1/projects/${projectId}/timelines`,
  );
}

export function applySubtitlesToTimeline(
  trackId: string,
  body: { timeline_id: string; cps?: number },
): Promise<SubtitleEditAnalysis> {
  return apiPost<SubtitleEditAnalysis>(
    `/api/v1/subtitle-tracks/${trackId}/apply-to-timeline`,
    body,
  );
}
