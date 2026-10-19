// Audio panel for the timeline screen (Phase 4, design §4.14).
// Upload (assets) -> register as audio -> place clips on the timeline.
// P0 has no TTS/dubbing: bgm|sfx uploads only.
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  apiDelete,
  apiGet,
  apiPost,
  apiUpload,
} from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import type { AudioClip, AudioGen } from "@/lib/types";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "요청에 실패했습니다.";
}

export function AudioPanel({
  projectId,
  timelineId,
  locked,
  notify,
}: {
  projectId: string;
  timelineId: string | null;
  locked: boolean;
  notify: (msg: string, kind?: "error" | "info") => void;
}) {
  const [audios, setAudios] = useState<AudioGen[]>([]);
  const [clips, setClips] = useState<AudioClip[]>([]);
  const [uploading, setUploading] = useState(false);
  const [audioId, setAudioId] = useState("");
  const [start, setStart] = useState("0");
  const [dur, setDur] = useState("");
  const [gain, setGain] = useState("0");
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    const [a, c] = await Promise.all([
      apiGet<AudioGen[]>(`/api/v1/projects/${projectId}/audio`),
      timelineId
        ? apiGet<AudioClip[]>(`/api/v1/timelines/${timelineId}/audio-clips`)
        : Promise.resolve([] as AudioClip[]),
    ]);
    setAudios(a);
    setClips(c);
    setAudioId((v) => (a.some((x) => x.id === v) ? v : (a[0]?.id ?? "")));
  }, [projectId, timelineId]);

  useEffect(() => {
    void load().catch((e) => notify(errMsg(e), "error"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, timelineId]);

  const upload = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file || locked) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("kind", "audio");
      form.append("project_id", projectId);
      const up = await apiUpload<{ asset_id: string }>(`/api/v1/assets/upload`, form);
      await apiPost(`/api/v1/projects/${projectId}/audio`, {
        asset_id: up.asset_id,
        kind: "bgm",
      });
      if (fileRef.current) fileRef.current.value = "";
      await load();
      notify("오디오를 등록했습니다.");
    } catch (e) {
      notify(errMsg(e), "error");
    } finally {
      setUploading(false);
    }
  };

  const place = async () => {
    if (!timelineId || !audioId || locked) return;
    try {
      await apiPost(`/api/v1/timelines/${timelineId}/audio-clips`, {
        audio_id: audioId,
        start_sec: Number(start),
        duration_sec: dur.trim() ? Number(dur) : null,
        gain_db: Number(gain),
      });
      await load();
      notify("오디오 클립을 배치했습니다.");
    } catch (e) {
      notify(errMsg(e), "error");
    }
  };

  const remove = async (clipId: string) => {
    if (!timelineId || locked) return;
    try {
      await apiDelete(`/api/v1/timelines/${timelineId}/audio-clips/${clipId}`);
      await load();
    } catch (e) {
      notify(errMsg(e), "error");
    }
  };

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-title">오디오 (BGM/SFX)</div>
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
        <input ref={fileRef} type="file" accept="audio/*,.wav,.mp3" disabled={locked} style={{ fontSize: 12 }} />
        <button className="btn" style={{ fontSize: 12 }} disabled={locked || uploading} onClick={upload}>
          {uploading ? "업로드 중…" : "업로드·등록"}
        </button>
        <span className="small muted">wav/mp3 — TTS·더빙은 P1에서 제공됩니다</span>
      </div>
      {audios.length > 0 && timelineId && (
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
          <select className="input" style={{ fontSize: 12 }} value={audioId} disabled={locked} onChange={(e) => setAudioId(e.target.value)}>
            {audios.map((a) => (
              <option key={a.id} value={a.id}>
                {a.kind} · {a.duration_sec != null ? `${a.duration_sec.toFixed(1)}s` : "?"} · {a.id.slice(0, 8)}
              </option>
            ))}
          </select>
          <label className="small muted">
            시작(초){" "}
            <input className="input" style={{ width: 64, fontSize: 12 }} type="number" step={0.1} min={0} value={start} disabled={locked} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label className="small muted">
            길이(초, 비우면 전체){" "}
            <input className="input" style={{ width: 64, fontSize: 12 }} type="number" step={0.1} min={0} value={dur} disabled={locked} onChange={(e) => setDur(e.target.value)} />
          </label>
          <label className="small muted">
            게인(dB){" "}
            <input className="input" style={{ width: 60, fontSize: 12 }} type="number" step={1} value={gain} disabled={locked} onChange={(e) => setGain(e.target.value)} />
          </label>
          <button className="btn" style={{ fontSize: 12 }} disabled={locked || !audioId} onClick={place}>
            타임라인에 배치
          </button>
        </div>
      )}
      {clips.map((c) => {
        const a = audios.find((x) => x.id === c.audio_id);
        return (
          <div key={c.id} style={{ display: "flex", gap: 8, alignItems: "center", padding: "4px 0", borderTop: "1px solid var(--border)", fontSize: 13 }}>
            <span className="badge">{c.kind}</span>
            <span>
              {c.start_sec.toFixed(1)}s{""}
              {c.duration_sec != null ? ` · ${c.duration_sec.toFixed(1)}s` : ` · 전체(${a?.duration_sec?.toFixed(1) ?? "?"}s)`}
              {" · "}
              {c.gain_db > 0 ? `+${c.gain_db}` : c.gain_db}dB
            </span>
            <span style={{ flex: 1 }} />
            <button className="btn" style={{ fontSize: 12 }} disabled={locked} onClick={() => remove(c.id)}>
              삭제
            </button>
          </div>
        );
      })}
      {clips.length === 0 && <p className="small muted">배치된 오디오가 없습니다. 렌더 시 타임라인 아래에 믹싱됩니다.</p>}
    </div>
  );
}
