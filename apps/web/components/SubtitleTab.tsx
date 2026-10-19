// Subtitle tab for the timeline screen (Phase 4, F-13, ADR-0002).
// Tracks, cues, style, generate-from-dialogue, export, and the render-time
// subtitle options (mode + track selection) saved on the timeline.
"use client";

import { useCallback, useEffect, useState } from "react";
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPut,
  API_BASE,
} from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import type {
  SubtitleCue,
  SubtitleStyle,
  SubtitleTrack,
  Timeline,
} from "@/lib/types";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "요청에 실패했습니다.";
}

function fmtT(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = (sec % 60).toFixed(1).padStart(4, "0");
  return `${m}:${s}`;
}

export function SubtitleTab({
  projectId,
  timeline,
  locked,
  onSaved,
  notify,
}: {
  projectId: string;
  timeline: Timeline | null;
  locked: boolean;
  onSaved: (t: Timeline) => void;
  notify: (msg: string, kind?: "error" | "info") => void;
}) {
  const [tracks, setTracks] = useState<SubtitleTrack[]>([]);
  const [trackId, setTrackId] = useState<string>("");
  const [cues, setCues] = useState<SubtitleCue[]>([]);
  const [style, setStyle] = useState<SubtitleStyle | null>(null);
  const [newLang, setNewLang] = useState("ko");
  const [newLabel, setNewLabel] = useState("");
  const [cueStart, setCueStart] = useState("0");
  const [cueEnd, setCueEnd] = useState("3");
  const [cueText, setCueText] = useState("");
  const [mode, setMode] = useState("single");
  const [selTracks, setSelTracks] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const loadTracks = useCallback(async () => {
    const list = await apiGet<SubtitleTrack[]>(
      `/api/v1/projects/${projectId}/subtitle-tracks`,
    );
    setTracks(list);
    setTrackId((v) => {
      if (list.some((t) => t.id === v)) return v;
      const def = list.find((t) => t.is_default) ?? list[0];
      return def ? def.id : "";
    });
  }, [projectId]);

  const loadCues = useCallback(async (tid: string) => {
    if (!tid) {
      setCues([]);
      return;
    }
    const list = await apiGet<SubtitleCue[]>(`/api/v1/subtitle-tracks/${tid}/cues`);
    setCues(list);
  }, []);

  const loadStyle = useCallback(async () => {
    try {
      const st = await apiGet<SubtitleStyle>(
        `/api/v1/projects/${projectId}/subtitle-style`,
      );
      setStyle(st);
    } catch {
      setStyle(null);
    }
  }, [projectId]);

  useEffect(() => {
    void loadTracks().catch((e) => notify(errMsg(e), "error"));
    void loadStyle().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    void loadCues(trackId).catch((e) => notify(errMsg(e), "error"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trackId]);

  useEffect(() => {
    if (timeline) {
      setMode(timeline.subtitle_mode ?? "single");
      setSelTracks(timeline.subtitle_track_ids ?? []);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeline?.id]);

  const createTrack = async () => {
    if (locked) return;
    setBusy(true);
    try {
      const t = await apiPost<SubtitleTrack>(
        `/api/v1/projects/${projectId}/subtitle-tracks`,
        { language_code: newLang.trim() || "ko", label: newLabel.trim() || null },
      );
      setNewLabel("");
      await loadTracks();
      setTrackId(t.id);
      notify("자막 트랙을 만들었습니다.");
    } catch (e) {
      notify(errMsg(e), "error");
    } finally {
      setBusy(false);
    }
  };

  const deleteTrack = async (tid: string) => {
    if (locked || !window.confirm("이 트랙과 모든 큐를 삭제할까요?")) return;
    try {
      await apiDelete(`/api/v1/projects/${projectId}/subtitle-tracks/${tid}`);
      await loadTracks();
      notify("트랙을 삭제했습니다.");
    } catch (e) {
      notify(errMsg(e), "error");
    }
  };

  const saveCues = async (next: { start_sec: number; end_sec: number; text: string }[]) => {
    if (!trackId || locked) return;
    setBusy(true);
    try {
      const saved = await apiPut<SubtitleCue[]>(
        `/api/v1/subtitle-tracks/${trackId}/cues`,
        { cues: next },
      );
      setCues(saved);
      notify(`${saved.length}개 큐를 저장했습니다.`);
    } catch (e) {
      notify(errMsg(e), "error");
    } finally {
      setBusy(false);
    }
  };

  const addCue = () => {
    const s = Number(cueStart);
    const e = Number(cueEnd);
    if (!cueText.trim() || Number.isNaN(s) || Number.isNaN(e)) {
      notify("시작·종료 시간과 텍스트를 입력해 주세요.", "error");
      return;
    }
    void saveCues([
      ...cues.map((c) => ({ start_sec: c.start_sec, end_sec: c.end_sec, text: c.text })),
      { start_sec: s, end_sec: e, text: cueText.trim() },
    ]);
    setCueText("");
  };

  const removeCue = (id: string) => {
    void saveCues(
      cues
        .filter((c) => c.id !== id)
        .map((c) => ({ start_sec: c.start_sec, end_sec: c.end_sec, text: c.text })),
    );
  };

  const generate = async () => {
    if (locked || !trackId) {
      notify("자막 트랙을 먼저 선택해 주세요.", "error");
      return;
    }
    setBusy(true);
    try {
      const saved = await apiPost<SubtitleCue[]>(
        `/api/v1/subtitle-tracks/${trackId}/generate`,
        {},
      );
      await loadCues(trackId);
      notify(`${saved.length}개 큐를 샷 대사에서 생성했습니다.`);
    } catch (e) {
      notify(errMsg(e), "error");
    } finally {
      setBusy(false);
    }
  };

  const saveStyle = async () => {
    if (!style || locked) return;
    setBusy(true);
    try {
      const saved = await apiPut<SubtitleStyle>(
        `/api/v1/projects/${projectId}/subtitle-style`,
        {
          font_family: style.font_family,
          font_size_px: style.font_size_px,
          color: style.color,
          background_color: style.background_color,
          background_opacity: style.background_opacity,
          position: style.position,
          safe_area_margin_pct: style.safe_area_margin_pct,
          line_break_rule: style.line_break_rule,
        },
      );
      setStyle(saved);
      notify("자막 스타일을 저장했습니다.");
    } catch (e) {
      notify(errMsg(e), "error");
    } finally {
      setBusy(false);
    }
  };

  const saveRenderOpts = async () => {
    if (!timeline || locked) return;
    setBusy(true);
    try {
      const t = await apiPatch<Timeline>(`/api/v1/timelines/${timeline.id}`, {
        subtitle_mode: mode,
        subtitle_track_ids: selTracks,
      });
      onSaved(t);
      notify("렌더 자막 옵션을 저장했습니다.");
    } catch (e) {
      notify(errMsg(e), "error");
    } finally {
      setBusy(false);
    }
  };

  const toggleTrack = (tid: string) =>
    setSelTracks((v) => (v.includes(tid) ? v.filter((x) => x !== tid) : [...v, tid]));

  return (
    <div style={{ display: "grid", gap: 12 }}>
      {/* Tracks */}
      <div className="card">
        <div className="card-title">자막 트랙</div>
        {tracks.map((t) => (
          <div key={t.id} style={{ display: "flex", gap: 8, alignItems: "center", padding: "4px 0" }}>
            <input
              type="radio"
              name="track"
              checked={trackId === t.id}
              onChange={() => setTrackId(t.id)}
            />
            <span style={{ fontSize: 13, fontWeight: 600 }}>
              {t.label || t.language_code}
              {t.is_default && <span className="badge green" style={{ marginLeft: 6 }}>기본</span>}
            </span>
            <span className="small muted">{t.language_code}</span>
            <span style={{ flex: 1 }} />
            <button className="btn" style={{ fontSize: 12 }} disabled={locked} onClick={() => deleteTrack(t.id)}>
              삭제
            </button>
          </div>
        ))}
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <input
            className="input"
            style={{ width: 90, fontSize: 12 }}
            value={newLang}
            disabled={locked}
            onChange={(e) => setNewLang(e.target.value)}
            placeholder="ko"
          />
          <input
            className="input"
            style={{ flex: 1, fontSize: 12 }}
            value={newLabel}
            disabled={locked}
            onChange={(e) => setNewLabel(e.target.value)}
            placeholder="트랙 이름 (선택)"
          />
          <button className="btn" style={{ fontSize: 12 }} disabled={locked || busy} onClick={createTrack}>
            트랙 추가
          </button>
          <button className="btn" style={{ fontSize: 12 }} disabled={locked || busy} onClick={generate} title="샷 대사에서 큐 자동 생성">
            대사에서 자동 생성
          </button>
        </div>
      </div>

      {/* Cues */}
      <div className="card">
        <div className="card-title">
          자막 큐
          {trackId && (
            <span style={{ marginLeft: 8 }}>
              <a
                className="btn"
                style={{ fontSize: 12, textDecoration: "none" }}
                href={`${API_BASE}/api/v1/subtitle-tracks/${trackId}/export?format=srt`}
                target="_blank"
                rel="noreferrer"
              >
                SRT
              </a>{" "}
              <a
                className="btn"
                style={{ fontSize: 12, textDecoration: "none" }}
                href={`${API_BASE}/api/v1/subtitle-tracks/${trackId}/export?format=vtt`}
                target="_blank"
                rel="noreferrer"
              >
                VTT
              </a>
            </span>
          )}
        </div>
        {!trackId && <p className="small muted">트랙을 먼저 선택해 주세요.</p>}
        {cues.map((c) => (
          <div key={c.id} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "4px 0", borderTop: "1px solid var(--border)" }}>
            <span className="badge">{fmtT(c.start_sec)} → {fmtT(c.end_sec)}</span>
            <span style={{ fontSize: 13, flex: 1 }}>{c.text}</span>
            <button className="btn" style={{ fontSize: 12 }} disabled={locked} onClick={() => removeCue(c.id)}>
              삭제
            </button>
          </div>
        ))}
        {trackId && (
          <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
            <input className="input" style={{ width: 70, fontSize: 12 }} type="number" step={0.1} min={0} value={cueStart} disabled={locked} onChange={(e) => setCueStart(e.target.value)} title="시작(초)" />
            <input className="input" style={{ width: 70, fontSize: 12 }} type="number" step={0.1} min={0} value={cueEnd} disabled={locked} onChange={(e) => setCueEnd(e.target.value)} title="종료(초)" />
            <input className="input" style={{ flex: 1, fontSize: 12 }} value={cueText} disabled={locked} onChange={(e) => setCueText(e.target.value)} placeholder="자막 텍스트" />
            <button className="btn" style={{ fontSize: 12 }} disabled={locked || busy} onClick={addCue}>
              큐 추가
            </button>
          </div>
        )}
        <p className="small muted" style={{ marginTop: 6 }}>
          겹치는 시간대는 저장 시 422로 거부됩니다. 1~7초 권장 길이를 벗어나면 QC에서 경고로 표시됩니다.
        </p>
      </div>

      {/* Style */}
      {style && (
        <div className="card">
          <div className="card-title">자막 스타일 (프로젝트 기본)</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <label className="small muted">
              글꼴{" "}
              <input className="input" style={{ width: 150, fontSize: 12 }} value={style.font_family} disabled={locked} onChange={(e) => setStyle({ ...style, font_family: e.target.value })} />
            </label>
            <label className="small muted">
              크기(px){" "}
              <input className="input" style={{ width: 70, fontSize: 12 }} type="number" min={8} max={400} value={style.font_size_px} disabled={locked} onChange={(e) => setStyle({ ...style, font_size_px: Number(e.target.value) })} />
            </label>
            <label className="small muted">
              색상{" "}
              <input className="input" type="color" value={style.color} disabled={locked} onChange={(e) => setStyle({ ...style, color: e.target.value })} />
            </label>
            <label className="small muted">
              위치{" "}
              <select className="input" style={{ fontSize: 12 }} value={style.position} disabled={locked} onChange={(e) => setStyle({ ...style, position: e.target.value })}>
                <option value="top">상단</option>
                <option value="middle">중앙</option>
                <option value="bottom">하단</option>
              </select>
            </label>
            <label className="small muted">
              안전여백(%){" "}
              <input className="input" style={{ width: 64, fontSize: 12 }} type="number" step={0.5} min={0} max={30} value={style.safe_area_margin_pct} disabled={locked} onChange={(e) => setStyle({ ...style, safe_area_margin_pct: Number(e.target.value) })} />
            </label>
            <button className="btn" style={{ fontSize: 12 }} disabled={locked || busy} onClick={saveStyle}>
              스타일 저장
            </button>
          </div>
        </div>
      )}

      {/* Render options */}
      <div className="card">
        <div className="card-title">렌더 자막 옵션</div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <label className="small muted">
            모드{" "}
            <select className="input" style={{ fontSize: 12 }} value={mode} disabled={locked} onChange={(e) => setMode(e.target.value)}>
              <option value="none">없음</option>
              <option value="single">단일 (첫 번째 트랙)</option>
              <option value="dual">이중 (두 트랙 동시)</option>
            </select>
          </label>
          {tracks.map((t) => (
            <label key={t.id} className="small" style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <input type="checkbox" checked={selTracks.includes(t.id)} disabled={locked} onChange={() => toggleTrack(t.id)} />
              {t.label || t.language_code}
            </label>
          ))}
          <button className="btn btn-primary" style={{ fontSize: 12 }} disabled={locked || busy || !timeline} onClick={saveRenderOpts}>
            타임라인에 저장
          </button>
        </div>
        <p className="small muted" style={{ marginTop: 6 }}>
          자막은 렌더 단계에서만 영상에 새겨집니다(burn-in). 프리셋의 “자막 새기기”가 켜져 있어야 합니다.
        </p>
      </div>
    </div>
  );
}
