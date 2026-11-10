// Workspace shell store: current project, tracked jobs, SSE state.
// The workspace layout owns the single useSse subscription and funnels all
// SSE events through applySseEvent; child screens read job state from here.
"use client";

import { create } from "zustand";
import { apiGet } from "@/lib/api-client";
import type { SseConnectionState } from "@/lib/sse";
import { SSE_EVENT } from "@/lib/sse-events";
import type {
  CostAlertPayload,
  JobCanceledPayload,
  RenderCompletedPayload,
} from "@/lib/sse-events";
import type {
  JobCompletedPayload,
  JobFailedPayload,
  JobInfo,
  JobProgressPayload,
  Project,
} from "@/lib/types";
import { toast } from "@/stores/toast";

export type { RenderCompletedPayload };

interface WorkspaceState {
  projectId: string | null;
  project: Project | null;
  jobs: Record<string, JobInfo>;
  /** Latest render.completed per timeline id (Phase 3). */
  renders: Record<string, RenderCompletedPayload>;
  /** Incremented on every SSE event so screens can react to completions. */
  eventSeq: number;
  /** Incremented after each reconnect resync: screens reload what they show. */
  resyncSeq: number;
  /**
   * Per job id (render: timeline id), bumped by every SSE event and every
   * applied resync result for it. A resync result is applied only when the
   * revision is still the one seen when its read started, so a late HTTP
   * response never overwrites newer state (review B6R2-M2).
   */
  jobRev: Record<string, number>;
  /**
   * Incremented when a reconnect resync STARTS. A caller captures it before
   * an HTTP request that creates jobs and passes it to track(): if a
   * reconnect began meanwhile, the resync could not see the new jobs, so
   * track() reads their durable state once (review B6R3-R2-M1).
   */
  reconnects: number;
  sseState: SseConnectionState;
  setProjectId: (id: string | null) => void;
  setProject: (p: Project | null) => void;
  setSseState: (s: SseConnectionState) => void;
  applySseEvent: (name: string, data: unknown) => void;
  /**
   * Register jobs accepted over HTTP (202) so a reconnect resync can settle
   * them even if no SSE event for them ever arrived. Never overwrites a job
   * that is already tracked (its terminal event may have come first).
   */
  track: (
    entries: { job_id: string; job_type: string }[],
    opts?: { reconnectsAtRequest?: number }
  ) => void;
  /**
   * The SSE stream reconnected: events published while it was down are not
   * replayed. Re-read the durable state of every tracked queued/running job
   * (and render) from the API, then bump eventSeq + resyncSeq so screens
   * settle pending state and reload (review B6-M1).
   */
  resync: () => Promise<void>;
  activeJobs: () => JobInfo[];
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

interface DurableState {
  job: JobInfo;
  render?: RenderCompletedPayload;
}

/** Map a durable row status to the tracked job status; null = still active. */
function settledStatus(status: string): JobInfo["status"] | null {
  if (status === "succeeded" || status === "done") return "completed";
  if (status === "failed") return "failed";
  if (status === "canceled") return "canceled";
  return null;
}

/**
 * Read one tracked job's durable state. Renders are timelines, TTS jobs are
 * audio rows, everything else is a generation (a batch answers with its
 * parent row). Returns null when the job is still active or the read fails
 * (the next event or resync settles it).
 */
async function fetchDurableState(job: JobInfo): Promise<DurableState | null> {
  try {
    if (job.job_type === "render") {
      const t = await apiGet<{ status: string; output_path: string | null }>(
        `/api/v1/timelines/${job.job_id}`
      );
      const status = settledStatus(t.status);
      if (!status) return null;
      return {
        job: { ...job, status, progress_pct: status === "completed" ? 100 : job.progress_pct },
        render: { timeline_id: job.job_id, output_path: t.output_path ?? "", status: t.status },
      };
    }
    const path =
      job.job_type === "tts" ? `/api/v1/audio/${job.job_id}` : `/api/v1/jobs/${job.job_id}`;
    const body = await apiGet<Record<string, unknown>>(path);
    const row = (isRecord(body.batch) ? body.batch : body) as {
      status: string;
      error_code?: string | null;
      error_message?: string | null;
    };
    const status = settledStatus(row.status);
    if (!status) return null;
    return {
      job: {
        ...job,
        status,
        progress_pct: status === "completed" ? 100 : job.progress_pct,
        stage: status === "completed" ? "완료" : status === "failed" ? "실패" : "취소됨",
        error_code: row.error_code ?? undefined,
        error_message: row.error_message ?? undefined,
      },
    };
  } catch {
    return null;
  }
}

type StoreGet = () => WorkspaceState;
type StoreSet = (
  fn: (s: WorkspaceState) => Partial<WorkspaceState>
) => void;

/**
 * Read the durable state of `targets` and merge it. A result is applied only
 * when the job's revision is still the one seen when its read started (no
 * newer SSE event / applied read in between, review B6R2-M2). eventSeq
 * always moves so screens re-check; resyncSeq only for a reconnect resync.
 */
async function refreshFromDurable(
  get: StoreGet,
  set: StoreSet,
  targets: JobInfo[],
  opts: { bumpResync: boolean }
): Promise<void> {
  const revAtStart = get().jobRev;
  const reads = targets.map(async (j) => ({
    rev: revAtStart[j.job_id] ?? 0,
    result: await fetchDurableState(j),
  }));
  const results = await Promise.all(reads);
  set((s) => {
    const jobs = { ...s.jobs };
    const renders = { ...s.renders };
    const jobRev = { ...s.jobRev };
    for (const { rev, result: r } of results) {
      if (!r) continue;
      const id = r.job.job_id;
      // A newer SSE event or applied read already updated this job: keep it.
      if ((jobRev[id] ?? 0) !== rev || !jobs[id]) continue;
      jobs[id] = { ...jobs[id], ...r.job };
      jobRev[id] = rev + 1;
      if (r.render) renders[r.render.timeline_id] = r.render;
    }
    return {
      jobs,
      renders,
      jobRev,
      eventSeq: s.eventSeq + 1,
      ...(opts.bumpResync ? { resyncSeq: s.resyncSeq + 1 } : {}),
    };
  });
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  projectId: null,
  project: null,
  jobs: {},
  renders: {},
  eventSeq: 0,
  resyncSeq: 0,
  jobRev: {},
  reconnects: 0,
  sseState: "disconnected",

  setProjectId: (id) => set({ projectId: id }),
  setProject: (p) => set({ project: p }),
  setSseState: (s) => set({ sseState: s }),

  track: (entries, opts) => {
    const added: JobInfo[] = [];
    set((s) => {
      const jobs = { ...s.jobs };
      // A job whose terminal SSE event arrived before its HTTP 202 is kept as
      // is, and eventSeq moves so screens settle the pending entry their
      // caller just registered (review B6R3-M1).
      let alreadySettled = false;
      for (const e of entries) {
        const known = e.job_id ? jobs[e.job_id] : undefined;
        if (known && known.status !== "queued" && known.status !== "running") {
          alreadySettled = true;
        }
        if (!e.job_id || known) continue;
        jobs[e.job_id] = {
          job_id: e.job_id,
          job_type: e.job_type,
          progress_pct: 0,
          stage: "대기",
          status: "queued",
        };
        added.push(jobs[e.job_id]);
      }
      return alreadySettled ? { jobs, eventSeq: s.eventSeq + 1 } : { jobs };
    });
    const missedReconnect =
      opts?.reconnectsAtRequest !== undefined &&
      opts.reconnectsAtRequest !== get().reconnects;
    if (added.length > 0 && missedReconnect) {
      void refreshFromDurable(get, set, added, { bumpResync: false });
    }
  },

  applySseEvent: (name, data) => {
    if (!isRecord(data)) return;
    const touched =
      typeof data.job_id === "string"
        ? data.job_id
        : name === SSE_EVENT.RENDER_COMPLETED && typeof data.timeline_id === "string"
          ? data.timeline_id
          : null;
    if (touched) {
      set((s) => ({ jobRev: { ...s.jobRev, [touched]: (s.jobRev[touched] ?? 0) + 1 } }));
    }
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
      const d = data as unknown as JobCanceledPayload;
      if (!d.job_id) return;
      set((s) => ({
        jobs: {
          ...s.jobs,
          [d.job_id]: {
            job_id: d.job_id,
            job_type: d.job_type ?? "",
            progress_pct: 0,
            stage: "취소됨",
            status: "canceled",
          },
        },
        eventSeq: s.eventSeq + 1,
      }));
    } else if (name === SSE_EVENT.COST_ALERT) {
      // budget_guard publishes this once when spend crosses threshold_pct.
      const d = data as unknown as CostAlertPayload;
      const current = get().projectId;
      if (!current || d.project_id === current) {
        const pct = Number(d.usage_pct).toFixed(1);
        const spent = Number(d.spent_usd).toFixed(2);
        const budget = Number(d.budget_usd).toFixed(2);
        toast(
          "월 예산의 " + pct + "%를 사용했습니다 ($" + spent + " / $" + budget +
            ", 경고 기준 " + d.threshold_pct + "%).",
          { kind: "error" }
        );
      }
      // The costs screen refetches its summary when eventSeq moves.
      set((s) => ({ eventSeq: s.eventSeq + 1 }));
    } else if (name === SSE_EVENT.QC_COMPLETED) {
      // Screens poll the latest QC report when eventSeq moves.
      set((s) => ({ eventSeq: s.eventSeq + 1 }));
    }
  },

  resync: async () => {
    set((s) => ({ reconnects: s.reconnects + 1 }));
    const active = Object.values(get().jobs).filter(
      (j) => j.status === "queued" || j.status === "running"
    );
    await refreshFromDurable(get, set, active, { bumpResync: true });
  },

  activeJobs: () =>
    Object.values(get().jobs).filter(
      (j) => j.status === "queued" || j.status === "running"
    ),
}));
