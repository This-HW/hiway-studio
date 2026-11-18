// Backend SSE event names and payloads (GET /api/v1/events).
//
// Generated from packages/contracts/sse_events.json into lib/contracts.gen.ts
// (`make gen-types`); this module only re-exports so callers keep one import
// path. Do not hand-write event names or payload shapes here.
export { SSE_EVENT, SSE_EVENT_NAMES } from "./contracts.gen";
export type {
  CostAlertPayload,
  JobCanceledPayload,
  JobCompletedPayload,
  JobFailedPayload,
  JobProgressPayload,
  QcCompletedPayload,
  RenderCompletedPayload,
  SseEventName,
  SseEventPayloads,
} from "./contracts.gen";
