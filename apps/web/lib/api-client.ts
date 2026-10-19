// Central API client for the backend.
// IMPORTANT: All API calls in this web app MUST go through this module.
// Do NOT call fetch() directly from components or hooks.

import { ApiError } from "./errors";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Error body shape returned by the backend (§8.4):
// { "code", "message", "retryable", "retry_after_sec", "details" }
async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      // non-JSON error body; fall through to generic handling
    }
    throw ApiError.fromResponse(res.status, body);
  }
  // 204 No Content
  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "GET",
    headers: { Accept: "application/json" },
  });
  return handleResponse<T>(res);
}

export async function apiPost<T>(
  path: string,
  body: unknown,
  opts: { idempotencyKey?: string } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };
  // Generation POSTs accept an optional Idempotency-Key; the backend replays
  // the original 202 instead of creating new jobs when the key repeats
  // inside the window (design §12.2).
  if (opts.idempotencyKey) {
    headers["Idempotency-Key"] = opts.idempotencyKey;
  }
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  return handleResponse<T>(res);
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  return handleResponse<T>(res);
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  return handleResponse<T>(res);
}

export async function apiDelete<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "DELETE",
    headers: { Accept: "application/json" },
  });
  return handleResponse<T>(res);
}

/**
 * Multipart upload. `form` carries the file and extra fields;
 * Content-Type is left to the browser so the boundary is set correctly.
 */
export async function apiUpload<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { Accept: "application/json" },
    body: form,
  });
  return handleResponse<T>(res);
}
