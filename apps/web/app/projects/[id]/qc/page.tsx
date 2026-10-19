// Project QC screen (Phase 4, F-14): pick a timeline, run QC, inspect the
// 13-check report. The timeline screen also embeds QC in its render card.
"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost } from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import type { QCReport, Timeline } from "@/lib/types";
import { QcReportView } from "@/components/QcReportView";
import { useToastStore } from "@/stores/toast";
import { useWorkspaceStore } from "@/stores/workspace";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "요청에 실패했습니다.";
}

export default function QcPage() {
  const params = useParams();
  const projectId = params.id as string;
  const toast = useToastStore();
  const eventSeq = useWorkspaceStore((s) => s.eventSeq);

  const [timelines, setTimelines] = useState<Timeline[]>([]);
  const [timelineId, setTimelineId] = useState<string>("");
  const [report, setReport] = useState<QCReport | null>(null);
  const [busy, setBusy] = useState(false);

  const loadReport = useCallback(async (tid: string) => {
    if (!tid) {
      setReport(null);
      return;
    }
    try {
      const r = await apiGet<QCReport | null>(`/api/v1/timelines/${tid}/qc-latest`);
      setReport(r);
    } catch {
      setReport(null);
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const list = await apiGet<Timeline[]>(`/api/v1/projects/${projectId}/timelines`);
        setTimelines(list);
        setTimelineId((v) => v || list[0]?.id || "");
      } catch (e) {
        toast.push(errMsg(e), { kind: "error" });
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    void loadReport(timelineId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timelineId, eventSeq]);

  const runQc = async () => {
    if (!timelineId || busy) return;
    setBusy(true);
    try {
      await apiPost(`/api/v1/timelines/${timelineId}/qc`, {});
      toast.push("QC 검사를 시작했습니다.");
      await loadReport(timelineId);
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <h1>QC · 출력 검사</h1>
      </div>
      <div className="card" style={{ marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <select
            className="input"
            style={{ fontSize: 13 }}
            value={timelineId}
            onChange={(e) => setTimelineId(e.target.value)}
          >
            {timelines.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name} · {t.status}
              </option>
            ))}
          </select>
          <button className="btn btn-primary" style={{ fontSize: 13 }} disabled={!timelineId || busy} onClick={runQc}>
            {busy ? "QC 실행 중…" : "QC 실행"}
          </button>
          <span className="small muted">
            자막·오디오·인코딩 13개 + 내용 2개 검사를 실행합니다. 렌더에는 최신 리포트가 필요합니다.
          </span>
        </div>
      </div>
      {timelines.length === 0 && <p className="small muted">타임라인이 없습니다.</p>}
      {report ? (
        <div className="card">
          <QcReportView report={report} />
        </div>
      ) : (
        timelineId && <p className="small muted">아직 QC 리포트가 없습니다. QC를 실행해 주세요.</p>
      )}
    </div>
  );
}
