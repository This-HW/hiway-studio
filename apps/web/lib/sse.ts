// Single SSE connection hook for the backend event stream.
// IMPORTANT: Screens MUST use this hook only. Do NOT create a new
// EventSource per screen — share one connection per component that
// mounts this hook (extend to a shared singleton if needed in Phase 1+).
// Unmounting closes the connection.

"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE } from "./api-client";

export type SseConnectionState = "connected" | "reconnecting" | "disconnected";

interface UseSseOptions {
  enabled?: boolean;
}

/**
 * Subscribe to the backend SSE stream (GET /api/v1/events).
 * - onEvent receives the parsed `event:` field name and the JSON-parsed `data:` payload.
 * - Returns the current connection state.
 */
export function useSse(
  onEvent: (name: string, data: unknown) => void,
  options: UseSseOptions = {}
): SseConnectionState {
  const { enabled = true } = options;
  const [state, setState] = useState<SseConnectionState>("disconnected");
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!enabled || typeof window === "undefined") {
      return;
    }

    const source = new EventSource(`${API_BASE}/api/v1/events`);
    setState("reconnecting");

    const handleAny = (ev: MessageEvent) => {
      const name = ev.type || "message";
      let data: unknown = ev.data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        // keep raw string when data is not JSON
      }
      onEventRef.current(name, data);
    };

    const handleOpen = () => setState("connected");
    const handleError = () => {
      // EventSource auto-reconnects per the SSE spec; mark as reconnecting
      // while the browser retries. Only drop to disconnected on close().
      if (source.readyState === EventSource.CLOSED) {
        setState("disconnected");
      } else {
        setState("reconnecting");
      }
    };

    // Parse named events from the `event:` field.
    // Phase 0 knows only the 7 names in lib/sse-events.ts; the hook stays
    // generic so Phase 1+ can add listeners without touching this file.
    source.onmessage = handleAny;
    source.onopen = handleOpen;
    source.onerror = handleError;

    return () => {
      source.onmessage = null;
      source.onopen = null;
      source.onerror = null;
      source.close();
      setState("disconnected");
    };
  }, [enabled]);

  return state;
}
