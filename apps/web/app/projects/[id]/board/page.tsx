// Board: scene/shot storyboard with frame generation and approval (P0-9).
"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { ConfirmModal } from "@/components/ConfirmModal";
import { Modal } from "@/components/Modal";
import { apiGet, apiPost } from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import { resolveFileUrl } from "@/lib/files";
import type { GenerationJob, Scene, Shot } from "@/lib/types";
import { SHOT_REFERENCE_PURPOSES } from "@/lib/types";
import { useBoardStore } from "@/stores/board";
import { useCharactersStore } from "@/stores/characters";
import { toast } from "@/stores/toast";
import { useWorkspaceStore } from "@/stores/workspace";

function statusBadge(status: string) {
  const map: Record<string, [string, string]> = {
    draft: ["gray", "초안"],
    frame_generating: ["yellow", "프레임 생성 중"],
    frame_done: ["", "프레임 완료"],
    frame_approved: ["green", "승인됨"],
    failed: ["yellow", "실패"],
  };
  const [cls, label] = map[status] ?? ["gray", status];
  return <span className={`badge ${cls}`}>{label}</span>;
}

// P1-2 카메라 프리셋, P1-1 멀티샷 (§05C-1, §05C-2)
type CameraPreset = {
  id: string;
  name: string;
  description: string | null;
  lens: string | null;
  movement: string | null;
  angle: string | null;
};

type MultishotFrame = {
  id: string;
  coverage_type: string;
  status: string;
  approved: boolean;
};

type MultishotSet = {
  id: string;
  shot_id: string;
  status: string;
  frames: MultishotFrame[];
};

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "요청에 실패했습니다.";
}

function ShotCard({ shot, sceneId }: { shot: Shot; sceneId: string }) {
  const s = useBoardStore();
  const thumb = resolveFileUrl(shot.thumbnail);
  const busy = s.busy[shot.id];
  const pending = (s.pendingTakes[shot.id] ?? []).length > 0;
  return (
    <div
      className="card clickable"
      onClick={() => s.selectShot(shot.id)}
      style={{ padding: 8 }}
    >
      {thumb ? (
        <img src={thumb} alt="" className="thumb" style={{ width: "100%" }} />
      ) : (
        <div className="placeholder-box" style={{ width: "100%", aspectRatio: "16/9" }}>
          {busy || pending ? "생성 중…" : "프레임 없음"}
        </div>
      )}
      <div style={{ marginTop: 6, display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
        {statusBadge(shot.status)}
        {shot.frame_stale && <span className="badge yellow">프롬프트 변경됨</span>}
        {shot.candidate_count > 0 && (
          <span className="badge gray">테이크 {shot.candidate_count}</span>
        )}
      </div>
      <div className="card-title" style={{ fontSize: 13, marginTop: 4 }}>
        #{shot.order_index + 1} {shot.shot_type ?? ""}
      </div>
      {shot.action && (
        <div style={{ fontSize: 12, color: "var(--muted)" }}>
          {shot.action.slice(0, 60)}
        </div>
      )}
      <div style={{ marginTop: 6, display: "flex", gap: 4 }}>
        <button
          className="btn btn-ghost"
          style={{ padding: "2px 8px", fontSize: 12 }}
          onClick={(e) => {
            e.stopPropagation();
            void s.moveShot(sceneId, shot.id, -1);
          }}
        >
          ←
        </button>
        <button
          className="btn btn-ghost"
          style={{ padding: "2px 8px", fontSize: 12 }}
          onClick={(e) => {
            e.stopPropagation();
            void s.moveShot(sceneId, shot.id, 1);
          }}
        >
          →
        </button>
      </div>
    </div>
  );
}

function TakeGrid({ shotId }: { shotId: string }) {
  const s = useBoardStore();
  const takes = s.takes[shotId] ?? [];
  const shot = s.scenes.flatMap((sc) => sc.shots).find((x) => x.id === shotId);
  const [inpaintFor, setInpaintFor] = useState<string | null>(null);
  const [inpaintPrompt, setInpaintPrompt] = useState("");
  const [inpaintMask, setInpaintMask] = useState("");
  const [outpaintFor, setOutpaintFor] = useState<string | null>(null);
  const [outpaintPrompt, setOutpaintPrompt] = useState("");

  if (takes.length === 0) return <p style={{ color: "var(--muted)" }}>테이크가 없습니다.</p>;
  return (
    <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))" }}>
      {takes.map((t: GenerationJob) => {
        const url = resolveFileUrl(t.output_thumbnail_path ?? t.output_path);
        const approved = shot?.approved_frame_generation_id === t.id;
        return (
          <div key={t.id} className={`card ${approved ? "selected" : ""}`} style={{ padding: 8 }}>
            {url ? (
              <img src={url} alt="" className="thumb" style={{ width: "100%" }} />
            ) : (
              <div className="placeholder-box" style={{ width: "100%", aspectRatio: "1/1" }}>
                {t.status}
              </div>
            )}
            <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
              {t.job_type} · {t.status}
              {t.batch_index != null && ` · take ${t.batch_index + 1}`}
            </div>
            <div style={{ display: "flex", gap: 4, marginTop: 6, flexWrap: "wrap" }}>
              {t.status === "succeeded" && !approved && (
                <button className="btn btn-primary" style={{ fontSize: 12, padding: "2px 8px" }} onClick={() => void s.approve(shotId, t.id)}>
                  승인
                </button>
              )}
              {approved && <span className="badge green">승인됨</span>}
              {t.status === "succeeded" && (
                <>
                  <button className="btn btn-ghost" style={{ fontSize: 12, padding: "2px 8px" }} onClick={() => void s.upscale(shotId, t.id)}>
                    업스케일
                  </button>
                  <button className="btn btn-ghost" style={{ fontSize: 12, padding: "2px 8px" }} onClick={() => setInpaintFor(inpaintFor === t.id ? null : t.id)}>
                    인페인트
                  </button>
                  <button className="btn btn-ghost" style={{ fontSize: 12, padding: "2px 8px" }} onClick={() => setOutpaintFor(outpaintFor === t.id ? null : t.id)}>
                    아웃페인트
                  </button>
                </>
              )}
            </div>
            {inpaintFor === t.id && (
              <div style={{ marginTop: 6, display: "grid", gap: 4 }}>
                <input className="input" placeholder="마스크 경로 (projects/…)" value={inpaintMask} onChange={(e) => setInpaintMask(e.target.value)} />
                <input className="input" placeholder="수정 프롬프트" value={inpaintPrompt} onChange={(e) => setInpaintPrompt(e.target.value)} />
                <button
                  className="btn btn-primary"
                  disabled={!inpaintMask || !inpaintPrompt}
                  onClick={() => {
                    void s.inpaint(shotId, t.id, inpaintMask, inpaintPrompt);
                    setInpaintFor(null);
                  }}
                >
                  인페인트 실행
                </button>
              </div>
            )}
            {outpaintFor === t.id && (
              <div style={{ marginTop: 6, display: "grid", gap: 4 }}>
                <input className="input" placeholder="아웃페인트 프롬프트" value={outpaintPrompt} onChange={(e) => setOutpaintPrompt(e.target.value)} />
                <button
                  className="btn btn-primary"
                  disabled={!outpaintPrompt}
                  onClick={() => {
                    void s.outpaint(shotId, t.id, outpaintPrompt);
                    setOutpaintFor(null);
                  }}
                >
                  아웃페인트 실행
                </button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ShotModal() {
  const s = useBoardStore();
  const shotId = s.selectedShotId;
  const shot = s.scenes.flatMap((sc) => sc.shots).find((x) => x.id === shotId);
  const chars = useCharactersStore();
  const params = useParams();
  const projectId = params.id as string;
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [batchSize, setBatchSize] = useState(2);
  const [useContinuity, setUseContinuity] = useState(false);
  const [refCharacter, setRefCharacter] = useState("");
  const [refPurpose, setRefPurpose] = useState<string>("primary");
  const [form, setForm] = useState<Partial<Shot>>({});
  const [presets, setPresets] = useState<CameraPreset[]>([]);
  const [presetId, setPresetId] = useState("");
  const [msBusy, setMsBusy] = useState(false);
  const [msSet, setMsSet] = useState<MultishotSet | null>(null);
  const [msError, setMsError] = useState("");
  const [msCost, setMsCost] = useState<string | null>(null);

  useEffect(() => {
    if (shot) {
      setForm({
        shot_type: shot.shot_type,
        camera_angle: shot.camera_angle,
        movement: shot.movement,
        duration_sec: shot.duration_sec,
        action: shot.action,
        dialogue: shot.dialogue,
        sfx_notes: shot.sfx_notes,
        intended_text: shot.intended_text,
      });
      void chars.init(projectId);
      setPresetId("");
      setMsSet(null);
      setMsError("");
      setMsCost(null);
      apiGet<CameraPreset[]>("/api/v1/camera-presets")
        .then(setPresets)
        .catch(() => setPresets([]));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shotId]);

  if (!shot || !shotId) return null;
  const refs = s.references[shotId] ?? [];
  const cont = s.continuity[shotId];
  const busy = s.busy[shotId];

  const save = () => {
    const patch: Partial<Shot> = {};
    for (const [k, v] of Object.entries(form)) {
      if (v !== (shot as unknown as Record<string, unknown>)[k]) {
        (patch as Record<string, unknown>)[k] = v;
      }
    }
    if (Object.keys(patch).length > 0) void s.updateShot(shotId, patch);
    else toast("변경 사항이 없습니다.");
  };

  const applyPreset = (id: string) => {
    setPresetId(id);
    const p = presets.find((x) => x.id === id);
    if (!p) return;
    setForm((f) => ({
      ...f,
      camera_angle: p.angle ?? f.camera_angle,
      movement: p.movement ?? f.movement,
    }));
    toast(`프리셋 "${p.name}" 적용 — 저장 버튼을 눌러 확정하세요.`);
  };

  const runMultishot = async () => {
    setMsBusy(true);
    setMsError("");
    try {
      const accepted = await apiPost<{ multishot_set_id: string; estimated_cost_usd: string }>(
        `/api/v1/shots/${shotId}/multishot`,
        {},
        { idempotencyKey: `multishot-${shotId}-${Date.now()}` },
      );
      setMsCost(accepted.estimated_cost_usd);
      const set = await apiGet<MultishotSet>(`/api/v1/multishot-sets/${accepted.multishot_set_id}`);
      setMsSet(set);
    } catch (e) {
      setMsError(errMsg(e));
    } finally {
      setMsBusy(false);
    }
  };

  const refreshMsSet = async () => {
    if (!msSet) return;
    try {
      setMsSet(await apiGet<MultishotSet>(`/api/v1/multishot-sets/${msSet.id}`));
    } catch (e) {
      setMsError(errMsg(e));
    }
  };

  return (
    <Modal wide title={`샷 #${shot.order_index + 1} — ${shot.shot_type ?? "미지정"}`} onClose={() => s.selectShot(null)}>
      <div style={{ display: "grid", gap: 12 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          {statusBadge(shot.status)}
          {shot.frame_stale && <span className="badge yellow">프롬프트가 바뀌어 프레임이 오래됨</span>}
          {shot.approved_at && <span className="badge green">승인 {new Date(shot.approved_at).toLocaleString()}</span>}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <label>샷 타입<input className="input" value={form.shot_type ?? ""} onChange={(e) => setForm({ ...form, shot_type: e.target.value || null })} /></label>
          <label>카메라 앵글<input className="input" value={form.camera_angle ?? ""} onChange={(e) => setForm({ ...form, camera_angle: e.target.value || null })} /></label>
          <label>무브먼트<input className="input" value={form.movement ?? ""} onChange={(e) => setForm({ ...form, movement: e.target.value || null })} /></label>
          <label>길이(초)<input className="input" type="number" min={0.1} step={0.1} value={form.duration_sec ?? 5} onChange={(e) => setForm({ ...form, duration_sec: Number(e.target.value) })} /></label>
        </div>
        <label>카메라 프리셋 (P1-2)
          <select className="input" value={presetId} onChange={(e) => applyPreset(e.target.value)} disabled={presets.length === 0}>
            <option value="">프리셋 선택 — 앵글·무브먼트에 적용</option>
            {presets.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}{p.lens ? ` · ${p.lens}` : ""}{p.description ? ` — ${p.description}` : ""}
              </option>
            ))}
          </select>
        </label>
        <label>액션<textarea className="input" rows={2} value={form.action ?? ""} onChange={(e) => setForm({ ...form, action: e.target.value || null })} /></label>
        <label>대사<textarea className="input" rows={2} value={form.dialogue ?? ""} onChange={(e) => setForm({ ...form, dialogue: e.target.value || null })} /></label>
        <label>SFX 메모<input className="input" value={form.sfx_notes ?? ""} onChange={(e) => setForm({ ...form, sfx_notes: e.target.value || null })} /></label>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-primary" onClick={save}>저장</button>
          <button className="btn btn-ghost" onClick={() => void s.duplicateShot(shotId)}>복제</button>
          <button className="btn btn-danger" onClick={() => setConfirmDelete(true)}>삭제</button>
        </div>

        <h3>레퍼런스</h3>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {refs.map((r) => {
            const url = resolveFileUrl(r.target_image);
            return (
              <div key={r.id} className="card" style={{ padding: 6, display: "flex", gap: 6, alignItems: "center" }}>
                {url && <img src={url} alt="" className="thumb" style={{ width: 40, height: 40 }} />}
                <div style={{ fontSize: 12 }}>
                  <div>{r.target_name ?? r.purpose}</div>
                  <div style={{ color: "var(--muted)" }}>{r.purpose}</div>
                </div>
                <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => void s.removeReference(shotId, r.id)}>×</button>
              </div>
            );
          })}
          {refs.length === 0 && <span style={{ color: "var(--muted)", fontSize: 13 }}>레퍼런스 없음</span>}
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <select className="input" value={refCharacter} onChange={(e) => setRefCharacter(e.target.value)}>
            <option value="">캐릭터 선택</option>
            {chars.characters.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
          <select className="input" value={refPurpose} onChange={(e) => setRefPurpose(e.target.value)}>
            {SHOT_REFERENCE_PURPOSES.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
          <button
            className="btn"
            disabled={!refCharacter}
            onClick={() => {
              void s.addReference(shotId, { character_id: refCharacter, purpose: refPurpose });
              setRefCharacter("");
            }}
          >
            추가
          </button>
        </div>

        <h3>프레임 생성</h3>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <label>배치
            <select className="input" value={batchSize} onChange={(e) => setBatchSize(Number(e.target.value))}>
              {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}장</option>)}
            </select>
          </label>
          <label style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 13 }}>
            <input type="checkbox" checked={useContinuity} onChange={(e) => setUseContinuity(e.target.checked)} />
            연속 생성
          </label>
          <button className="btn btn-primary" disabled={busy} onClick={() => void s.generateFrame(shotId, { batchSize, useContinuity })}>
            {busy ? "생성 중…" : "프레임 생성"}
          </button>
          <button className="btn" disabled={busy} onClick={() => void s.retake(shotId)}>리테이크</button>
        </div>
        {useContinuity && cont && (
          <div style={{ fontSize: 13, color: "var(--muted)" }}>
            {cont.available
              ? `이전 샷 스틸 사용 — ${cont.summary}`
              : `연속 생성 불가: ${cont.reason}`}
          </div>
        )}

        <h3>테이크 ({(s.takes[shotId] ?? []).length})</h3>
        <TakeGrid shotId={shotId} />

        <h3>멀티샷 (P1-1)</h3>
        <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 8 }}>
          승인된 프레임에서 여러 앵글·구도의 커버리지를 생성합니다. 프레임 미승인 시 실행할 수 없습니다.
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn btn-primary" disabled={msBusy} onClick={() => void runMultishot()}>
            {msBusy ? "생성 중…" : "멀티샷 생성"}
          </button>
          {msSet && (
            <button className="btn btn-ghost" onClick={() => void refreshMsSet()}>
              상태 새로고침
            </button>
          )}
          {msCost && <span style={{ fontSize: 13, color: "var(--muted)" }}>예상 ~${msCost}</span>}
        </div>
        {msError && <p style={{ color: "red", fontSize: 13 }}>{msError}</p>}
        {msSet && (
          <div style={{ marginTop: 8, display: "grid", gap: 4 }}>
            <div style={{ fontSize: 13 }}>
              세트 상태: <span className="badge yellow">{msSet.status}</span>
              {msSet.status !== "done" && msSet.status !== "failed" && " — 생성 중…"}
            </div>
            {msSet.frames.map((f) => (
              <div key={f.id} style={{ fontSize: 12, color: "var(--muted)" }}>
                {f.coverage_type} · {f.status}
                {f.approved && <span className="badge green" style={{ marginLeft: 6 }}>승인됨</span>}
              </div>
            ))}
            {msSet.frames.length === 0 && (
              <div style={{ fontSize: 12, color: "var(--muted)" }}>프레임 생성 중…</div>
            )}
          </div>
        )}
      </div>
      {confirmDelete && (
        <ConfirmModal
          title="샷 삭제"
          message="이 샷을 삭제할까요? 생성된 테이크 파일은 유지됩니다."
          confirmLabel="삭제"
          onClose={() => setConfirmDelete(false)}
          onConfirm={() => {
            setConfirmDelete(false);
            void s.deleteShot(shotId);
          }}
        />
      )}
    </Modal>
  );
}

export default function BoardPage() {
  const params = useParams();
  const projectId = params.id as string;
  const s = useBoardStore();
  const eventSeq = useWorkspaceStore((st) => st.eventSeq);
  const jobs = useWorkspaceStore((st) => st.jobs);

  useEffect(() => {
    void s.init(projectId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // When every in-flight take for a shot reaches a terminal SSE state, refresh.
  useEffect(() => {
    const st = useBoardStore.getState();
    for (const shotId of Object.keys(st.pendingTakes)) {
      if (st.takesSettled(shotId, jobs)) st.handleTakesSettled(shotId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventSeq]);

  return (
    <div className="page">
      <div className="page-header">
        <h1>보드</h1>
        <button className="btn btn-primary" onClick={() => void s.addScene()}>씬 추가</button>
      </div>
      {s.loading && <p>불러오는 중…</p>}
      {s.error && <p style={{ color: "red" }}>{s.error}</p>}
      <div style={{ display: "flex", gap: 16, overflowX: "auto", paddingBottom: 16 }}>
        {s.scenes.map((scene: Scene) => (
          <div key={scene.id} style={{ minWidth: 260, maxWidth: 260 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <strong>{scene.title ?? `씬 ${scene.order_index + 1}`}</strong>
              <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => void s.addShot(scene.id)}>
                + 샷
              </button>
            </div>
            {scene.location && (
              <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 8 }}>{scene.location}</div>
            )}
            <div style={{ display: "grid", gap: 8 }}>
              {scene.shots.map((shot) => (
                <ShotCard key={shot.id} shot={shot} sceneId={scene.id} />
              ))}
              {scene.shots.length === 0 && (
                <div style={{ fontSize: 13, color: "var(--muted)" }}>샷이 없습니다.</div>
              )}
            </div>
          </div>
        ))}
      </div>
      {s.selectedShotId && <ShotModal />}
    </div>
  );
}
