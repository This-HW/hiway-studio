// Project cost dashboard (Phase 4, F-15, design §9): spend summary by kind,
// monthly budget usage, budget setter, and a what-if estimate form.
"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost, apiPut } from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import type { CostEstimateItem, CostSummary } from "@/lib/types";
import { useToastStore } from "@/stores/toast";
import { useWorkspaceStore } from "@/stores/workspace";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "요청에 실패했습니다.";
}

function usd(v: number): string {
  return `$${v.toFixed(4)}`;
}

export default function CostsPage() {
  const params = useParams();
  const projectId = params.id as string;
  const toast = useToastStore();
  const eventSeq = useWorkspaceStore((s) => s.eventSeq);

  const [summary, setSummary] = useState<CostSummary | null>(null);
  const [budgetInput, setBudgetInput] = useState("");
  const [estKind, setEstKind] = useState("image");
  const [estQty, setEstQty] = useState("10");
  const [estDur, setEstDur] = useState("5");
  const [estItems, setEstItems] = useState<CostEstimateItem[]>([]);
  const [estTotal, setEstTotal] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const s = await apiGet<CostSummary>(`/api/v1/projects/${projectId}/costs/summary`);
      setSummary(s);
      setBudgetInput((v) => (v === "" && s.budget_usd != null ? String(s.budget_usd) : v));
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    }
  }, [projectId, toast]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, eventSeq]);

  const saveBudget = async () => {
    setBusy(true);
    try {
      await apiPut(`/api/v1/projects/${projectId}/budget`, {
        monthly_budget_usd: budgetInput.trim() ? Number(budgetInput) : null,
      });
      toast.push("월 예산을 저장했습니다.");
      setBudgetInput("");
      await load();
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  const estimate = async () => {
    setBusy(true);
    try {
      const res = await apiPost<{
        items: CostEstimateItem[];
        total_estimated_usd: number;
        budget_usage_after_pct: number | null;
      }>(`/api/v1/costs/estimate`, {
        project_id: projectId,
        items: [
          {
            kind: estKind,
            quantity: Number(estQty) || 1,
            duration_sec: Number(estDur) || 0,
          },
        ],
      });
      setEstItems(res.items);
      setEstTotal(res.total_estimated_usd);
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  const usage = summary?.budget_usage_pct;
  const alert = usage != null && usage >= 80;

  return (
    <div className="page">
      <div className="page-header">
        <h1>비용</h1>
      </div>

      {alert && (
        <div className="card" style={{ marginBottom: 12, borderColor: "var(--danger)" }}>
          <span style={{ color: "var(--danger)", fontSize: 13, fontWeight: 600 }}>
            월 예산의 {usage?.toFixed(1)}%를 사용했습니다.
            {usage != null && usage >= 100 ? " 새 생성 작업은 차단됩니다." : " 100%에 도달하면 새 생성이 차단됩니다."}
          </span>
        </div>
      )}

      <div className="card" style={{ marginBottom: 12 }}>
        <div className="card-title">이번 달 지출</div>
        {!summary ? (
          <p className="small muted">불러오는 중…</p>
        ) : (
          <>
            <div style={{ fontSize: 22, fontWeight: 700, marginBottom: 6 }}>
              {usd(summary.total_actual_usd)}
              {summary.budget_usd != null && (
                <span className="small muted" style={{ marginLeft: 8 }}>
                  / {usd(summary.budget_usd)} ({summary.budget_usage_pct?.toFixed(1)}%)
                </span>
              )}
            </div>
            {summary.budget_usd != null && (
              <div style={{ height: 8, background: "var(--border)", borderRadius: 4, marginBottom: 10 }}>
                <div
                  style={{
                    height: 8,
                    borderRadius: 4,
                    width: `${Math.min(summary.budget_usage_pct ?? 0, 100)}%`,
                    background: alert ? "var(--danger)" : "var(--primary)",
                  }}
                />
              </div>
            )}
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                  <th style={{ padding: "4px 6px" }}>종류</th>
                  <th style={{ padding: "4px 6px" }}>실제 지출</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(summary.by_kind).map(([k, v]) => (
                  <tr key={k} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: "4px 6px" }}>{k}</td>
                    <td style={{ padding: "4px 6px" }}>{usd(v)}</td>
                  </tr>
                ))}
                {Object.keys(summary.by_kind).length === 0 && (
                  <tr>
                    <td colSpan={2} className="small muted" style={{ padding: "4px 6px" }}>
                      아직 지출이 없습니다.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
            <p className="small muted" style={{ marginTop: 6 }}>
              예상 합계(진행 중 포함) {usd(summary.total_estimated_usd)} · 집계 기간{" "}
              {summary.period_start.slice(0, 10)} ~ {summary.period_end.slice(0, 10)} (UTC)
            </p>
          </>
        )}
      </div>

      <div className="card" style={{ marginBottom: 12 }}>
        <div className="card-title">월 예산</div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            className="input"
            style={{ width: 140, fontSize: 13 }}
            type="number"
            min={0}
            step={1}
            value={budgetInput}
            onChange={(e) => setBudgetInput(e.target.value)}
            placeholder={summary?.budget_usd != null ? String(summary.budget_usd) : "미설정"}
          />
          <button className="btn" style={{ fontSize: 13 }} disabled={busy} onClick={saveBudget}>
            저장
          </button>
          <span className="small muted">비우면 예산 제한을 해제합니다. 80% 도달 시 알림, 100%에서 새 생성이 차단됩니다.</span>
        </div>
      </div>

      <div className="card">
        <div className="card-title">비용 미리보기</div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <select className="input" style={{ fontSize: 13 }} value={estKind} onChange={(e) => setEstKind(e.target.value)}>
            <option value="image">이미지</option>
            <option value="video">영상</option>
            <option value="upscale">업스케일</option>
            <option value="outpaint">아웃페인트</option>
          </select>
          <label className="small muted">
            수량{" "}
            <input className="input" style={{ width: 70, fontSize: 13 }} type="number" min={1} value={estQty} onChange={(e) => setEstQty(e.target.value)} />
          </label>
          {estKind === "video" && (
            <label className="small muted">
              길이(초){" "}
              <input className="input" style={{ width: 70, fontSize: 13 }} type="number" min={1} value={estDur} onChange={(e) => setEstDur(e.target.value)} />
            </label>
          )}
          <button className="btn" style={{ fontSize: 13 }} disabled={busy} onClick={estimate}>
            계산
          </button>
          {estTotal != null && (
            <span style={{ fontSize: 14, fontWeight: 600 }}>예상 {usd(estTotal)}</span>
          )}
        </div>
        {estItems.length > 0 && (
          <p className="small muted" style={{ marginTop: 6 }}>
            {estItems.map((i) => `${i.kind} × ${i.quantity} (${i.model_id})`).join(", ")}
          </p>
        )}
      </div>
    </div>
  );
}
