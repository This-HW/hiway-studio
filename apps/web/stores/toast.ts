// Shared toast queue (Korean messages, optional action link).
"use client";

import { create } from "zustand";

export interface ToastItem {
  id: number;
  message: string;
  kind: "info" | "error";
  actionLabel?: string;
  onAction?: () => void;
}

interface ToastState {
  toasts: ToastItem[];
  push: (
    message: string,
    opts?: { kind?: "info" | "error"; actionLabel?: string; onAction?: () => void }
  ) => void;
  dismiss: (id: number) => void;
}

let nextId = 1;

export const useToastStore = create<ToastState>((set) => ({
  toasts: [],
  push: (message, opts) => {
    const id = nextId++;
    const toast: ToastItem = {
      id,
      message,
      kind: opts?.kind ?? "info",
      actionLabel: opts?.actionLabel,
      onAction: opts?.onAction,
    };
    set((s) => ({ toasts: [...s.toasts, toast] }));
    // Auto-dismiss after 6s unless it has an action.
    if (!opts?.onAction) {
      window.setTimeout(() => {
        set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
      }, 6000);
    }
  },
  dismiss: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

export function toast(
  message: string,
  opts?: { kind?: "info" | "error"; actionLabel?: string; onAction?: () => void }
): void {
  useToastStore.getState().push(message, opts);
}
