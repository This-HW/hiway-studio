// Job retry / cancel (services/api/app/routers/generations.py):
//   POST /api/v1/jobs/{id}/retry  -> 202 { job_id, estimated_cost_usd }  (failed jobs only)
//   POST /api/v1/jobs/{id}/cancel -> 200 { job_id }                      (queued/running only)
// Both accept a generation id (a batch parent re-queues/cancels its children)
// or an audio (TTS) id. Wrong state answers 400 VALIDATION_ERROR.
import { apiPost } from "@/lib/api-client";
import { ERROR_SPEC, type ErrorCode } from "@/lib/contracts.gen";
import { ApiError } from "@/lib/errors";
import { koreanErrorMessage } from "@/lib/format";
import type { JobAccepted } from "@/lib/types";

export function retryJob(jobId: string): Promise<JobAccepted> {
  return apiPost<JobAccepted>(`/api/v1/jobs/${jobId}/retry`, {});
}

export function cancelJob(jobId: string): Promise<{ job_id: string }> {
  return apiPost<{ job_id: string }>(`/api/v1/jobs/${jobId}/cancel`, {});
}

/**
 * "재시도" is offered only for a failure whose error code the errors contract
 * marks retryable (P0_SCREENS: `retryable=true`). Unknown or missing codes
 * are not offered: retrying a deterministic failure would just fail again.
 */
export function isRetryableFailure(errorCode: string | null | undefined): boolean {
  if (!errorCode || !(errorCode in ERROR_SPEC)) return false;
  return ERROR_SPEC[errorCode as ErrorCode].retryable;
}

/** Jobs that retry/cancel can address (a render is a timeline, not a job). */
export function isJobActionable(jobType: string | null | undefined): boolean {
  return jobType !== "render";
}

/**
 * Row label that tells same-type jobs apart in the header job list: the job
 * type plus a short id (tracked jobs carry no shot/take reference), e.g.
 * "text_to_image #1a2b3c4d" (review B10-M5).
 */
export function jobLabel(job: { job_id: string; job_type?: string | null }): string {
  return `${job.job_type || "작업"} #${job.job_id.slice(0, 8)}`;
}

/** Korean toast text for a rejected retry/cancel request. */
export function jobActionErrorMessage(e: unknown, action: "retry" | "cancel"): string {
  const fallback = action === "retry" ? "재시도에 실패했습니다." : "취소에 실패했습니다.";
  if (!(e instanceof ApiError)) return fallback;
  if (e.code === "NOT_FOUND") return "작업을 찾을 수 없습니다.";
  if (e.code === "VALIDATION_ERROR" || e.status === 409) {
    return action === "retry"
      ? "실패한 작업만 재시도할 수 있습니다. 작업 상태를 다시 불러옵니다."
      : "대기 중이거나 실행 중인 작업만 취소할 수 있습니다. 작업 상태를 다시 불러옵니다.";
  }
  return koreanErrorMessage(e.code, fallback);
}
