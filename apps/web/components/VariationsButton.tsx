// Prompt variation launcher (P1-3). Standalone: POST
// /shots/{shotId}/frame/variations with an Idempotency-Key and reports the
// accepted jobs + per-variation seeds back to the caller.
"use client";

import { useState } from "react";
import { apiPost } from "@/lib/api-client";

export interface VariationJob {
  job_id: string;
  prompt_final: string;
  seed: number;
}

export interface VariationsAccepted {
  job_id: string;
  variation_job_ids: string[];
  variations: VariationJob[];
  count: number;
  variation_axis: string;
  model_id: string;
  estimated_cost_usd: string;
}

interface Props {
  shotId: string;
  onAccepted?: (result: VariationsAccepted) => void;
}

export function VariationsButton({ shotId, onAccepted }: Props) {
  const [count, setCount] = useState(3);
  const [axis, setAxis] = useState<"prompt" | "seed">("prompt");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [last, setLast] = useState<VariationsAccepted | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const result = await apiPost<VariationsAccepted>(
        `/api/v1/shots/${shotId}/frame/variations`,
        { count, variation_axis: axis },
        { idempotencyKey: `${shotId}:${axis}:${count}:${Date.now()}` },
      );
      setLast(result);
      onAccepted?.(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : "배리에이션 생성에 실패했습니다.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="variations-box">
      <div className="variations-controls">
        <label>
          개수
          <select value={count} onChange={(e) => setCount(Number(e.target.value))}>
            {[2, 3, 4].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label>
          변형 축
          <select
            value={axis}
            onChange={(e) => setAxis(e.target.value as "prompt" | "seed")}
          >
            <option value="prompt">프롬프트</option>
            <option value="seed">시드</option>
          </select>
        </label>
        <button type="button" onClick={run} disabled={busy}>
          {busy ? "생성 중…" : "배리에이션 생성"}
        </button>
      </div>
      {error && <p className="error-text">{error}</p>}
      {last && (
        <p className="hint">
          {last.count}개 잡 접수 · 예상 비용 ${last.estimated_cost_usd} · 시드{" "}
          {last.variations.map((v) => v.seed).join(", ")}
        </p>
      )}
    </div>
  );
}
