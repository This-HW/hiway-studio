// Subtitle-driven edit analysis panel (P1-6, design §8.5).
// Calls POST /subtitle-tracks/{id}/apply-to-timeline and shows the per-clip
// duration proposals + mismatch warnings. Analysis only — the timeline is
// never rewritten here; the user confirms via the timeline tab.
"use client";

import { useEffect, useState } from "react";
import {
  applySubtitlesToTimeline,
  listSubtitleTracks,
  listTimelines,
  type SubtitleEditAnalysis,
  type SubtitleTrack,
  type TimelineSummary,
} from "@/lib/episodes-api";

export function SubtitleEditPanel({ projectId }: { projectId: string }) {
  const [tracks, setTracks] = useState<SubtitleTrack[]>([]);
  const [timelines, setTimelines] = useState<TimelineSummary[]>([]);
  const [trackId, setTrackId] = useState("");
  const [timelineId, setTimelineId] = useState("");
  const [cps, setCps] = useState(12);
  const [analysis, setAnalysis] = useState<SubtitleEditAnalysis | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const [ts, tls] = await Promise.all([
          listSubtitleTracks(projectId),
          listTimelines(projectId),
        ]);
        setTracks(ts);
        setTimelines(tls);
        if (ts.length > 0) setTrackId(ts[0].id);
        if (tls.length > 0) setTimelineId(tls[0].id);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [projectId]);

  const onRun = async () => {
    if (!trackId || !timelineId) return;
    setRunning(true);
    setError(null);
    try {
      setAnalysis(
        await applySubtitlesToTimeline(trackId, {
          timeline_id: timelineId,
          cps,
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <section className="card" aria-label="자막 기반 편집 분석">
      <h2 className="section-title">자막 기반 편집 분석</h2>
      <p className="muted">
        자막 큐 텍스트 길이를 발화 시간(글자수/CPS)으로 환산해 연결된 클립
        길이와 비교합니다. 분석·제안만 수행하고 타임라인을 직접 바꾸지
        않습니다.
      </p>
      {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div className="field">
          <label>자막 트랙</label>
          <select
            className="input"
            value={trackId}
            onChange={(e) => setTrackId(e.target.value)}
          >
            {tracks.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label ?? t.language_code} ({t.language_code})
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>타임라인</label>
          <select
            className="input"
            value={timelineId}
            onChange={(e) => setTimelineId(e.target.value)}
          >
            {timelines.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>CPS (초당 글자수)</label>
          <input
            className="input"
            type="number"
            min={1}
            step={0.5}
            value={cps}
            onChange={(e) => setCps(Number(e.target.value) || 12)}
          />
        </div>
      </div>
      <div style={{ marginTop: 8 }}>
        <button
          className="btn"
          onClick={() => void onRun()}
          disabled={running || !trackId || !timelineId}
        >
          {running ? "분석 중…" : "분석 실행"}
        </button>
      </div>

      {analysis && (
        <div style={{ marginTop: 16 }}>
          {analysis.warnings.length > 0 && (
            <div
              className="card"
              style={{ borderColor: "var(--warn, #b58900)" }}
              aria-label="경고"
            >
              <strong>경고 ({analysis.warnings.length})</strong>
              <ul>
                {analysis.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th>큐 구간</th>
                <th>글자수</th>
                <th>현재 클립</th>
                <th>예상 발화</th>
                <th>판정</th>
              </tr>
            </thead>
            <tbody>
              {analysis.adjustments.map((a) => (
                <tr key={a.cue_id}>
                  <td>
                    {a.cue_start_sec.toFixed(2)}s–{a.cue_end_sec.toFixed(2)}s
                  </td>
                  <td>{a.cue_text_len}</td>
                  <td>{a.current_dur_sec.toFixed(2)}s</td>
                  <td>{a.suggested_dur_sec.toFixed(2)}s</td>
                  <td>{a.fits ? "적합" : "컷 초과"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
