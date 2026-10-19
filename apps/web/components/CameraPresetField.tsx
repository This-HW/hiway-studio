// Camera preset + 180-degree side selector (P1-2). Standalone controlled
// field: the parent owns the shot patch state and persists it via PATCH
// /shots/{id}; this component only renders the controls.
"use client";

import { useEffect, useState } from "react";
import { apiGet } from "@/lib/api-client";

export interface CameraPreset {
  id: string;
  name: string;
  description: string | null;
  lens: string | null;
  movement: string | null;
  angle: string | null;
  settings: Record<string, unknown>;
  is_builtin: boolean;
}

export type CameraSide = "left" | "right" | "center" | null;

const SIDES: { key: Exclude<CameraSide, null>; label: string }[] = [
  { key: "left", label: "좌" },
  { key: "center", label: "중앙" },
  { key: "right", label: "우" },
];

interface Props {
  presetId: string | null;
  cameraSide: CameraSide;
  onChange: (patch: { camera_preset_id: string | null; camera_side: CameraSide }) => void;
}

export function CameraPresetField({ presetId, cameraSide, onChange }: Props) {
  const [presets, setPresets] = useState<CameraPreset[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<CameraPreset[]>("/api/v1/camera-presets")
      .then(setPresets)
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : "프리셋을 불러오지 못했습니다."),
      );
  }, []);

  const selected = presets.find((p) => p.id === presetId);

  return (
    <div className="camera-preset-field">
      <label>
        카메라 프리셋
        <select
          value={presetId ?? ""}
          onChange={(e) =>
            onChange({ camera_preset_id: e.target.value || null, camera_side: cameraSide })
          }
        >
          <option value="">직접 설정</option>
          {presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
              {p.lens ? ` · ${p.lens}` : ""}
            </option>
          ))}
        </select>
      </label>
      {selected?.description && <p className="hint">{selected.description}</p>}
      <div className="camera-side-row">
        <span>화면 축 (180도 규칙)</span>
        {SIDES.map((s) => (
          <button
            key={s.key}
            type="button"
            className={cameraSide === s.key ? "active" : ""}
            onClick={() =>
              onChange({
                camera_preset_id: presetId,
                camera_side: cameraSide === s.key ? null : s.key,
              })
            }
          >
            {s.label}
          </button>
        ))}
      </div>
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}
