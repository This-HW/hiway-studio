// Backend SSE event names (GET /api/v1/events).
//
// NOTE: The canonical source is packages/contracts; Phase 1+ will generate
// these constants with `make gen-types`. Keep this file in sync with the
// contracts package until codegen lands.
export const SSE_EVENT = {
  JOB_PROGRESS: "job.progress",
  JOB_COMPLETED: "job.completed",
  JOB_FAILED: "job.failed",
  JOB_CANCELED: "job.canceled",
  QC_COMPLETED: "qc.completed",
  RENDER_COMPLETED: "render.completed",
  COST_ALERT: "cost.alert",
} as const;

export type SseEventName = (typeof SSE_EVENT)[keyof typeof SSE_EVENT];
