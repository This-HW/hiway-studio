"use client";

import { useState } from "react";
import type { ReactNode } from "react";
import { Modal } from "./Modal";

interface ConfirmModalProps {
  title: string;
  message: string;
  /** When set, the user must type this exact text to enable the confirm button. */
  requireTitleInput?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
  children?: ReactNode;
}

export function ConfirmModal({
  title,
  message,
  requireTitleInput,
  confirmLabel = "확인",
  cancelLabel = "취소",
  danger = false,
  busy = false,
  onConfirm,
  onClose,
  children,
}: ConfirmModalProps) {
  const [input, setInput] = useState("");
  const canConfirm =
    !busy &&
    (requireTitleInput === undefined || input.trim() === requireTitleInput);

  return (
    <Modal title={title} onClose={onClose}>
      <p style={{ whiteSpace: "pre-wrap" }}>{message}</p>
      {children}
      {requireTitleInput !== undefined && (
        <div className="field">
          <label>
            확인을 위해 아래 제목을 그대로 입력하세요: {requireTitleInput}
          </label>
          <input
            className="input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={requireTitleInput}
            disabled={busy}
          />
        </div>
      )}
      <div className="modal-actions">
        <button className="btn" onClick={onClose} disabled={busy}>
          {cancelLabel}
        </button>
        <button
          className={danger ? "btn btn-danger" : "btn btn-primary"}
          onClick={onConfirm}
          disabled={!canConfirm}
        >
          {busy ? "처리 중…" : confirmLabel}
        </button>
      </div>
    </Modal>
  );
}
