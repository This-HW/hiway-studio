// Workspace shell store: current project, tracked jobs, SSE state.
// The workspace layout owns the single useSse subscription and funnels all
// SSE events through applySseEvent; child screens read job state from here.
"use client";

import { create } from "zustand";
import type { SseConnectionState } from "@/lib/sse";
import { SSE_EVENT } from "@/lib/sse-events";
import type {
  JobCompletedPayload,
  JobFailedPayload,
  JobInfo,
  JobProgressPayload,
  Project,
} from "@/lib/types";

export interface RenderCompletedPayload {
  timeline_id: string;
  output_path: string;
  status: string;
}

interface WorkspaceState {
  projectId: string | null;
  project: Project | null;
  jobs: Record<string, JobInfo>;
  /** Latest render.completed per timeline id (Phase 3). */
  renders: Record<string, RenderCompletedPayload>;
  /** Incremented on every SSE event so screens can react to completions. */
  eventSeq: number;
  sseState: SseConnectionState;
  setProjectId: (id: string | null) => void;
  setProject: (p: Project | null) => void;
  setSseState: (s: SseConnectionState) => void;
  applySseEvent: (name: string, data: unknown) => void;
  activeJobs: () => JobInfo[];
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  projectId: null,
  project: null,
  jobs: {},
  renders: {},
  eventSeq: 0,
  sseState: "disconnected",

  setProjectId: (id) => set({ projectId: id }),
  setProject: (p) => set({ project: p }),
  setSseState: (s) => set({ sseState: s }),

  applySseEvent: (name, data) => {
    if (!isRecord(data)) return;
    if (name === SSE_EVENT.JOB_PROGRESS) {
      const d = data as unknown as JobProgressPayload;
      if (!d.job_id) return;
      set((s) => ({
        jobs: {
          ...s.jobs,
          [d.job_id]: {
            job_id: d.job_id,
            job_type: d.job_type,
            progress_pct: d.progress_pct,
            stage: d.stage,
            status:
              d.status === "running" || d.status === "queued"
                ? d.status
                : "running",
            eta_sec: d.eta_sec,
          },
        },
        eventSeq: s.eventSeq + 1,
      }));
    } else if (name === SSE_EVENT.RENDER_COMPLETED) {
      const d = data as unknown as RenderCompletedPayload;
      if (!d.timeline_id) return;
      set((s) => ({
        renders: { ...s.renders, [d.timeline_id]: d },
        eventSeq: s.eventSeq + 1,
      }));
    } else if (name === SSE_EVENT.JOB_COMPLETED) {
      const d = data as unknown as JobCompletedPayload;
      if (!d.job_id) return;
      set((s) => ({
        jobs: {
          ...s.jobs,
          [d.job_id]: {
            job_id: d.job_id,
            job_type: d.job_type,
            progress_pct: 100,
            stage: "완료",
            status: "completed",
          },
        },
        eventSeq: s.eventSeq + 1,
      }));
    } else if (name === SSE_EVENT.JOB_FAILED) {
      const d = data as unknown as JobFailedPayload;
      if (!d.job_id) return;
      set((s) => ({
        jobs: {
          ...s.jobs,
          [d.job_id]: {
            job_id: d.job_id,
            job_type: d.job_type,
            progress_pct: 0,
            stage: "실패",
            status: "failed",
            error_code: d.error_code,
            error_message: d.error_message,
          },
        },
        eventSeq: s.eventSeq + 1,
      }));
    } else if (name === SSE_EVENT.JOB_CANCELED) {
      const d = data as unknown as { job_id?: string; job_type?: string };
      if (!d.job_id) return;
      set((s) => ({
        jobs: {
          ...s.jobs,
          [d.job_id as string]: {
            job_id: d.job_id as string,
            job_type: (d.job_type as string) ?? "",
            progress_pct: 0,
            stage: "취소됨",
            status: "canceled",
          },
        },
        eventSeq: s.eventSeq + 1,
      }));
    } else if (name === SSE_EVENT.QC_COMPLETED || name === SSE_EVENT.COST_ALERT) {
      // Screens poll the latest QC report / cost summary when eventSeq moves.
      set((s) => ({ eventSeq: s.eventSeq + 1 }));
    }
  },

  activeJobs: () =>
    Object.values(get().jobs).filter(
      (j) => j.status === "queued" || j.status === "running"
    ),
}));
