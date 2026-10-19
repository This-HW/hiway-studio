"use client";

import { useToastStore } from "@/stores/toast";

export function ToastStack() {
  const toasts = useToastStore((s) => s.toasts);
  const dismiss = useToastStore((s) => s.dismiss);

  return (
    <div className="toast-stack" aria-live="polite">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={t.kind === "error" ? "toast error" : "toast"}
          onClick={() => dismiss(t.id)}
        >
          <span>{t.message}</span>
          {t.actionLabel && t.onAction && (
            <button
              className="toast-action"
              onClick={(e) => {
                e.stopPropagation();
                dismiss(t.id);
                t.onAction?.();
              }}
            >
              {t.actionLabel}
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
