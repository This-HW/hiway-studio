// Board screen store (app/projects/[id]/board): scenes, shots, frame generation,
// references and approval. The review screen reuses this store's data.
//
// All generation POSTs send an Idempotency-Key so double clicks / retries do
// not create duplicate jobs (design §12.2).
"use client";

import { create } from "zustand";
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
} from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import { koreanErrorMessage } from "@/lib/format";
import type {
  ContinuityInfo,
  GenerationJob,
  JobAccepted,
  Scene,
  Shot,
  ShotReference,
} from "@/lib/types";
import { toast } from "./toast";
import { useWorkspaceStore } from "./workspace";

/** Register HTTP-accepted takes so a reconnect resync can settle them even
 * when no SSE event for them arrived (review B6R2-M1). Called AFTER the
 * pending registry is written: if a take's terminal event already arrived,
 * track() bumps eventSeq and the screen's settle effect sees the pending
 * entry (review B6R3-M1). */
function trackTakes(ids: string[], reconnectsAtRequest: number): void {
  useWorkspaceStore
    .getState()
    .track(
      ids.map((job_id) => ({ job_id, job_type: "text_to_image" })),
      { reconnectsAtRequest }
    );
}

/** Reconnect marker captured before a job-creating request (B6R3-R2-M1). */
function reconnectMarker(): number {
  return useWorkspaceStore.getState().reconnects;
}

interface BoardState {
  projectId: string | null;
  scenes: Scene[];
  loading: boolean;
  error: string | null;
  selectedShotId: string | null;
  references: Record<string, ShotReference[]>;
  takes: Record<string, GenerationJob[]>;
  continuity: Record<string, ContinuityInfo>;
  /** shotId -> take job ids currently in flight (watched via SSE job events). */
  pendingTakes: Record<string, string[]>;
  busy: Record<string, boolean>;

  init: (projectId: string) => Promise<void>;
  refresh: () => Promise<void>;
  selectShot: (id: string | null) => void;
  addScene: () => Promise<void>;
  updateScene: (id: string, patch: Partial<Scene>) => Promise<void>;
  deleteScene: (id: string) => Promise<void>;
  addShot: (sceneId: string) => Promise<void>;
  updateShot: (id: string, patch: Partial<Shot>) => Promise<void>;
  deleteShot: (id: string) => Promise<void>;
  duplicateShot: (id: string) => Promise<void>;
  moveShot: (sceneId: string, shotId: string, dir: -1 | 1) => Promise<void>;
  loadShotDetail: (shotId: string) => Promise<void>;
  generateFrame: (
    shotId: string,
    opts?: { batchSize?: number; useContinuity?: boolean },
  ) => Promise<void>;
  retake: (shotId: string) => Promise<void>;
  inpaint: (
    shotId: string,
    generationId: string,
    maskPath: string,
    prompt: string,
  ) => Promise<void>;
  upscale: (shotId: string, generationId: string) => Promise<void>;
  outpaint: (
    shotId: string,
    generationId: string,
    prompt: string,
  ) => Promise<void>;
  approve: (shotId: string, generationId: string) => Promise<void>;
  /** "재시도" on a failed take: POST /jobs/{id}/retry, then watch it as pending. */
  retryTake: (shotId: string, take: { id: string; job_type: string }) => Promise<boolean>;
  /** "취소" on a queued/running take: POST /jobs/{id}/cancel; settles via SSE/resync. */
  cancelTake: (shotId: string, take: { id: string; job_type: string }) => Promise<boolean>;
  addReference: (
    shotId: string,
    ref: { character_id?: string; asset_id?: string; purpose: string },
  ) => Promise<void>;
  removeReference: (shotId: string, refId: string) => Promise<void>;
  /** True when every in-flight take for the shot reached a terminal SSE state. */
  takesSettled: (
    shotId: string,
    jobs: Record<string, { status: string; verifying?: boolean }>,
  ) => boolean;
  /** Called from the page when a shot's takes all settled. */
  handleTakesSettled: (shotId: string) => void;
  /**
   * Called from the page on every workspace event: reload a loaded shot
   * whose take cards disagree with a tracked job that changed since the last
   * check — e.g. a retry/cancel from the header job list, which knows no
   * shot (review B10-M1). Returns the shot ids it reloaded.
   */
  syncTakesWithJobs: (
    jobs: Record<string, { status: string }>,
    jobRev: Record<string, number>,
  ) => string[];
}

/** The open shot's take list shows new queued/running takes right away, so
 * their "취소" is reachable before the whole batch settles (review B10-M4). */
function reloadIfOpen(get: () => BoardState, shotId: string): void {
  if (get().selectedShotId === shotId) void get().loadShotDetail(shotId);
}

function errMsg(e: unknown, fallback: string): string {
  if (e instanceof ApiError) return koreanErrorMessage(e.code, e.message || fallback);
  return fallback;
}

async function pendingTakeIds(jobId: string): Promise<string[]> {
  try {
    const detail = await apiGet<{ children?: { id: string }[] }>(
      `/api/v1/jobs/${jobId}`,
    );
    const ids = (detail.children ?? []).map((c) => c.id);
    return ids.length > 0 ? ids : [jobId];
  } catch {
    return [jobId];
  }
}

/** Add one take id to a shot's pending list (keeps takes already in flight). */
function withPending(
  pending: Record<string, string[]>,
  shotId: string,
  jobId: string,
): Record<string, string[]> {
  const ids = pending[shotId] ?? [];
  return ids.includes(jobId) ? pending : { ...pending, [shotId]: [...ids, jobId] };
}

/** Take row status (generation) in the workspace job vocabulary. */
function takeStatus(rowStatus: string): string {
  return rowStatus === "succeeded" ? "completed" : rowStatus;
}

/** Latest loadShotDetail request per shot: only its response is applied, so
 * an older, slower response never overwrites a newer one (review B10R2-M2). */
const shotDetailSeq = new Map<string, number>();

/** jobRev last acted on per take id (syncTakesWithJobs reloads once per change). */
const takeSyncRev = new Map<string, number>();

function replaceShot(scenes: Scene[], shot: Shot): Scene[] {
  return scenes.map((sc) => ({
    ...sc,
    shots: sc.shots.map((s) => (s.id === shot.id ? shot : s)),
  }));
}

export const useBoardStore = create<BoardState>((set, get) => ({
  projectId: null,
  scenes: [],
  loading: false,
  error: null,
  selectedShotId: null,
  references: {},
  takes: {},
  continuity: {},
  pendingTakes: {},
  busy: {},

  init: async (projectId) => {
    if (get().projectId === projectId && get().scenes.length > 0) return;
    set({ projectId, loading: true, error: null });
    try {
      const scenes = await apiGet<Scene[]>(`/api/v1/projects/${projectId}/scenes`);
      set({ scenes, loading: false });
    } catch (e) {
      set({ error: errMsg(e, "씬 목록을 불러오지 못했습니다."), loading: false });
    }
  },

  refresh: async () => {
    const { projectId } = get();
    if (!projectId) return;
    try {
      const scenes = await apiGet<Scene[]>(`/api/v1/projects/${projectId}/scenes`);
      set({ scenes });
    } catch (e) {
      set({ error: errMsg(e, "씬 목록을 불러오지 못했습니다.") });
    }
  },

  selectShot: (id) => {
    set({ selectedShotId: id });
    if (id) void get().loadShotDetail(id);
  },

  addScene: async () => {
    const { projectId } = get();
    if (!projectId) return;
    try {
      const scene = await apiPost<Scene>(`/api/v1/projects/${projectId}/scenes`, {
        title: `씬 ${get().scenes.length + 1}`,
      });
      set({ scenes: [...get().scenes, scene] });
    } catch (e) {
      toast(errMsg(e, "씬 추가에 실패했습니다."), { kind: "error" });
    }
  },

  updateScene: async (id, patch) => {
    try {
      const scene = await apiPatch<Scene>(`/api/v1/scenes/${id}`, patch);
      set({ scenes: get().scenes.map((s) => (s.id === id ? { ...s, ...scene } : s)) });
    } catch (e) {
      toast(errMsg(e, "씬 수정에 실패했습니다."), { kind: "error" });
    }
  },

  deleteScene: async (id) => {
    try {
      await apiDelete(`/api/v1/scenes/${id}`);
      set({ scenes: get().scenes.filter((s) => s.id !== id) });
    } catch (e) {
      toast(errMsg(e, "씬 삭제에 실패했습니다."), { kind: "error" });
    }
  },

  addShot: async (sceneId) => {
    try {
      const shot = await apiPost<Shot>(`/api/v1/scenes/${sceneId}/shots`, {});
      set({
        scenes: get().scenes.map((sc) =>
          sc.id === sceneId ? { ...sc, shots: [...sc.shots, shot] } : sc,
        ),
      });
    } catch (e) {
      toast(errMsg(e, "샷 추가에 실패했습니다."), { kind: "error" });
    }
  },

  updateShot: async (id, patch) => {
    try {
      const shot = await apiPatch<Shot>(`/api/v1/shots/${id}`, patch);
      set({ scenes: replaceShot(get().scenes, shot) });
      if (get().selectedShotId === id) void get().loadShotDetail(id);
    } catch (e) {
      toast(errMsg(e, "샷 수정에 실패했습니다."), { kind: "error" });
    }
  },

  deleteShot: async (id) => {
    try {
      await apiDelete(`/api/v1/shots/${id}`);
      set({
        scenes: get().scenes.map((sc) => ({
          ...sc,
          shots: sc.shots.filter((s) => s.id !== id),
        })),
        selectedShotId: get().selectedShotId === id ? null : get().selectedShotId,
      });
    } catch (e) {
      toast(errMsg(e, "샷 삭제에 실패했습니다."), { kind: "error" });
    }
  },

  duplicateShot: async (id) => {
    try {
      const shot = await apiPost<Shot>(`/api/v1/shots/${id}/duplicate`, {});
      await get().refresh();
      void shot;
    } catch (e) {
      toast(errMsg(e, "샷 복제에 실패했습니다."), { kind: "error" });
    }
  },

  moveShot: async (sceneId, shotId, dir) => {
    const scene = get().scenes.find((s) => s.id === sceneId);
    if (!scene) return;
    const idx = scene.shots.findIndex((s) => s.id === shotId);
    const next = idx + dir;
    if (idx < 0 || next < 0 || next >= scene.shots.length) return;
    const ordered = scene.shots.map((s) => s.id);
    [ordered[idx], ordered[next]] = [ordered[next], ordered[idx]];
    try {
      const shots = await apiPost<Shot[]>(`/api/v1/scenes/${sceneId}/shots/reorder`, {
        ordered_ids: ordered,
      });
      set({
        scenes: get().scenes.map((sc) => (sc.id === sceneId ? { ...sc, shots } : sc)),
      });
    } catch (e) {
      toast(errMsg(e, "샷 순서 변경에 실패했습니다."), { kind: "error" });
    }
  },

  loadShotDetail: async (shotId) => {
    const seq = (shotDetailSeq.get(shotId) ?? 0) + 1;
    shotDetailSeq.set(shotId, seq);
    const latest = () => shotDetailSeq.get(shotId) === seq;
    try {
      const [refs, cont] = await Promise.all([
        apiGet<ShotReference[]>(`/api/v1/shots/${shotId}/references`),
        apiGet<ContinuityInfo>(`/api/v1/shots/${shotId}/continuity`),
      ]);
      const takes = await apiGet<GenerationJob[]>(
        `/api/v1/jobs?shot_id=${shotId}`,
      );
      if (!latest()) return;
      set({
        references: { ...get().references, [shotId]: refs },
        takes: {
          ...get().takes,
          [shotId]: takes.filter((j) => !j.is_batch).reverse(),
        },
        continuity: { ...get().continuity, [shotId]: cont },
      });
    } catch (e) {
      if (!latest()) return;
      toast(errMsg(e, "샷 상세를 불러오지 못했습니다."), { kind: "error" });
    }
  },

  generateFrame: async (shotId, opts = {}) => {
    const key = `gen-${shotId}-${crypto.randomUUID()}`;
    set({ busy: { ...get().busy, [shotId]: true } });
    try {
      const marker = reconnectMarker();
      const res = await apiPost<JobAccepted>(
        `/api/v1/shots/${shotId}/frame`,
        {
          batch_size: opts.batchSize ?? 2,
          use_continuity: opts.useContinuity ?? false,
        },
        { idempotencyKey: key },
      );
      const takeIds = await pendingTakeIds(res.job_id);
      set({
        pendingTakes: { ...get().pendingTakes, [shotId]: takeIds },
        busy: { ...get().busy, [shotId]: false },
      });
      trackTakes(takeIds, marker);
      reloadIfOpen(get, shotId);
      toast(
        `프레임 생성 시작 (예상 $${Number(res.estimated_cost_usd).toFixed(4)})`,
      );
    } catch (e) {
      set({ busy: { ...get().busy, [shotId]: false } });
      toast(errMsg(e, "프레임 생성에 실패했습니다."), { kind: "error" });
    }
  },

  retake: async (shotId) => {
    const key = `retake-${shotId}-${crypto.randomUUID()}`;
    set({ busy: { ...get().busy, [shotId]: true } });
    try {
      const marker = reconnectMarker();
      const res = await apiPost<JobAccepted>(
        `/api/v1/shots/${shotId}/retake`,
        { batch_size: 2 },
        { idempotencyKey: key },
      );
      const takeIds = await pendingTakeIds(res.job_id);
      set({
        pendingTakes: { ...get().pendingTakes, [shotId]: takeIds },
        busy: { ...get().busy, [shotId]: false },
      });
      trackTakes(takeIds, marker);
      reloadIfOpen(get, shotId);
      toast("리테이크 시작");
    } catch (e) {
      set({ busy: { ...get().busy, [shotId]: false } });
      toast(errMsg(e, "리테이크에 실패했습니다."), { kind: "error" });
    }
  },

  inpaint: async (shotId, generationId, maskPath, prompt) => {
    const key = `inpaint-${shotId}-${crypto.randomUUID()}`;
    try {
      const marker = reconnectMarker();
      const res = await apiPost<JobAccepted>(
        `/api/v1/shots/${shotId}/inpaint`,
        { generation_id: generationId, mask_path: maskPath, prompt },
        { idempotencyKey: key },
      );
      set({ pendingTakes: { ...get().pendingTakes, [shotId]: [res.job_id] } });
      trackTakes([res.job_id], marker);
      reloadIfOpen(get, shotId);
      toast("인페인트 시작");
    } catch (e) {
      toast(errMsg(e, "인페인트에 실패했습니다."), { kind: "error" });
    }
  },

  upscale: async (shotId, generationId) => {
    const key = `upscale-${shotId}-${crypto.randomUUID()}`;
    try {
      const marker = reconnectMarker();
      const res = await apiPost<JobAccepted>(
        `/api/v1/shots/${shotId}/upscale`,
        { generation_id: generationId },
        { idempotencyKey: key },
      );
      set({ pendingTakes: { ...get().pendingTakes, [shotId]: [res.job_id] } });
      trackTakes([res.job_id], marker);
      reloadIfOpen(get, shotId);
      toast("업스케일 시작");
    } catch (e) {
      toast(errMsg(e, "업스케일에 실패했습니다."), { kind: "error" });
    }
  },

  outpaint: async (shotId, generationId, prompt) => {
    const key = `outpaint-${shotId}-${crypto.randomUUID()}`;
    try {
      const marker = reconnectMarker();
      const res = await apiPost<JobAccepted>(
        `/api/v1/shots/${shotId}/outpaint`,
        { generation_id: generationId, prompt },
        { idempotencyKey: key },
      );
      set({ pendingTakes: { ...get().pendingTakes, [shotId]: [res.job_id] } });
      trackTakes([res.job_id], marker);
      reloadIfOpen(get, shotId);
      toast("아웃페인트 시작");
    } catch (e) {
      toast(errMsg(e, "아웃페인트에 실패했습니다."), { kind: "error" });
    }
  },

  approve: async (shotId, generationId) => {
    try {
      const shot = await apiPost<Shot>(`/api/v1/shots/${shotId}/frame/approve`, {
        generation_id: generationId,
      });
      set({ scenes: replaceShot(get().scenes, shot) });
      void get().loadShotDetail(shotId);
      toast("프레임을 승인했습니다.");
    } catch (e) {
      toast(errMsg(e, "승인에 실패했습니다."), { kind: "error" });
    }
  },

  retryTake: async (shotId, take) => {
    const ok = await useWorkspaceStore.getState().retryJob(
      { job_id: take.id, job_type: take.job_type },
      { onAccepted: () => set({ pendingTakes: withPending(get().pendingTakes, shotId, take.id) }) },
    );
    // Accepted: the card shows the server's queued row. Rejected: the
    // server's current state (the take may have changed meanwhile).
    void get().loadShotDetail(shotId);
    return ok;
  },

  cancelTake: async (shotId, take) => {
    // The card keeps the take's server state until job.canceled (or a durable
    // read) settles it; handleTakesSettled then reloads the shot.
    const ok = await useWorkspaceStore.getState().cancelJob(
      { job_id: take.id, job_type: take.job_type },
      { onAccepted: () => set({ pendingTakes: withPending(get().pendingTakes, shotId, take.id) }) },
    );
    if (!ok) void get().loadShotDetail(shotId);
    return ok;
  },

  addReference: async (shotId, ref) => {
    try {
      await apiPost(`/api/v1/shots/${shotId}/references`, ref);
      await get().loadShotDetail(shotId);
    } catch (e) {
      toast(errMsg(e, "레퍼런스 추가에 실패했습니다."), { kind: "error" });
    }
  },

  removeReference: async (shotId, refId) => {
    try {
      // ADR-0012: shot references are deleted under their shot.
      await apiDelete(`/api/v1/shots/${shotId}/references/${refId}`);
      await get().loadShotDetail(shotId);
    } catch (e) {
      toast(errMsg(e, "레퍼런스 삭제에 실패했습니다."), { kind: "error" });
    }
  },

  takesSettled: (shotId, jobs) => {
    const ids = get().pendingTakes[shotId];
    if (!ids || ids.length === 0) return false;
    return ids.every((id) => {
      const st = jobs[id]?.status;
      // An unconfirmed terminal event after a retry does not settle the take.
      if (jobs[id]?.verifying) return false;
      return st === "completed" || st === "failed" || st === "canceled";
    });
  },

  handleTakesSettled: (shotId) => {
    const next = { ...get().pendingTakes };
    delete next[shotId];
    set({ pendingTakes: next });
    // Refresh the board so the new thumbnail / status appears.
    void get().refresh();
    if (get().selectedShotId === shotId) void get().loadShotDetail(shotId);
  },

  syncTakesWithJobs: (jobs, jobRev) => {
    const stale: string[] = [];
    let terminal = false;
    for (const [shotId, rows] of Object.entries(get().takes)) {
      let shotStale = false;
      for (const t of rows) {
        const job = jobs[t.id];
        if (!job || job.status === takeStatus(t.status)) continue;
        const rev = jobRev[t.id] ?? 0;
        if (takeSyncRev.get(t.id) === rev) continue;
        takeSyncRev.set(t.id, rev);
        shotStale = true;
        if (job.status === "completed" || job.status === "failed" || job.status === "canceled") {
          terminal = true;
        }
      }
      if (shotStale) stale.push(shotId);
    }
    for (const shotId of stale) void get().loadShotDetail(shotId);
    // Shot status / thumbnail follow a finished take.
    if (terminal) void get().refresh();
    return stale;
  },
}));
