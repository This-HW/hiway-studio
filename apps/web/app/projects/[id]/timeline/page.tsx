// Timeline: clip strip -> cut list -> audio -> subtitles -> QC -> render
// (Phases 3-4, F-10/F-12/F-13/F-14/F-15). Subtitles live here as a tab
// (ADR-0002); QC runs from the render card and the project QC screen.
"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet, apiPatch, apiPost } from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import { resolveFileUrl } from "@/lib/files";
import type {
  ExportPreset,
  QCReport,
  RenderAccepted,
  Shot,
  Timeline,
  TimelineClipEntry,
  VideoAccepted,
  VideoClip,
} from "@/lib/types";
import { AudioPanel } from "@/components/AudioPanel";
import { QcReportView } from "@/components/QcReportView";
import { SubtitleTab } from "@/components/SubtitleTab";
import TimelineExportButtons from "@/components/TimelineExportButtons";
import { useBoardStore } from "@/stores/board";
import { useToastStore } from "@/stores/toast";
import { useWorkspaceStore } from "@/stores/workspace";

type Tab = "video" | "subtitle";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "요청에 실패했습니다.";
}

function fmtSec(sec: number): string {
  return `${sec.toFixed(1)}s`;
}

// P1-9 썸네일 생성 (§05C-6): 영상용 썸네일 후보 묶음 생성.
// 제목 텍스트는 후처리(PIL) 오버레이로 합성되며 이미지 모델이 그리지 않는다.
function ThumbnailSection({ projectId, timelines }: { projectId: string; timelines: Timeline[] }) {
  const [titleText, setTitleText] = useState("");
  const [count, setCount] = useState(4);
  const [timelineId, setTimelineId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cost, setCost] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await apiPost<{ job_id: string; estimated_cost_usd: string; count: number }>(
        `/api/v1/projects/${projectId}/thumbnails`,
        {
          timeline_id: timelineId || undefined,
          title_text: titleText || undefined,
          count,
        },
        { idempotencyKey: `thumbnails-${projectId}-${Date.now()}` },
      );
      setJobId(res.job_id);
      setCost(res.estimated_cost_usd);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "요청에 실패했습니다.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="card-title">썸네일 생성 (P1-9)</div>
      <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 8 }}>
        영상용 썸네일 후보를 묶음으로 생성합니다. 제목은 이미지 위에 후처리로 합성됩니다.
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <label style={{ fontSize: 12 }}>제목 텍스트 (최대 60자)
          <input className="input" value={titleText} onChange={(e) => setTitleText(e.target.value)} maxLength={60} placeholder="썸네일 제목" style={{ marginLeft: 4 }} />
        </label>
        <label style={{ fontSize: 12 }}>개수
          <select className="input" value={count} onChange={(e) => setCount(Number(e.target.value))} style={{ marginLeft: 4 }}>
            {[2, 3, 4, 5, 6, 7, 8].map((n) => <option key={n} value={n}>{n}개</option>)}
          </select>
        </label>
        <label style={{ fontSize: 12 }}>타임라인 (선택)
          <select className="input" value={timelineId} onChange={(e) => setTimelineId(e.target.value)} style={{ marginLeft: 4 }}>
            <option value="">미지정</option>
            {timelines.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </label>
        <button className="btn btn-primary" disabled={busy} onClick={() => void run()}>
          {busy ? "생성 중…" : "썸네일 생성"}
        </button>
        {cost && <span style={{ fontSize: 12, color: "var(--muted)" }}>예상 ~${cost}</span>}
      </div>
      {error && <p style={{ color: "red", fontSize: 12 }}>{error} <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => void run()}>재시도</button></p>}
      {jobId && !error && (
        <p style={{ fontSize: 12, color: "var(--muted)", marginTop: 6 }}>
          잡 접수됨 — 생성 중… (job {jobId.slice(0, 8)})
        </p>
      )}
    </div>
  );
}

/** Per-shot clip strip row: generate a clip (I2V needs an approved frame). */
function ShotClipRow({
  shot,
  clips,
  motionPrompt,
  setMotionPrompt,
  busy,
  onGenerate,
  onAddClip,
  onRefresh,
}: {
  shot: Shot;
  clips: VideoClip[];
  motionPrompt: string;
  setMotionPrompt: (v: string) => void;
  busy: boolean;
  onGenerate: () => void;
  onAddClip: (clip: VideoClip) => void;
  onRefresh: () => void;
}) {
  const done = clips.filter((c) => c.status === "done");
  const latest = done[0] ?? null;
  const generating = clips.some((c) => c.status === "generating");
  const approved = !!shot.approved_frame_generation_id;
  return (
    <div className="card" style={{ padding: 10 }}>
      <div style={{ display: "flex", gap: 10 }}>
        {shot.thumbnail ? (
          <img src={resolveFileUrl(shot.thumbnail) ?? ""} alt="" className="thumb" style={{ width: 96 }} />
        ) : (
          <div className="thumb" style={{ width: 96, height: 54, background: "var(--border)" }} />
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>
            샷 #{shot.order_index + 1}
            {approved ? (
              <span className="badge green" style={{ marginLeft: 6 }}>프레임 승인됨</span>
            ) : (
              <span className="badge" style={{ marginLeft: 6 }}>프레임 미승인</span>
            )}
            {generating && <span className="badge" style={{ marginLeft: 6 }}>클립 생성 중…</span>}
          </div>
          {latest && (
            <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 2 }}>
              최신 클립 {fmtSec(latest.duration_sec)} · {latest.resolution} · {latest.mode.toUpperCase()}
            </div>
          )}
          {latest?.output_path && (
            <video
              src={resolveFileUrl(latest.output_path) ?? ""}
              style={{ width: 160, marginTop: 6, borderRadius: 6 }}
              muted
              playsInline
              preload="metadata"
              onMouseEnter={(e) => void e.currentTarget.play().catch(() => {})}
              onMouseLeave={(e) => e.currentTarget.pause()}
            />
          )}
        </div>
      </div>
      <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
        <input
          className="input"
          style={{ flex: 1, fontSize: 12 }}
          placeholder="모션 프롬프트 — 움직이거나 변할 것만 (예: 카메라가 천천히 다가간다)"
          value={motionPrompt}
          disabled={!approved || busy}
          onChange={(e) => setMotionPrompt(e.target.value)}
        />
        <button
          className="btn btn-primary"
          style={{ fontSize: 12 }}
          disabled={!approved || busy || !motionPrompt.trim()}
          onClick={onGenerate}
          title={approved ? "승인된 프레임으로 클립 생성" : "클립 생성에는 승인된 프레임이 필요합니다"}
        >
          {busy ? "생성 중…" : "클립 생성"}
        </button>
        {latest && (
          <button className="btn" style={{ fontSize: 12 }} onClick={() => onAddClip(latest)}>
            타임라인에 추가
          </button>
        )}
        <button className="btn" style={{ fontSize: 12 }} onClick={onRefresh} title="클립 목록 새로고침">
          ↻
        </button>
      </div>
    </div>
  );
}

export default function TimelinePage() {
  const params = useParams();
  const projectId = params.id as string;
  const board = useBoardStore();
  const toast = useToastStore();
  const eventSeq = useWorkspaceStore((s) => s.eventSeq);
  const renders = useWorkspaceStore((s) => s.renders);
  const jobs = useWorkspaceStore((s) => s.jobs);

  const [timelines, setTimelines] = useState<Timeline[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [presets, setPresets] = useState<ExportPreset[]>([]);
  const [presetId, setPresetId] = useState<string>("");
  const [tab, setTab] = useState<Tab>("video");
  const [shotClips, setShotClips] = useState<Record<string, VideoClip[]>>({});
  const [prompts, setPrompts] = useState<Record<string, string>>({});
  const [genBusy, setGenBusy] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [renderEstimate, setRenderEstimate] = useState<number | null>(null);
  const [qc, setQc] = useState<QCReport | null>(null);
  const [ignoreWarn, setIgnoreWarn] = useState(false);
  const [qcBusy, setQcBusy] = useState(false);

  const shots = useMemo(
    () => board.scenes.flatMap((sc) => sc.shots).sort((a, b) => a.order_index - b.order_index),
    [board.scenes],
  );
  const locked = timeline?.status === "rendering";
  const renderJob = activeId ? jobs[activeId] : undefined;

  const loadTimeline = useCallback(async (id: string) => {
    const t = await apiGet<Timeline>(`/api/v1/timelines/${id}`);
    setTimeline(t);
  }, []);

  const loadShotClips = useCallback(async (shotId: string) => {
    const list = await apiGet<VideoClip[]>(`/api/v1/shots/${shotId}/videos`);
    setShotClips((m) => ({ ...m, [shotId]: list }));
  }, []);

  // Initial load: timelines (create one when empty) + presets + shots.
  useEffect(() => {
    void board.init(projectId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    (async () => {
      try {
        const [tls, prs] = await Promise.all([
          apiGet<Timeline[]>(`/api/v1/projects/${projectId}/timelines`),
          apiGet<ExportPreset[]>(`/api/v1/export-presets?project_id=${projectId}`),
        ]);
        setPresets(prs);
        const builtin = prs.find((p) => p.builtin);
        if (builtin) setPresetId((v) => v || builtin.id);
        let list = tls;
        if (list.length === 0) {
          const created = await apiPost<Timeline>(`/api/v1/projects/${projectId}/timelines`, {});
          list = [created];
        }
        setTimelines(list);
        setActiveId((v) => v ?? list[0].id);
      } catch (e) {
        toast.push(errMsg(e), { kind: "error" });
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    if (activeId) void loadTimeline(activeId).catch((e) => toast.push(errMsg(e), { kind: "error" }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, loadTimeline]);

  useEffect(() => {
    shots.forEach((s) => {
      if (!(s.id in shotClips)) void loadShotClips(s.id).catch(() => {});
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shots]);

  // SSE: render.completed -> reload the timeline; job completions bump eventSeq.
  useEffect(() => {
    if (!activeId) return;
    const r = renders[activeId];
    if (r) {
      void loadTimeline(activeId).catch(() => {});
      if (r.status === "done") toast.push("렌더가 완료되었습니다.");
      else if (r.status === "failed") toast.push("렌더에 실패했습니다.", { kind: "error" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventSeq]);

  /** Create a new timeline and switch to it (projects can hold several). */
  const createTimeline = async () => {
    if (creating) return;
    setCreating(true);
    try {
      const t = await apiPost<Timeline>(`/api/v1/projects/${projectId}/timelines`, {
        name: `타임라인 ${timelines.length + 1}`,
      });
      setTimelines((list) => [...list, t]);
      setActiveId(t.id);
      toast.push(`"${t.name}"을 만들었습니다.`);
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setCreating(false);
    }
  };

  const saveClipOrder = async (entries: TimelineClipEntry[]) => {
    if (!activeId || locked) return;
    setSaving(true);
    try {
      const t = await apiPatch<Timeline>(`/api/v1/timelines/${activeId}`, { clip_order: entries });
      setTimeline(t);
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setSaving(false);
    }
  };

  const generateClip = async (shot: Shot) => {
    const prompt = (prompts[shot.id] ?? "").trim();
    if (!prompt) return;
    setGenBusy((m) => ({ ...m, [shot.id]: true }));
    try {
      const res = await apiPost<VideoAccepted>(
        `/api/v1/shots/${shot.id}/video`,
        { mode: "i2v", motion_prompt: prompt },
        { idempotencyKey: `video-${shot.id}-${Date.now()}` },
      );
      toast.push(`클립 생성 시작 (예상 $${Number(res.estimated_cost_usd).toFixed(4)})`);
      await loadShotClips(shot.id);
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setGenBusy((m) => ({ ...m, [shot.id]: false }));
    }
  };

  const buildFromShots = async () => {
    if (!activeId || locked) return;
    try {
      const t = await apiPost<Timeline>(`/api/v1/timelines/${activeId}/build-from-shots`, {});
      setTimeline(t);
      toast.push(`${t.clip_order.length}개 클립을 샷 순서대로 조립했습니다.`);
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    }
  };

  const moveClip = (index: number, dir: -1 | 1) => {
    if (!timeline) return;
    const entries = [...timeline.clip_order];
    const j = index + dir;
    if (j < 0 || j >= entries.length) return;
    [entries[index], entries[j]] = [entries[j], entries[index]];
    void saveClipOrder(entries);
  };

  const trimClip = (index: number, field: "in_sec" | "out_sec", value: number) => {
    if (!timeline) return;
    const entries = timeline.clip_order.map((c, i) => (i === index ? { ...c, [field]: value } : c));
    void saveClipOrder(entries);
  };

  const removeClip = (index: number) => {
    if (!timeline) return;
    void saveClipOrder(timeline.clip_order.filter((_, i) => i !== index));
  };

  const render = async () => {
    if (!activeId || !presetId || locked) return;
    try {
      const res = await apiPost<RenderAccepted>(`/api/v1/timelines/${activeId}/render`, {
        export_preset_id: presetId,
        ignore_warnings: ignoreWarn, // QC gate (ADR-0006): warn needs this flag
      });
      setRenderEstimate(Number(res.estimated_cost_usd));
      await loadTimeline(activeId);
      toast.push("렌더를 시작했습니다.");
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    }
  };

  const loadQc = useCallback(async (id: string) => {
    try {
      const r = await apiGet<QCReport | null>(`/api/v1/timelines/${id}/qc-latest`);
      setQc(r);
    } catch {
      setQc(null);
    }
  }, []);

  const runQc = async () => {
    if (!activeId || locked || qcBusy) return;
    setQcBusy(true);
    try {
      await apiPost<{ qc_report_id: string }>(`/api/v1/timelines/${activeId}/qc`, {});
      toast.push("QC 검사를 시작했습니다.");
      await loadQc(activeId);
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setQcBusy(false);
    }
  };

  // qc.completed bumps eventSeq (workspace store) -> refresh the report.
  useEffect(() => {
    if (activeId) void loadQc(activeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, eventSeq]);

  const totalSec = (timeline?.clip_order ?? []).reduce((a, c) => a + (c.out_sec - c.in_sec), 0);
  const outputUrl = timeline?.output_path ? resolveFileUrl(timeline.output_path) : null;

  return (
    <div className="page">
      <div className="page-header">
        <h1>타임라인</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn" onClick={buildFromShots} disabled={locked || saving}>
            샷 순서대로 자동 조립
          </button>
          {activeId && <TimelineExportButtons timelineId={activeId} />}
        </div>
      </div>

      {/* Timeline selector: projects can hold several timelines. */}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
        <label className="small muted" htmlFor="timeline-select">
          타임라인
        </label>
        <select
          id="timeline-select"
          className="input"
          style={{ fontSize: 13, maxWidth: 320 }}
          value={activeId ?? ""}
          onChange={(e) => setActiveId(e.target.value)}
        >
          {timelines.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name} · {t.clip_order.length}개 클립
            </option>
          ))}
        </select>
        <button className="btn" style={{ fontSize: 13 }} disabled={creating} onClick={createTimeline}>
          {creating ? "생성 중…" : "새 타임라인"}
        </button>
      </div>

      <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
        {(["video", "subtitle"] as Tab[]).map((t) => (
          <button
            key={t}
            className={tab === t ? "btn btn-primary" : "btn"}
            style={{ fontSize: 13 }}
            onClick={() => setTab(t)}
          >
            {t === "video" ? "영상" : "자막"}
          </button>
        ))}
      </div>

      {tab === "subtitle" ? (
        <SubtitleTab
          projectId={projectId}
          timeline={timeline}
          locked={locked}
          onSaved={setTimeline}
          notify={(msg, kind) => toast.push(msg, { kind: kind ?? "info" })}
        />
      ) : (
        <>
          <AudioPanel
            projectId={projectId}
            timelineId={activeId}
            locked={locked}
            notify={(msg, kind) => toast.push(msg, { kind: kind ?? "info" })}
          />
          {/* Cut list */}
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">
              컷 리스트
              {timeline && (
                <span className="small muted" style={{ marginLeft: 8 }}>
                  {timeline.clip_order.length}개 클립 · 합계 {fmtSec(totalSec)}
                  {locked && <span className="badge" style={{ marginLeft: 8 }}>렌더 중 — 편집 잠금</span>}
                </span>
              )}
            </div>
            {(timeline?.clip_order ?? []).length === 0 && (
              <p style={{ color: "var(--muted)", fontSize: 13 }}>
                비어 있습니다. 아래 클립 스트립에서 클립을 생성하고 “타임라인에 추가”하거나,
                “샷 순서대로 자동 조립”을 눌러 주세요.
              </p>
            )}
            {(timeline?.clip_order ?? []).map((c, i, arr) => {
              const shot = shots.find((s) => s.id === c.shot_id);
              return (
                <div
                  key={`${c.video_id}-${i}`}
                  style={{ display: "flex", gap: 8, alignItems: "center", padding: "6px 0", borderTop: i ? "1px solid var(--border)" : undefined }}
                >
                  <span className="badge">{i + 1}</span>
                  <span style={{ fontSize: 13, minWidth: 90 }}>
                    {shot ? `샷 #${shot.order_index + 1}` : "샷?"}
                  </span>
                  <label style={{ fontSize: 12, color: "var(--muted)" }}>
                    in{" "}
                    <input
                      className="input"
                      type="number"
                      step={0.1}
                      min={0}
                      value={c.in_sec}
                      disabled={locked}
                      style={{ width: 70, fontSize: 12 }}
                      onChange={(e) => trimClip(i, "in_sec", Number(e.target.value))}
                    />
                  </label>
                  <label style={{ fontSize: 12, color: "var(--muted)" }}>
                    out{" "}
                    <input
                      className="input"
                      type="number"
                      step={0.1}
                      min={0}
                      value={c.out_sec}
                      disabled={locked}
                      style={{ width: 70, fontSize: 12 }}
                      onChange={(e) => trimClip(i, "out_sec", Number(e.target.value))}
                    />
                  </label>
                  <span style={{ fontSize: 12, color: "var(--muted)" }}>{fmtSec(c.out_sec - c.in_sec)}</span>
                  <span style={{ flex: 1 }} />
                  <button className="btn" style={{ fontSize: 12 }} disabled={locked || i === 0} onClick={() => moveClip(i, -1)}>↑</button>
                  <button className="btn" style={{ fontSize: 12 }} disabled={locked || i === arr.length - 1} onClick={() => moveClip(i, 1)}>↓</button>
                  <button className="btn" style={{ fontSize: 12 }} disabled={locked} onClick={() => removeClip(i)}>
                    삭제
                  </button>
                </div>
              );
            })}
          </div>

          {/* QC + Render */}
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">QC · 렌더</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
              <button className="btn" style={{ fontSize: 13 }} disabled={locked || qcBusy || (timeline?.clip_order.length ?? 0) === 0} onClick={runQc}>
                {qcBusy ? "QC 실행 중…" : "QC 실행"}
              </button>
              <span className="small muted">
                렌더에는 최신 QC 리포트가 필요합니다. 경고가 있으면 “경고 무시”를 켜야 렌더됩니다.
              </span>
            </div>
            {qc && (
              <div style={{ marginBottom: 10 }}>
                <QcReportView report={qc} />
              </div>
            )}
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <select
                className="input"
                value={presetId}
                disabled={locked}
                onChange={(e) => setPresetId(e.target.value)}
                style={{ fontSize: 13 }}
              >
                {presets.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} · {p.resolution} · {p.fps}fps{p.builtin ? "" : " (커스텀)"}
                  </option>
                ))}
              </select>
              <label className="small" style={{ display: "flex", gap: 4, alignItems: "center" }}>
                <input type="checkbox" checked={ignoreWarn} disabled={locked} onChange={(e) => setIgnoreWarn(e.target.checked)} />
                QC 경고 무시
              </label>
              <button className="btn btn-primary" disabled={locked || !presetId || (timeline?.clip_order.length ?? 0) === 0} onClick={render}>
                {locked ? "렌더 중…" : "렌더 시작"}
              </button>
              {renderEstimate != null && (
                <span className="small muted">예상 렌더 비용 ${renderEstimate.toFixed(4)} (로컬 ffmpeg)</span>
              )}
            </div>
            {locked && renderJob && (
              <div style={{ marginTop: 8 }}>
                <div style={{ height: 8, background: "var(--border)", borderRadius: 4 }}>
                  <div
                    style={{ height: 8, borderRadius: 4, background: "var(--primary)", width: `${renderJob.progress_pct ?? 0}%` }}
                  />
                </div>
                <div className="small muted" style={{ marginTop: 4 }}>
                  {renderJob.stage} · {Math.round(renderJob.progress_pct ?? 0)}%
                </div>
              </div>
            )}
            {timeline?.status === "failed" && (
              <p style={{ color: "var(--danger)", fontSize: 13 }}>
                렌더 실패: {timeline.error_message ?? timeline.error_code}
              </p>
            )}
            {timeline?.status === "done" && outputUrl && (
              <div style={{ marginTop: 12 }}>
                <video src={outputUrl} controls style={{ width: "100%", maxWidth: 720, borderRadius: 8 }} />
                <div style={{ marginTop: 6 }}>
                  <a className="btn" href={outputUrl} download="output.mp4" style={{ fontSize: 13, textDecoration: "none" }}>
                    MP4 다운로드
                  </a>
                </div>
              </div>
            )}
          </div>

          {/* Clip strip */}
          <div className="card-title" style={{ marginBottom: 8 }}>클립 스트립</div>
          {board.loading && <p>불러오는 중…</p>}
          <div style={{ display: "grid", gap: 10 }}>
            {shots.map((shot) => (
              <ShotClipRow
                key={shot.id}
                shot={shot}
                clips={shotClips[shot.id] ?? []}
                motionPrompt={prompts[shot.id] ?? ""}
                setMotionPrompt={(v) => setPrompts((m) => ({ ...m, [shot.id]: v }))}
                busy={!!genBusy[shot.id]}
                onGenerate={() => generateClip(shot)}
                onAddClip={(clip) =>
                  saveClipOrder([
                    ...(timeline?.clip_order ?? []),
                    { shot_id: clip.shot_id, video_id: clip.id, in_sec: 0, out_sec: clip.duration_sec },
                  ])
                }
                onRefresh={() => loadShotClips(shot.id)}
              />
            ))}
          </div>
          {shots.length === 0 && !board.loading && (
            <p style={{ color: "var(--muted)" }}>샷이 없습니다. 보드에서 샷을 먼저 만들어 주세요.</p>
          )}
          <ThumbnailSection projectId={projectId} timelines={timelines} />
        </>
      )}
    </div>
  );
}
