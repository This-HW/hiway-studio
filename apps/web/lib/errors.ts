// Structured API error matching the backend §8.4 error format:
// { "code", "message", "retryable", "retry_after_sec", "details" }

interface ApiErrorInit {
  code: string;
  message: string;
  retryable: boolean;
  retryAfterSec?: number;
  details?: unknown;
  status: number;
}

export class ApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly retryAfterSec?: number;
  readonly details?: unknown;
  readonly status: number;

  constructor(init: ApiErrorInit) {
    super(init.message);
    this.name = "ApiError";
    this.code = init.code;
    this.retryable = init.retryable;
    this.retryAfterSec = init.retryAfterSec;
    this.details = init.details;
    this.status = init.status;
  }

  static fromResponse(status: number, body: unknown): ApiError {
    if (body !== null && typeof body === "object" && !Array.isArray(body)) {
      const b = body as Record<string, unknown>;
      if (typeof b.code === "string" && typeof b.message === "string") {
        return new ApiError({
          code: b.code,
          message: b.message,
          retryable: b.retryable === true,
          retryAfterSec:
            typeof b.retry_after_sec === "number" ? b.retry_after_sec : undefined,
          details: b.details,
          status,
        });
      }
    }
    return new ApiError({
      code: `HTTP_${status}`,
      message: `Request failed with status ${status}`,
      retryable: status >= 500 || status === 429,
      status,
    });
  }
}
