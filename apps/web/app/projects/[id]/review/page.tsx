// Review: compare frame takes side by side and approve one per shot (P0-7).
// Video generation (Phase 3) is not wired here: an unapproved shot shows the
// approval requirement instead of a generate-video button.
"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { resolveFileUrl } from "@/lib/files";
import type { GenerationJob, Shot } from "@/lib/types";
import { apiPost } from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import { useBoardStore } from "@/stores/board";
import { useWorkspaceStore } from "@/stores/workspace";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "요청에 실패했습니다.";
}

type VariationJob = {
  job_id: string;
  prompt_final: string;
  seed: number;
};

// P1-3 배리에이션 (§05C-3): 샷의 프롬프트·시드를 바꿔 후보 추가 생성
function VariationSection({ shotId }: { shotId: string }) {
  const [count, setCount] = useState(3);
  const [axis, setAxis] = useState<"prompt" | "seed">("prompt");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{
    count: number;
    estimated_cost_usd: string;
    variations: VariationJob[];
  } | null>(null);

  const run = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await apiPost<{
        count: number;
        estimated_cost_usd: string;
        variations: VariationJob[];
      }>(
        `/api/v1/shots/${shotId}/frame/variations`,
        { count, variation_axis: axis },
        { idempotencyKey: `variations-${shotId}-${Date.now()}` },
      );
      setResult(res);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ marginTop: 12, borderTop: "1px solid #eee", paddingTop: 8 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>배리에이션 (P1-3)</div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <label style={{ fontSize: 12 }}>개수
          <select className="input" value={count} onChange={(e) => setCount(Number(e.target.value))} style={{ marginLeft: 4 }}>
            {[2, 3, 4].map((n) => <option key={n} value={n}>{n}개</option>)}
          </select>
        </label>
        <label style={{ fontSize: 12 }}>변형 축
          <select className="input" value={axis} onChange={(e) => setAxis(e.target.value as "prompt" | "seed")} style={{ marginLeft: 4 }}>
            <option value="prompt">프롬프트</option>
            <option value="seed">시드</option>
          </select>
        </label>
        <button className="btn" disabled={busy} onClick={() => void run()}>
          {busy ? "생성 중…" : "배리에이션 생성"}
        </button>
        {result && <span style={{ fontSize: 12, color: "var(--muted)" }}>예상 ~${result.estimated_cost_usd}</span>}
      </div>
      {error && <p style={{ color: "red", fontSize: 12 }}>{error} <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => void run()}>재시도</button></p>}
      {result && (
        <ul style={{ fontSize: 12, color: "var(--muted)", paddingLeft: 18, marginTop: 6 }}>
          {result.variations.map((v) => (
            <li key={v.job_id}>
              seed {v.seed} — {v.prompt_final.slice(0, 80)}{v.prompt_final.length > 80 ? "…" : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ShotReview({ shot }: { shot: Shot }) {
  const s = useBoardStore();
  const [expanded, setExpanded] = useState<string | null>(null);
  const takes = (s.takes[shot.id] ?? []).filter((t) => t.status === "succeeded");

  useEffect(() => {
    void s.loadShotDetail(shot.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shot.id]);

  if (takes.length === 0) return null;
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-title">
        씬 샷 #{shot.order_index + 1} — {shot.shot_type ?? "미지정"}
        {shot.approved_frame_generation_id && (
          <span className="badge green" style={{ marginLeft: 8 }}>승인됨</span>
        )}
      </div>
      <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", marginTop: 8 }}>
        {takes.map((t: GenerationJob) => {
          const url = resolveFileUrl(t.output_thumbnail_path ?? t.output_path);
          const approved = shot.approved_frame_generation_id === t.id;
          return (
            <div key={t.id} className={`card ${approved ? "selected" : ""}`} style={{ padding: 8 }}>
              {url && <img src={url} alt="" className="thumb" style={{ width: "100%", cursor: "zoom-in" }} onClick={() => setExpanded(url)} />}
              <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
                {t.model_id ?? "모델 미상"}
                {t.batch_index != null && ` · take ${t.batch_index + 1}`}
                {t.actual_cost_usd != null && ` · $${Number(t.actual_cost_usd).toFixed(4)}`}
              </div>
              <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
                {approved ? (
                  <span className="badge green">승인됨</span>
                ) : (
                  <button className="btn btn-primary" style={{ fontSize: 12 }} onClick={() => void s.approve(shot.id, t.id)}>
                    승인
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {!shot.approved_frame_generation_id && (
        <div style={{ marginTop: 8, fontSize: 13, color: "var(--muted)" }}>
          영상 생성(Phase 3)은 승인된 프레임이 있어야 가능합니다 — 한 테이크를 승인해 주세요.
        </div>
      )}
      <VariationSection shotId={shot.id} />
      {expanded && (
        <div className="modal-overlay" onClick={() => setExpanded(null)} role="dialog" aria-modal="true">
          <div className="modal" style={{ maxWidth: 720 }}>
            <img src={expanded} alt="" style={{ width: "100%" }} />
          </div>
        </div>
      )}
    </div>
  );
}

export default function ReviewPage() {
  const params = useParams();
  const projectId = params.id as string;
  const s = useBoardStore();
  const eventSeq = useWorkspaceStore((st) => st.eventSeq);
  const jobs = useWorkspaceStore((st) => st.jobs);

  useEffect(() => {
    void s.init(projectId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    const st = useBoardStore.getState();
    for (const shotId of Object.keys(st.pendingTakes)) {
      if (st.takesSettled(shotId, jobs)) st.handleTakesSettled(shotId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventSeq]);

  const reviewable = s.scenes.flatMap((sc) =>
    sc.shots.filter((sh) => sh.candidate_count > 0 || sh.approved_frame_generation_id),
  );

  return (
    <div className="page">
      <div className="page-header">
        <h1>리뷰</h1>
      </div>
      {s.loading && <p>불러오는 중…</p>}
      {reviewable.length === 0 && !s.loading && (
        <p style={{ color: "var(--muted)" }}>
          승인 대기 중인 테이크가 없습니다. 보드에서 프레임을 생성해 주세요.
        </p>
      )}
      {reviewable.map((shot) => (
        <ShotReview key={shot.id} shot={shot} />
      ))}
    </div>
  );
}
