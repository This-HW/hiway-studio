// Single SSE connection hook for the backend event stream.
// IMPORTANT: Screens MUST use this hook only. Do NOT create a new
// EventSource per screen — share one connection per component that
// mounts this hook (extend to a shared singleton if needed in Phase 1+).
// Unmounting closes the connection.

"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE } from "./api-client";
import { SSE_EVENT_NAMES } from "./sse-events";

export type SseConnectionState = "connected" | "reconnecting" | "disconnected";

interface UseSseOptions {
  enabled?: boolean;
  /**
   * Called when the stream re-opens after a drop. Events published while
   * disconnected are not replayed (memory/Redis pub-sub backend), so the
   * caller must refetch what it shows.
   */
  onResync?: () => void;
}

/**
 * Subscribe to the backend SSE stream (GET /api/v1/events).
 * - The backend sends NAMED events (`event: job.progress` ...). EventSource
 *   only routes unnamed frames to `onmessage`, so a listener is registered
 *   for every contract event name (lib/contracts.gen.ts, generated from
 *   packages/contracts/sse_events.json). Unnamed frames still arrive as
 *   "message".
 * - onEvent receives the event name and the JSON-parsed `data:` payload.
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
  const onResyncRef = useRef(options.onResync);
  onResyncRef.current = options.onResync;

  useEffect(() => {
    if (!enabled || typeof window === "undefined") {
      return;
    }

    const source = new EventSource(`${API_BASE}/api/v1/events`);
    setState("reconnecting");
    // True after an error until the next successful open: that open is a
    // reconnect, and anything published in between was missed.
    let dropped = false;

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
    const listener = handleAny as EventListener;

    const handleOpen = () => {
      setState("connected");
      if (dropped) {
        dropped = false;
        onResyncRef.current?.();
      }
    };
    const handleError = () => {
      // EventSource auto-reconnects per the SSE spec; mark as reconnecting
      // while the browser retries. Only drop to disconnected on close().
      dropped = true;
      if (source.readyState === EventSource.CLOSED) {
        setState("disconnected");
      } else {
        setState("reconnecting");
      }
    };

    for (const name of SSE_EVENT_NAMES) {
      source.addEventListener(name, listener);
    }
    source.onmessage = handleAny;
    source.onopen = handleOpen;
    source.onerror = handleError;

    return () => {
      for (const name of SSE_EVENT_NAMES) {
        source.removeEventListener(name, listener);
      }
      source.onmessage = null;
      source.onopen = null;
      source.onerror = null;
      source.close();
      setState("disconnected");
    };
  }, [enabled]);

  return state;
}
