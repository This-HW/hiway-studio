// Phase-6A API helpers: world bible CRUD + timeline XML/EDL export.
// api-client.ts is frozen (owned by the parent); this module only adds
// typed wrappers for the new endpoints.
import {
  API_BASE,
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
} from "@/lib/api-client";

export type BibleCategory =
  | "character"
  | "faction"
  | "location"
  | "rule"
  | "event"
  | "item";

export const BIBLE_CATEGORIES: BibleCategory[] = [
  "character",
  "faction",
  "location",
  "rule",
  "event",
  "item",
];

export interface BibleEntry {
  id: string;
  project_id: string;
  category: BibleCategory;
  title: string;
  body: string;
  tags: string[];
  created_at: string;
  updated_at: string;
}

export function listBibleEntries(
  projectId: string,
  opts: { category?: string; tag?: string; q?: string } = {},
): Promise<BibleEntry[]> {
  const params = new URLSearchParams();
  if (opts.category) params.set("category", opts.category);
  if (opts.tag) params.set("tag", opts.tag);
  if (opts.q) params.set("q", opts.q);
  const qs = params.toString();
  return apiGet<BibleEntry[]>(
    `/api/v1/projects/${projectId}/bible${qs ? `?${qs}` : ""}`,
  );
}

export function createBibleEntry(
  projectId: string,
  body: { category: BibleCategory; title: string; body?: string; tags?: string[] },
): Promise<BibleEntry> {
  return apiPost<BibleEntry>(`/api/v1/projects/${projectId}/bible`, body);
}

export function updateBibleEntry(
  entryId: string,
  body: Partial<{ category: BibleCategory; title: string; body: string; tags: string[] }>,
): Promise<BibleEntry> {
  return apiPatch<BibleEntry>(`/api/v1/bible/${entryId}`, body);
}

export function deleteBibleEntry(entryId: string): Promise<void> {
  return apiDelete(`/api/v1/bible/${entryId}`);
}

/** Download URL for a timeline's NLE export (XML or EDL).
 * Absolute API URL: the caller renders it as an <a href>, which resolves
 * relative URLs against the web origin, not the API. */
export function timelineExportUrl(
  timelineId: string,
  format: "xml" | "edl",
): string {
  return `${API_BASE}/api/v1/timelines/${timelineId}/export?format=${format}`;
}
