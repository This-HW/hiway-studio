// Header job badge: active job count, progress dropdown, SSE status dot.
// All job state comes from the workspace store (fed by the layout's useSse).
"use client";

import { useState } from "react";
import { isJobActionable, isRetryableFailure, jobLabel } from "@/lib/jobs-api";
import type { SseConnectionState } from "@/lib/sse";
import { useWorkspaceStore } from "@/stores/workspace";

export function SseDot({ state }: { state: SseConnectionState }) {
  const label =
    state === "connected"
      ? "연결됨"
      : state === "reconnecting"
      ? "재연결 중"
      : "끊김";
  return (
    <span className="row" title={`SSE ${label}`}>
      <span
        className={
          state === "connected"
            ? "sse-dot"
            : state === "reconnecting"
            ? "sse-dot reconnecting"
            : "sse-dot disconnected"
        }
      />
      <span className="small muted">{label}</span>
    </span>
  );
}

export function JobBadge() {
  const [open, setOpen] = useState(false);
  const jobs = useWorkspaceStore((s) => s.jobs);
  const sseState = useWorkspaceStore((s) => s.sseState);
  const retryJob = useWorkspaceStore((s) => s.retryJob);
  const cancelJob = useWorkspaceStore((s) => s.cancelJob);
  // Job ids with a retry/cancel request in flight; an accepted cancel stays
  // disabled until job.canceled (or a durable read) moves the row.
  const [acting, setActing] = useState<Record<string, boolean>>({});
  const act = (id: string, action: () => Promise<boolean>, keepOnSuccess: boolean) => {
    setActing((a) => ({ ...a, [id]: true }));
    void action().then((ok) => {
      if (!ok || !keepOnSuccess) setActing((a) => ({ ...a, [id]: false }));
    });
  };

  const active = Object.values(jobs).filter(
    (j) => j.status === "queued" || j.status === "running"
  );
  const recent = Object.values(jobs)
    .filter((j) => j.status === "completed" || j.status === "failed")
    .slice(-5)
    .reverse();

  return (
    <div className="row">
      <SseDot state={sseState} />
      <div className="job-badge">
        <button
          className="btn"
          onClick={() => setOpen((v) => !v)}
          aria-label={`진행 중인 작업 ${active.length}개`}
        >
          작업 {active.length > 0 ? <strong>{active.length}</strong> : "0"}
        </button>
        {open && (
          <div className="job-dropdown">
            {active.length === 0 && recent.length === 0 && (
              <p className="small muted" style={{ padding: "8px 10px" }}>
                진행 중인 작업이 없습니다.
              </p>
            )}
            {active.map((j) => (
              <div key={j.job_id} className="job-row">
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="small" style={{ fontWeight: 600 }}>
                    {jobLabel(j)}
                  </span>
                  <span className="small muted">{j.progress_pct}%</span>
                </div>
                <div className="small muted">{j.stage}</div>
                <div className="progress-bar">
                  <div style={{ width: `${j.progress_pct}%` }} />
                </div>
                {j.eta_sec !== undefined && (
                  <div className="small muted">예상 {j.eta_sec}초 남음</div>
                )}
                {isJobActionable(j.job_type) && (
                  <button
                    className="btn btn-ghost small"
                    disabled={acting[j.job_id]}
                    aria-label={`${jobLabel(j)} 취소`}
                    onClick={() => act(j.job_id, () => cancelJob(j), true)}
                  >
                    {acting[j.job_id] ? "취소 요청됨" : "취소"}
                  </button>
                )}
              </div>
            ))}
            {recent.length > 0 && (
              <>
                <div
                  className="small muted"
                  style={{ padding: "8px 10px 4px" }}
                >
                  최근 완료/실패
                </div>
                {recent.map((j) => (
                  <div key={j.job_id} className="job-row">
                    <span
                      className={
                        j.status === "completed" ? "badge green" : "badge yellow"
                      }
                    >
                      {j.status === "completed" ? "완료" : "실패"}
                    </span>{" "}
                    <span className="small">{jobLabel(j)}</span>
                    {j.error_code && (
                      <div className="small muted">{j.error_code}</div>
                    )}
                    {j.status === "failed" &&
                      !j.verifying &&
                      isJobActionable(j.job_type) &&
                      isRetryableFailure(j.error_code) && (
                        <button
                          className="btn small"
                          disabled={acting[j.job_id]}
                          aria-label={`${jobLabel(j)} 재시도`}
                          onClick={() => act(j.job_id, () => retryJob(j), false)}
                        >
                          재시도
                        </button>
                      )}
                  </div>
                ))}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
