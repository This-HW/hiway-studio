// Workspace shell store: current project, tracked jobs, SSE state.
// The workspace layout owns the single useSse subscription and funnels all
// SSE events through applySseEvent; child screens read job state from here.
"use client";

import { create } from "zustand";
import { apiGet } from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import {
  cancelJob as postCancelJob,
  jobActionErrorMessage,
  retryJob as postRetryJob,
} from "@/lib/jobs-api";
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

/**
 * After an accepted retry, SSE events of the OLD attempt can still arrive
 * (the payload carries no attempt id). For this long, a terminal event for the
 * job is applied but marked `verifying` and checked against the server, whose
 * state wins over the event (review B10R3-M1).
 */
export const RETRY_VERIFY_WINDOW_MS = 30_000;

/**
 * A failed server check of a `verifying` job is retried after these delays
 * (3 retries). If all fail, the job stays `verifying` (fail-closed: its
 * pending take does not settle) until a reconnect resync reads it (review
 * B10R4-M1).
 */
export const VERIFY_RETRY_DELAYS_MS = [2_000, 5_000, 10_000] as const;

type VerifyScheduler = (fn: () => void, ms: number) => void;
let scheduleVerify: VerifyScheduler = (fn, ms) => {
  setTimeout(fn, ms);
};
/** Replace the retry timer (tests run the retries without waiting). */
export function setVerifyScheduler(fn: VerifyScheduler): void {
  scheduleVerify = fn;
}

/** job id -> end of its retry verify window (Date.now() ms). */
const retryVerifyUntil = new Map<string, number>();
/** job id -> current verification run; a newer terminal event starts a new one. */
const verifyRun = new Map<string, number>();

function retryWindowOpen(id: string): boolean {
  const until = retryVerifyUntil.get(id);
  if (until === undefined) return false;
  if (Date.now() <= until) return true;
  retryVerifyUntil.delete(id);
  return false;
}

function isTerminal(status: string | undefined): boolean {
  return status === "completed" || status === "failed" || status === "canceled";
}

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
   * Per job id, bumped ONLY by SSE events (jobRev also moves for applied
   * durable reads). retryJob() keeps the state an SSE event wrote during its
   * POST (that event belongs to the new attempt), but a durable read applied
   * meanwhile may describe the OLD attempt and never suppresses the re-queue
   * (review B10-H1).
   */
  sseRev: Record<string, number>;
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
  /**
   * "재시도": POST /jobs/{id}/retry. On 202 the job is tracked as queued
   * again (unless an SSE event for it arrived while the POST was in flight —
   * that event is newer) and then settles through SSE / resync like any new
   * job. `onAccepted` runs before tracking, so a screen can register the job
   * as pending first (same order as track(), review B6R3-M1). Errors toast in
   * Korean; returns whether the server accepted.
   */
  retryJob: (
    entry: { job_id: string; job_type: string },
    opts?: { onAccepted?: () => void }
  ) => Promise<boolean>;
  /**
   * "취소": POST /jobs/{id}/cancel. Never marks the job canceled itself: it
   * settles from the server's job.canceled event or, if that is missed, from
   * a durable read of the job (and any later reconnect resync).
   */
  cancelJob: (
    entry: { job_id: string; job_type: string },
    opts?: { onAccepted?: () => void }
  ) => Promise<boolean>;
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
 * parent row). Returns null when the read fails (the next event or resync
 * settles it) and, unless `includeActive`, when the job is still active.
 * `includeActive` is for a job whose local state may be a stale terminal one
 * (after a retry/cancel the server rejected or accepted, review B10-M2/M3).
 */
async function fetchDurableState(
  job: JobInfo,
  includeActive = false
): Promise<DurableState | null> {
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
    if (!status) {
      if (!includeActive) return null;
      const active: JobInfo["status"] = row.status === "queued" ? "queued" : "running";
      const wasActive = job.status === "queued" || job.status === "running";
      return {
        job: {
          ...job,
          status: active,
          progress_pct: wasActive ? job.progress_pct : 0,
          stage: wasActive ? job.stage : active === "queued" ? "대기" : "진행 중",
          error_code: undefined,
          error_message: undefined,
        },
      };
    }
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

/** 400 VALIDATION_ERROR / 409: the job is not in the state the action needs. */
function isStateRejection(e: unknown): boolean {
  return e instanceof ApiError && (e.code === "VALIDATION_ERROR" || e.status === 409);
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
  opts: { bumpResync: boolean; includeActive?: boolean }
): Promise<(DurableState | null)[]> {
  const revAtStart = get().jobRev;
  const reads = targets.map(async (j) => ({
    rev: revAtStart[j.job_id] ?? 0,
    // A `verifying` job's local terminal state is unconfirmed: read active
    // states too, so the server's state replaces it either way.
    result: await fetchDurableState(j, (opts.includeActive ?? false) || j.verifying === true),
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
      // An applied read IS the server's state: nothing left to verify.
      jobs[id] = { ...jobs[id], ...r.job, verifying: false };
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
  return results.map((r) => r.result);
}

/**
 * After a retry/cancel the server answered: track the job if this screen
 * never saw it, then apply its durable state whatever it is (active states
 * included), unless a newer SSE event lands during the read. Returns the
 * status the server reported (undefined when the read failed). A terminal
 * server state closes the job's retry verify window: it is the new
 * attempt's own outcome.
 */
async function confirmFromServer(
  get: StoreGet,
  set: StoreSet,
  entry: { job_id: string; job_type: string }
): Promise<JobInfo["status"] | undefined> {
  get().track([entry]);
  const job = get().jobs[entry.job_id];
  if (!job) return undefined;
  const [result] = await refreshFromDurable(get, set, [job], {
    bumpResync: false,
    includeActive: true,
  });
  const status = result?.job.status;
  if (isTerminal(status)) retryVerifyUntil.delete(entry.job_id);
  return status;
}

/**
 * A terminal SSE event landed inside the job's retry verify window (it was
 * applied with `verifying: true`). Fail-closed (review B10R4-M1): the flag is
 * cleared only by an applied server read (refreshFromDurable) or by a newer
 * SSE event replacing the job. A failed read keeps it and is retried after
 * VERIFY_RETRY_DELAYS_MS; when those run out, one toast says so and the next
 * reconnect resync reads the job again.
 */
function verifyTerminalEvent(get: StoreGet, set: StoreSet, id: string, jobType: string): void {
  const run = (verifyRun.get(id) ?? 0) + 1;
  verifyRun.set(id, run);
  const current = () => verifyRun.get(id) === run && get().jobs[id]?.verifying === true;
  const attempt = async (n: number): Promise<void> => {
    if (!current()) return;
    const status = await confirmFromServer(get, set, { job_id: id, job_type: jobType });
    // Read OK (applied, or superseded by a newer event): done either way.
    if (status !== undefined || !current()) return;
    if (n < VERIFY_RETRY_DELAYS_MS.length) {
      scheduleVerify(() => void attempt(n + 1), VERIFY_RETRY_DELAYS_MS[n]);
      return;
    }
    toast(
      "작업의 서버 상태를 확인하지 못했습니다. 다시 연결되면 다시 확인합니다.",
      { kind: "error" }
    );
  };
  void attempt(0);
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  projectId: null,
  project: null,
  jobs: {},
  renders: {},
  eventSeq: 0,
  resyncSeq: 0,
  jobRev: {},
  sseRev: {},
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
      set((s) => ({
        jobRev: { ...s.jobRev, [touched]: (s.jobRev[touched] ?? 0) + 1 },
        sseRev: { ...s.sseRev, [touched]: (s.sseRev[touched] ?? 0) + 1 },
      }));
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
      const verify = retryWindowOpen(d.job_id) || get().jobs[d.job_id]?.verifying === true;
      set((s) => ({
        jobs: {
          ...s.jobs,
          [d.job_id]: {
            job_id: d.job_id,
            job_type: d.job_type,
            progress_pct: 100,
            stage: "완료",
            status: "completed",
            ...(verify ? { verifying: true } : {}),
          },
        },
        eventSeq: s.eventSeq + 1,
      }));
      if (verify) verifyTerminalEvent(get, set, d.job_id, d.job_type);
    } else if (name === SSE_EVENT.JOB_FAILED) {
      const d = data as unknown as JobFailedPayload;
      if (!d.job_id) return;
      const verify = retryWindowOpen(d.job_id) || get().jobs[d.job_id]?.verifying === true;
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
            ...(verify ? { verifying: true } : {}),
          },
        },
        eventSeq: s.eventSeq + 1,
      }));
      if (verify) verifyTerminalEvent(get, set, d.job_id, d.job_type);
    } else if (name === SSE_EVENT.JOB_CANCELED) {
      const d = data as unknown as JobCanceledPayload;
      if (!d.job_id) return;
      const verify = retryWindowOpen(d.job_id) || get().jobs[d.job_id]?.verifying === true;
      set((s) => ({
        jobs: {
          ...s.jobs,
          [d.job_id]: {
            job_id: d.job_id,
            job_type: d.job_type ?? "",
            progress_pct: 0,
            stage: "취소됨",
            status: "canceled",
            ...(verify ? { verifying: true } : {}),
          },
        },
        eventSeq: s.eventSeq + 1,
      }));
      if (verify) verifyTerminalEvent(get, set, d.job_id, d.job_type ?? "");
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
    // Also jobs whose terminal event is still unconfirmed (B10R4-M1).
    const targets = Object.values(get().jobs).filter(
      (j) => j.status === "queued" || j.status === "running" || j.verifying === true
    );
    await refreshFromDurable(get, set, targets, { bumpResync: true });
  },

  retryJob: async (entry, opts) => {
    const id = entry.job_id;
    const marker = get().reconnects;
    const sseRevAtRequest = get().sseRev[id] ?? 0;
    const jobRevAtRequest = get().jobRev[id] ?? 0;
    try {
      await postRetryJob(id);
    } catch (e) {
      toast(jobActionErrorMessage(e, "retry"), { kind: "error" });
      // Rejected for its state (e.g. another tab retried first): show what
      // the server has, active states included (review B10-M3).
      if (isStateRejection(e)) await confirmFromServer(get, set, entry);
      return false;
    }
    opts?.onAccepted?.();
    // Armed on every accepted retry, also when an active SSE is kept below:
    // a later terminal event may still be the old attempt's (B10R3-M1).
    retryVerifyUntil.set(id, Date.now() + RETRY_VERIFY_WINDOW_MS);
    let readDuringPost = false;
    let verify = false;
    set((s) => {
      const known = s.jobs[id];
      if ((s.sseRev[id] ?? 0) !== sseRevAtRequest && known) {
        // A progress/running event during the POST can only be the new
        // attempt: keep it, only let screens re-check.
        if (known.status === "queued" || known.status === "running") {
          return { eventSeq: s.eventSeq + 1 };
        }
        // A terminal event may be the OLD attempt's late job.failed (SSE
        // payloads carry no attempt id, review B10R2-M1): re-queue and let
        // the server decide below.
        verify = true;
      }
      // A durable read applied during the POST may describe the OLD attempt
      // (review B10-H1): it never suppresses the re-queue, and a fresh read
      // below covers the case where it already saw the new attempt.
      const rev = s.jobRev[id] ?? 0;
      readDuringPost = rev !== jobRevAtRequest;
      return {
        jobs: {
          ...s.jobs,
          [id]: {
            job_id: id,
            job_type: s.jobs[id]?.job_type || entry.job_type,
            progress_pct: 0,
            stage: "대기",
            status: "queued",
          },
        },
        // Invalidates a resync read of the OLD attempt still in flight.
        jobRev: { ...s.jobRev, [id]: rev + 1 },
        eventSeq: s.eventSeq + 1,
      };
    });
    if (verify) {
      // Server state incl. active; an SSE event after the 202 still wins
      // (jobRev guard in refreshFromDurable).
      void confirmFromServer(get, set, entry);
    } else if (readDuringPost || marker !== get().reconnects) {
      // A reconnect began meanwhile (its resync could not see the new
      // attempt) or a read raced the POST: read the job once more.
      void refreshFromDurable(get, set, [get().jobs[id]], { bumpResync: false });
    }
    toast("재시도를 요청했습니다.");
    return true;
  },

  cancelJob: async (entry, opts) => {
    const id = entry.job_id;
    try {
      await postCancelJob(id);
    } catch (e) {
      toast(jobActionErrorMessage(e, "cancel"), { kind: "error" });
      // Usually the job finished meanwhile: show what the server has.
      if (isStateRejection(e)) await confirmFromServer(get, set, entry);
      return false;
    }
    opts?.onAccepted?.();
    // Server-confirmed state only: the job.canceled event usually wins; this
    // read covers a missed event. It runs even when the local state is an
    // OLD terminal one (a failed earlier attempt, review B10-M2) — a newer
    // SSE event that lands during the read still wins (jobRev guard).
    void confirmFromServer(get, set, entry);
    toast("취소를 요청했습니다.");
    return true;
  },

  activeJobs: () =>
    Object.values(get().jobs).filter(
      (j) => j.status === "queued" || j.status === "running"
    ),
}));
