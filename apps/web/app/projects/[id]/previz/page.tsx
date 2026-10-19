// Camera previz viewer (Phase 6C, P2, ADR-0010).
// Canvas 2D only — no three.js or other heavy dependencies.
// Shows a top-down plan view and a side view of the camera path with the
// FOV frustum, plus a time scrubber. This is a *camera-move planning aid*,
// not a 3D render: there is no scene geometry or lighting.
// Coordinate system: meters, Y-up.
"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api-client";
import { ApiError } from "@/lib/errors";

type Vec3 = { x: number; y: number; z: number };
type Keyframe = {
  id: string;
  shot_id: string;
  t_sec: number;
  position: Vec3;
  target: Vec3;
  fov_deg: number;
};
type Sample = { t_sec: number; position: Vec3; target: Vec3; fov_deg: number };
type Path = { shot_id: string; keyframe_count: number; samples: Sample[] };
type Shot = { id: string; order_index: number; action?: string | null };

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "요청에 실패했습니다.";
}


function makeProjector(
  pts: Vec3[],
  w: number,
  h: number,
  view: "top" | "side"
): (p: Vec3) => [number, number] {
  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => (view === "top" ? p.z : p.y));
  const minX = Math.min(...xs, 0);
  const maxX = Math.max(...xs, 0);
  const minY = Math.min(...ys, 0);
  const maxY = Math.max(...ys, 0);
  const pad = 1.5;
  const sx = (w - 40) / Math.max(0.1, maxX - minX + pad * 2);
  const sy = (h - 40) / Math.max(0.1, maxY - minY + pad * 2);
  const s = Math.min(sx, sy);
  return (p: Vec3): [number, number] => {
    const px = p.x; // both views: x is horizontal
    const py = view === "top" ? p.z : p.y;
    const x = 20 + (px - minX + pad) * s;
    // Top view: +z goes down. Side view: +y goes up.
    const y =
      view === "top"
        ? 20 + (py - minY + pad) * s
        : h - 20 - (py - minY + pad) * s;
    return [x, y];
  };
}

function drawView(
  canvas: HTMLCanvasElement,
  view: "top" | "side",
  keyframes: Keyframe[],
  samples: Sample[],
  current: Sample | null
) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const allPts: Vec3[] = [
    ...keyframes.map((k) => k.position),
    ...keyframes.map((k) => k.target),
    ...samples.map((s) => s.position),
  ];
  if (allPts.length === 0) {
    ctx.fillStyle = "#888";
    ctx.font = "13px sans-serif";
    ctx.fillText("키프레임이 없습니다. 아래 폼에서 추가하세요.", 16, 24);
    return;
  }
  const proj = makeProjector(allPts, w, h, view);

  // Grid.
  ctx.strokeStyle = "#eee";
  ctx.lineWidth = 1;
  for (let gx = 0; gx <= w; gx += 40) {
    ctx.beginPath();
    ctx.moveTo(gx, 0);
    ctx.lineTo(gx, h);
    ctx.stroke();
  }
  for (let gy = 0; gy <= h; gy += 40) {
    ctx.beginPath();
    ctx.moveTo(0, gy);
    ctx.lineTo(w, gy);
    ctx.stroke();
  }

  // Path polyline.
  if (samples.length > 1) {
    ctx.strokeStyle = "#2563eb";
    ctx.lineWidth = 2;
    ctx.beginPath();
    samples.forEach((s, i) => {
      const [x, y] = proj(s.position);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  // Targets.
  keyframes.forEach((k) => {
    const [x, y] = proj(k.target);
    ctx.strokeStyle = "#dc2626";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x - 6, y);
    ctx.lineTo(x + 6, y);
    ctx.moveTo(x, y - 6);
    ctx.lineTo(x, y + 6);
    ctx.stroke();
  });

  // Keyframe markers.
  keyframes.forEach((k) => {
    const [x, y] = proj(k.position);
    ctx.fillStyle = "#2563eb";
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#333";
    ctx.font = "11px sans-serif";
    ctx.fillText(`${k.t_sec.toFixed(1)}s`, x + 7, y - 7);
  });

  // Current camera + FOV frustum.
  if (current) {
    const [cx, cy] = proj(current.position);
    const [tx, ty] = proj(current.target);
    const half = ((current.fov_deg / 2) * Math.PI) / 180;
    const dx = tx - cx;
    const dy = ty - cy;
    const base = Math.atan2(dy, dx);
    const len = Math.hypot(dx, dy) || 1;
    const rayLen = Math.min(len * 1.6, 400);
    ctx.fillStyle = "rgba(37, 99, 235, 0.12)";
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, rayLen, base - half, base + half);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = "#16a34a";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(tx, ty);
    ctx.stroke();
    // Camera body.
    ctx.fillStyle = "#16a34a";
    ctx.beginPath();
    ctx.arc(cx, cy, 6, 0, Math.PI * 2);
    ctx.fill();
  }

  // Axis labels.
  ctx.fillStyle = "#888";
  ctx.font = "11px sans-serif";
  ctx.fillText(view === "top" ? "평면도 (X-Z)" : "측면도 (X-Y)", 8, h - 8);
}

export default function PrevizPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;

  const [shots, setShots] = useState<Shot[]>([]);
  const [shotId, setShotId] = useState<string>("");
  const [keyframes, setKeyframes] = useState<Keyframe[]>([]);
  const [path, setPath] = useState<Path | null>(null);
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState("");

  // Add-keyframe form.
  const [tSec, setTSec] = useState("0");
  const [pos, setPos] = useState<Vec3>({ x: 0, y: 1.6, z: 5 });
  const [tgt, setTgt] = useState<Vec3>({ x: 0, y: 1.2, z: 0 });
  const [fov, setFov] = useState("40");
  const [editingId, setEditingId] = useState<string | null>(null);

  const topRef = useRef<HTMLCanvasElement>(null);
  const sideRef = useRef<HTMLCanvasElement>(null);
  const playRef = useRef<number | null>(null);

  const loadShots = useCallback(async () => {
    try {
      const scenes = await apiGet<{ id: string }[]>(`/api/v1/projects/${projectId}/scenes`);
      const all: Shot[] = [];
      for (const sc of scenes) {
        const ss = await apiGet<Shot[]>(`/api/v1/scenes/${sc.id}/shots`);
        all.push(...ss);
      }
      all.sort((a, b) => a.order_index - b.order_index);
      setShots(all);
      if (all.length > 0 && !shotId) setShotId(all[0].id);
    } catch (e) {
      setError(errMsg(e));
    }
  }, [projectId, shotId]);

  const loadKeyframes = useCallback(async () => {
    if (!shotId) return;
    try {
      const kfs = await apiGet<Keyframe[]>(`/api/v1/shots/${shotId}/camera-keyframes`);
      setKeyframes(kfs);
      const p = await apiGet<Path>(`/api/v1/shots/${shotId}/camera-path?samples=200`);
      setPath(p);
      setT(p.samples.length > 0 ? p.samples[0].t_sec : 0);
      setPlaying(false);
    } catch (e) {
      setError(errMsg(e));
    }
  }, [shotId]);

  useEffect(() => {
    loadShots();
  }, [loadShots]);
  useEffect(() => {
    loadKeyframes();
  }, [loadKeyframes]);

  const current: Sample | null = (() => {
    if (!path || path.samples.length === 0) return null;
    let best = path.samples[0];
    for (const s of path.samples) {
      if (Math.abs(s.t_sec - t) < Math.abs(best.t_sec - t)) best = s;
    }
    return best;
  })();

  useEffect(() => {
    if (topRef.current) drawView(topRef.current, "top", keyframes, path?.samples ?? [], current);
    if (sideRef.current) drawView(sideRef.current, "side", keyframes, path?.samples ?? [], current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyframes, path, t]);

  useEffect(() => {
    if (!playing || !path || path.samples.length < 2) return;
    const t1 = path.samples[path.samples.length - 1].t_sec;
    const start = performance.now();
    const from = t;
    const tick = (now: number) => {
      const el = (now - start) / 1000;
      const nt = from + el;
      if (nt >= t1) {
        setT(t1);
        setPlaying(false);
        return;
      }
      setT(nt);
      playRef.current = requestAnimationFrame(tick);
    };
    playRef.current = requestAnimationFrame(tick);
    return () => {
      if (playRef.current) cancelAnimationFrame(playRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing]);

  const tMin = path && path.samples.length > 0 ? path.samples[0].t_sec : 0;
  const tMax =
    path && path.samples.length > 0 ? path.samples[path.samples.length - 1].t_sec : 0;

  async function addKeyframe() {
    if (!shotId) return;
    try {
      setError("");
      const payload = {
        t_sec: parseFloat(tSec),
        position: pos,
        target: tgt,
        fov_deg: parseFloat(fov),
      };
      if (editingId) {
        await apiPatch(`/api/v1/camera-keyframes/${editingId}`, payload);
        setEditingId(null);
      } else {
        await apiPost(`/api/v1/shots/${shotId}/camera-keyframes`, payload);
      }
      await loadKeyframes();
    } catch (e) {
      setError(errMsg(e));
    }
  }

  function startEdit(k: Keyframe) {
    setEditingId(k.id);
    setTSec(String(k.t_sec));
    setPos(k.position);
    setTgt(k.target);
    setFov(String(k.fov_deg));
  }

  function cancelEdit() {
    setEditingId(null);
    setTSec("0");
    setPos({ x: 0, y: 1.6, z: 5 });
    setTgt({ x: 0, y: 1.2, z: 0 });
    setFov("40");
  }

  async function removeKeyframe(id: string) {
    try {
      setError("");
      await apiDelete(`/api/v1/camera-keyframes/${id}`);
      await loadKeyframes();
    } catch (e) {
      setError(errMsg(e));
    }
  }

  function numField(label: string, v: number, set: (n: number) => void) {
    return (
      <label style={{ display: "inline-block", marginRight: 8, fontSize: 13 }}>
        {label}{" "}
        <input
          className="input"
          type="number"
          step="0.1"
          value={v}
          onChange={(e) => set(parseFloat(e.target.value) || 0)}
          style={{ width: 80 }}
        />
      </label>
    );
  }

  return (
    <main style={{ padding: 24, maxWidth: 1100 }}>
      <h1 style={{ fontSize: 20, marginBottom: 4 }}>카메라 프리비즈</h1>
      <p style={{ color: "#666", fontSize: 13, marginBottom: 16 }}>
        카메라 동선을 확인하는 간이 뷰어입니다. 실제 3D 렌더링이 아니라 키프레임
        사이를 선형 보간한 경로와 FOV를 평면도·측면도에 표시합니다. 단위: 미터,
        Y-up 좌표계.
      </p>

      {error && <p style={{ color: "#c00", fontSize: 13 }}>{error}</p>}

      <div style={{ marginBottom: 12 }}>
        <label style={{ fontSize: 13 }}>
          샷{" "}
          <select className="input" value={shotId} onChange={(e) => setShotId(e.target.value)}>
            {shots.map((s) => (
              <option key={s.id} value={s.id}>
                #{s.order_index} {s.action ?? s.id.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div style={{ display: "flex", gap: 16, marginBottom: 16 }}>
        <div>
          <canvas ref={topRef} width={480} height={360} style={{ border: "1px solid #ddd" }} />
        </div>
        <div>
          <canvas ref={sideRef} width={480} height={360} style={{ border: "1px solid #ddd" }} />
        </div>
      </div>

      <div style={{ marginBottom: 16, display: "flex", alignItems: "center", gap: 8 }}>
        <button className="btn" onClick={() => setPlaying((p) => !p)} disabled={!path || path.samples.length < 2}>
          {playing ? "일시정지" : "재생"}
        </button>
        <input
          type="range"
          min={tMin}
          max={tMax}
          step={0.01}
          value={t}
          onChange={(e) => {
            setT(parseFloat(e.target.value));
            setPlaying(false);
          }}
          style={{ width: 400 }}
        />
        <span style={{ fontSize: 13 }}>
          {t.toFixed(2)}s / {tMax.toFixed(2)}s
        </span>
        {current && (
          <span style={{ fontSize: 12, color: "#666" }}>
            pos ({current.position.x.toFixed(2)}, {current.position.y.toFixed(2)},{" "}
            {current.position.z.toFixed(2)}) · fov {current.fov_deg.toFixed(1)}°
          </span>
        )}
      </div>

      <section style={{ border: "1px solid #e2e2e2", borderRadius: 8, padding: 16, marginBottom: 16 }}>
        <h2 style={{ fontSize: 15, margin: "0 0 12px" }}>키프레임 추가</h2>
        <div style={{ marginBottom: 8 }}>
          <label style={{ fontSize: 13, marginRight: 8 }}>
            t_sec{" "}
            <input className="input" type="number" step="0.1" min="0" value={tSec} onChange={(e) => setTSec(e.target.value)} style={{ width: 80 }} />
          </label>
          <label style={{ fontSize: 13 }}>
            fov°{" "}
            <input className="input" type="number" step="1" value={fov} onChange={(e) => setFov(e.target.value)} style={{ width: 80 }} />
          </label>
        </div>
        <div style={{ marginBottom: 8, fontSize: 13 }}>
          위치: {numField("x", pos.x, (n) => setPos({ ...pos, x: n }))}
          {numField("y", pos.y, (n) => setPos({ ...pos, y: n }))}
          {numField("z", pos.z, (n) => setPos({ ...pos, z: n }))}
        </div>
        <div style={{ marginBottom: 8, fontSize: 13 }}>
          타깃: {numField("x", tgt.x, (n) => setTgt({ ...tgt, x: n }))}
          {numField("y", tgt.y, (n) => setTgt({ ...tgt, y: n }))}
          {numField("z", tgt.z, (n) => setTgt({ ...tgt, z: n }))}
        </div>
        <button className="btn btn-primary" onClick={addKeyframe}>{editingId ? "수정 저장" : "추가"}</button>
        {editingId && (
          <button className="btn" onClick={cancelEdit} style={{ marginLeft: 8 }}>
            취소
          </button>
        )}
      </section>

      <section style={{ border: "1px solid #e2e2e2", borderRadius: 8, padding: 16 }}>
        <h2 style={{ fontSize: 15, margin: "0 0 12px" }}>
          키프레임 목록 ({keyframes.length})
        </h2>
        {keyframes.length === 0 && <p style={{ fontSize: 13, color: "#888" }}>없음</p>}
        <ul style={{ fontSize: 13, paddingLeft: 18 }}>
          {keyframes.map((k) => (
            <li key={k.id} style={{ marginBottom: 4 }}>
              {k.t_sec.toFixed(2)}s — pos ({k.position.x.toFixed(1)}, {k.position.y.toFixed(1)},{" "}
              {k.position.z.toFixed(1)}) → tgt ({k.target.x.toFixed(1)}, {k.target.y.toFixed(1)},{" "}
              {k.target.z.toFixed(1)}), fov {k.fov_deg.toFixed(0)}°{" "}
              <button className="btn btn-ghost" onClick={() => startEdit(k)} style={{ marginLeft: 8 }}>
                수정
              </button>
              <button className="btn btn-ghost" onClick={() => removeKeyframe(k.id)} style={{ marginLeft: 4 }}>
                삭제
              </button>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
