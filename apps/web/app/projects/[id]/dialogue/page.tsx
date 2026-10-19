// Dialogue lab (Phase 5D, P1-5): TTS dialogue per shot, voice clone per
// character, and narration-based B-roll suggestions per timeline.
// New page only — no existing web files were modified.
"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { apiGet, apiPost } from "@/lib/api-client";
import { ApiError } from "@/lib/errors";

type DialogueJob = {
  job_id: string;
  estimated_cost_usd?: string | null;
};

type AudioJob = {
  id: string;
  status: string;
  output_path: string | null;
  duration_sec: number | null;
  voice_id: string | null;
  emotion_weights: Record<string, number> | null;
  error_message: string | null;
};

type BrollSuggestion = {
  asset_id: string;
  asset_name: string;
  keyword: string;
  score: number;
};

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "요청에 실패했습니다.";
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section
      style={{ border: "1px solid #e2e2e2", borderRadius: 8, padding: 16, marginBottom: 16 }}
    >
      <h2 style={{ margin: "0 0 12px", fontSize: 16 }}>{title}</h2>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "block", marginBottom: 8, fontSize: 13 }}>
      <span style={{ display: "block", marginBottom: 4, color: "#555" }}>{label}</span>
      {children}
    </label>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "6px 8px",
  border: "1px solid #ccc",
  borderRadius: 4,
  fontSize: 13,
};

export default function DialogueLabPage() {
  const params = useParams();
  const projectId = String(params.id);

  const [shotId, setShotId] = useState("");
  const [characterId, setCharacterId] = useState("");
  const [text, setText] = useState("");
  const [emotions, setEmotions] = useState('{"joy": 0.8}');
  const [targetDur, setTargetDur] = useState("");
  const [job, setJob] = useState<AudioJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [cloneCharId, setCloneCharId] = useState("");
  const [sampleAssetId, setSampleAssetId] = useState("");
  const [clonedVoice, setClonedVoice] = useState("");

  const [timelineId, setTimelineId] = useState("");
  const [suggestions, setSuggestions] = useState<BrollSuggestion[]>([]);
  const [keywords, setKeywords] = useState<string[]>([]);

  async function runDialogue() {
    setBusy(true);
    setError("");
    setJob(null);
    try {
      let emotionWeights: Record<string, number> | undefined;
      if (emotions.trim()) {
        emotionWeights = JSON.parse(emotions) as Record<string, number>;
      }
      const accepted = await apiPost<DialogueJob>(
        `/api/v1/shots/${shotId}/dialogue`,
        {
          character_id: characterId,
          text,
          emotion_weights: emotionWeights,
          target_duration_sec: targetDur ? Number(targetDur) : undefined,
        },
        { idempotencyKey: `dialogue-${Date.now()}` },
      );
      const status = await apiGet<AudioJob>(`/api/v1/audio/${accepted.job_id}`);
      setJob(status);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function runClone() {
    setBusy(true);
    setError("");
    setClonedVoice("");
    try {
      const res = await apiPost<{ voice_id: string }>(
        `/api/v1/characters/${cloneCharId}/voice/clone`,
        { sample_asset_id: sampleAssetId },
      );
      setClonedVoice(res.voice_id);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function runBroll() {
    setBusy(true);
    setError("");
    setSuggestions([]);
    try {
      const res = await apiPost<{
        suggestions: BrollSuggestion[];
        keywords: string[];
      }>(`/api/v1/projects/${projectId}/broll-suggest`, { timeline_id: timelineId });
      setSuggestions(res.suggestions);
      setKeywords(res.keywords);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{ padding: 24, maxWidth: 760 }}>
      <h1 style={{ fontSize: 20, marginBottom: 4 }}>대사 · 음성 랩</h1>
      <p style={{ color: "#666", fontSize: 13, marginBottom: 16 }}>
        TTS 대사 생성, 음성 복제, 내레이션 기반 B-roll 제안을 한 곳에서 실행합니다.
      </p>
      {error && (
        <p style={{ color: "#b00020", fontSize: 13, marginBottom: 12 }}>{error}</p>
      )}

      <Card title="대사 TTS (POST /shots/{id}/dialogue)">
        <Field label="Shot ID">
          <input style={inputStyle} value={shotId} onChange={(e) => setShotId(e.target.value)} />
        </Field>
        <Field label="Character ID">
          <input
            style={inputStyle}
            value={characterId}
            onChange={(e) => setCharacterId(e.target.value)}
          />
        </Field>
        <Field label="대사 텍스트">
          <textarea
            style={{ ...inputStyle, minHeight: 64 }}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </Field>
        <Field label="감정 가중치 (JSON, 각 0~1)">
          <input style={inputStyle} value={emotions} onChange={(e) => setEmotions(e.target.value)} />
        </Field>
        <Field label="목표 길이 (초, 선택 — 립싱크 ±10% 맞춤)">
          <input style={inputStyle} value={targetDur} onChange={(e) => setTargetDur(e.target.value)} />
        </Field>
        <button disabled={busy || !shotId || !characterId || !text} onClick={runDialogue}>
          {busy ? "생성 중…" : "TTS 생성"}
        </button>
        {job && (
          <dl style={{ fontSize: 13, marginTop: 12 }}>
            <dt>상태</dt>
            <dd>{job.status}</dd>
            <dt>산출물</dt>
            <dd>{job.output_path ?? "-"}</dd>
            <dt>길이</dt>
            <dd>{job.duration_sec != null ? `${job.duration_sec.toFixed(2)}s` : "-"}</dd>
            <dt>voice</dt>
            <dd>{job.voice_id ?? "(기본)"}</dd>
          </dl>
        )}
      </Card>

      <Card title="음성 복제 (POST /characters/{id}/voice/clone)">
        <Field label="Character ID">
          <input
            style={inputStyle}
            value={cloneCharId}
            onChange={(e) => setCloneCharId(e.target.value)}
          />
        </Field>
        <Field label="샘플 오디오 Asset ID">
          <input
            style={inputStyle}
            value={sampleAssetId}
            onChange={(e) => setSampleAssetId(e.target.value)}
          />
        </Field>
        <button disabled={busy || !cloneCharId || !sampleAssetId} onClick={runClone}>
          {busy ? "복제 중…" : "음성 복제"}
        </button>
        {clonedVoice && (
          <p style={{ fontSize: 13, marginTop: 8 }}>
            발급된 voice_id: <code>{clonedVoice}</code>
          </p>
        )}
      </Card>

      <Card title="B-roll 자동 제안 (POST /projects/{id}/broll-suggest)">
        <Field label="Timeline ID">
          <input
            style={inputStyle}
            value={timelineId}
            onChange={(e) => setTimelineId(e.target.value)}
          />
        </Field>
        <button disabled={busy || !timelineId} onClick={runBroll}>
          {busy ? "분석 중…" : "B-roll 제안"}
        </button>
        {keywords.length > 0 && (
          <p style={{ fontSize: 12, color: "#666" }}>
            키워드: {keywords.join(", ")}
          </p>
        )}
        {suggestions.length > 0 && (
          <ul style={{ fontSize: 13, paddingLeft: 18 }}>
            {suggestions.map((s) => (
              <li key={s.asset_id}>
                {s.asset_name} — 키워드 “{s.keyword}” (score {s.score})
              </li>
            ))}
          </ul>
        )}
      </Card>
    </main>
  );
}
