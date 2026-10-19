// Character Lab: scope tabs + card grid + right detail panel (references / wardrobe).
"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef } from "react";
import { ConfirmModal } from "@/components/ConfirmModal";
import { Modal } from "@/components/Modal";
import { useCharactersStore } from "@/stores/characters";
import { toast } from "@/stores/toast";
import { useWorkspaceStore } from "@/stores/workspace";
import type { Character, CharacterScope, Reference } from "@/lib/types";
import { CONSISTENCY_METHODS } from "@/lib/types";
import { resolveFileUrl } from "@/lib/files";
import { apiGet, apiPost } from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import { useState } from "react";

const SCOPES: { key: CharacterScope; label: string }[] = [
  { key: "project", label: "내 프로젝트" },
  { key: "global", label: "전역" },
  { key: "all", label: "전체" },
];

// P1-4 LoRA 학습 (§05C-4): 캐릭터 레퍼런스를 학습 데이터로 LoRA 학습
function LoraTrainingCard({ characterId, references }: { characterId: string; references: Reference[] }) {
  const [selected, setSelected] = useState<string[]>([]);
  const [triggerWord, setTriggerWord] = useState("");
  const [rank, setRank] = useState("");
  const [steps, setSteps] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cost, setCost] = useState<string | null>(null);
  const [loras, setLoras] = useState<{ id: string; trigger_word: string; status: string; training_image_count: number | null }[]>([]);

  const loadLoras = async () => {
    try {
      setLoras(await apiGet<{ id: string; trigger_word: string; status: string; training_image_count: number | null }[]>(
        `/api/v1/characters/${characterId}/loras`,
      ));
    } catch {
      // 목록 조회 실패는 조용히 무시 (학습 기능 자체는 동작)
    }
  };

  useEffect(() => {
    setSelected([]);
    setTriggerWord("");
    setConfirm(false);
    setError("");
    setCost(null);
    void loadLoras();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [characterId]);

  const toggle = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  const run = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await apiPost<{ estimated_cost_usd: string }>(
        `/api/v1/characters/${characterId}/lora/train`,
        {
          training_image_ids: selected,
          rank: rank ? Number(rank) : undefined,
          steps: steps ? Number(steps) : undefined,
          trigger_word: triggerWord,
          confirm: selected.length < 8 ? confirm : false,
        },
        { idempotencyKey: `lora-train-${characterId}-${Date.now()}` },
      );
      setCost(res.estimated_cost_usd);
      toast("LoRA 학습을 시작했습니다.");
      void loadLoras();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "요청에 실패했습니다.");
    } finally {
      setBusy(false);
    }
  };

  const needConfirm = selected.length < 8;
  const canRun = selected.length > 0 && triggerWord.trim().length > 0 && (!needConfirm || confirm);

  return (
    <div className="card" style={{ marginBottom: 10 }}>
      <h3 className="section-title">LoRA 학습 (P1-4)</h3>
      <p className="small muted" style={{ marginBottom: 8 }}>
        레퍼런스 이미지를 학습 데이터로 캐릭터 전용 LoRA를 학습합니다. 8장 이상 권장.
      </p>
      <div className="field">
        <label>학습 이미지 ({selected.length}장 선택)</label>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 4 }}>
          {references.map((r) => {
            const url = resolveFileUrl(r.image_path);
            const on = selected.includes(r.id);
            return (
              <label key={r.id} style={{ cursor: "pointer", border: on ? "2px solid var(--primary)" : "1px solid #ddd", borderRadius: 4, padding: 2 }}>
                <input type="checkbox" checked={on} onChange={() => toggle(r.id)} style={{ display: "none" }} />
                {url && <img src={url} alt={r.role} style={{ width: 48, height: 48, objectFit: "cover", display: "block" }} />}
                <span className="small muted" style={{ fontSize: 10 }}>{r.role}</span>
              </label>
            );
          })}
          {references.length === 0 && <span className="small muted">레퍼런스가 없습니다. 먼저 레퍼런스를 등록하세요.</span>}
        </div>
      </div>
      <div className="field">
        <label>트리거 워드 (필수)</label>
        <input className="input" value={triggerWord} onChange={(e) => setTriggerWord(e.target.value)} placeholder="예: mira_character" maxLength={64} />
      </div>
      <div className="row">
        <div className="field">
          <label>rank (선택)</label>
          <input className="input" type="number" min={1} max={256} value={rank} onChange={(e) => setRank(e.target.value)} placeholder="기본값" />
        </div>
        <div className="field">
          <label>steps (선택)</label>
          <input className="input" type="number" min={1} value={steps} onChange={(e) => setSteps(e.target.value)} placeholder="기본값" />
        </div>
      </div>
      {needConfirm && selected.length > 0 && (
        <label className="small" style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8 }}>
          <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
          학습 이미지가 8장 미만임을 확인했습니다
        </label>
      )}
      <button className="btn btn-primary" disabled={busy || !canRun} onClick={() => void run()}>
        {busy ? "학습 시작 중…" : "LoRA 학습 시작"}
      </button>
      {cost && <span className="small muted" style={{ marginLeft: 8 }}>예상 ~${cost}</span>}
      {error && <p style={{ color: "var(--danger)", fontSize: 13 }}>{error}</p>}
      {loras.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <h4 className="section-title" style={{ fontSize: 13 }}>학습된 LoRA</h4>
          {loras.map((l) => (
            <div key={l.id} className="list-row">
              <div>
                <div style={{ fontWeight: 600 }}>{l.trigger_word}</div>
                <div className="small muted">{l.training_image_count ?? "?"}장 · {l.status}</div>
              </div>
              {(l.status === "training" || l.status === "queued") && (
                <span className="badge yellow">학습 중…</span>
              )}
              {l.status === "done" && <span className="badge green">완료</span>}
              {l.status === "failed" && <span className="badge" style={{ color: "var(--danger)" }}>실패</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MasterThumb({ c }: { c: Character }) {  const url = c.master_thumbnail_url ?? resolveFileUrl(null);
  if (url) {
    return <img src={url} alt={c.name} className="thumb square" />;
  }
  return <div className="placeholder-box square">마스터 없음</div>;
}

export default function CharactersPage() {
  const params = useParams();
  const projectId = params.id as string;
  const s = useCharactersStore();

  const eventSeq = useWorkspaceStore((st) => st.eventSeq);
  const jobs = useWorkspaceStore((st) => st.jobs);
  const masterFileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    void s.init(projectId);
    void s.loadImageModels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // Watch SSE job completions for pending master generations.
  useEffect(() => {
    const pending = s.masterJobByCharacter;
    for (const [characterId, jobId] of Object.entries(pending)) {
      const job = jobs[jobId];
      if (!job) continue;
      if (job.status === "completed") {
        s.markMasterJobDone(characterId);
        toast("마스터 초상이 생성되었습니다.");
        void s.refreshDetail();
        void s.loadCharacters();
      } else if (job.status === "failed") {
        s.markMasterJobDone(characterId);
        toast(
          `마스터 생성 실패: ${job.error_code ?? "알 수 없는 오류"}`,
          { kind: "error" }
        );
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventSeq]);

  const detail = s.detail;
  const pendingMasterJob = s.selectedId
    ? s.masterJobByCharacter[s.selectedId]
    : undefined;
  const pendingJob = pendingMasterJob ? jobs[pendingMasterJob] : undefined;

  const onMasterFile = (file: File | undefined) => {
    if (file) void s.uploadMaster(file);
  };

  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <div className="tabs" style={{ marginBottom: 0, borderBottom: "none" }}>
          {SCOPES.map((sc) => (
            <button
              key={sc.key}
              className={s.scope === sc.key ? "tab active" : "tab"}
              onClick={() => s.setScope(sc.key)}
            >
              {sc.label}
            </button>
          ))}
        </div>
        <button className="btn btn-primary" onClick={s.openDialog}>
          새 캐릭터
        </button>
      </div>

      <div className="two-col" style={{ gridTemplateColumns: "1fr 340px" }}>
        {/* ---- grid ---- */}
        <section aria-label="캐릭터 목록">
          {s.loading && <p className="muted">불러오는 중…</p>}
          {s.error && <p style={{ color: "var(--danger)" }}>{s.error}</p>}
          {!s.loading && s.characters.length === 0 && (
            <div className="empty-state">
              <p>첫 캐릭터를 만드세요.</p>
              <p className="small">
                팁: 외형 앵커(appearance_prompt)에 나이·체형·헤어·의상 같은
                고정 요소를 구체적으로 적으면 일관성이 좋아집니다.
              </p>
            </div>
          )}
          <div className="grid-cards">
            {s.characters.map((c) => {
              const busy =
                s.masterJobByCharacter[c.id] !== undefined;
              const job = busy ? jobs[s.masterJobByCharacter[c.id]] : undefined;
              return (
                <div
                  key={c.id}
                  className={
                    c.id === s.selectedId ? "card clickable selected" : "card clickable"
                  }
                  onClick={() => void s.select(c.id)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void s.select(c.id);
                  }}
                >
                  <MasterThumb c={c} />
                  {busy && (
                    <div style={{ marginTop: 8 }}>
                      <div className="small muted">
                        마스터 생성 중… {job?.progress_pct ?? 0}%
                      </div>
                      <div className="progress-bar">
                        <div style={{ width: `${job?.progress_pct ?? 0}%` }} />
                      </div>
                    </div>
                  )}
                  <h3 className="card-title" style={{ marginTop: 8 }}>
                    {c.name}
                  </h3>
                  <div className="row">
                    <span className="badge gray">v{c.version}</span>
                    {c.scope && <span className="badge gray">{c.scope}</span>}
                    <span className="small muted">
                      사용 샷 {c.referencing_shots ?? 0}개
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        {/* ---- detail panel ---- */}
        <aside aria-label="캐릭터 상세">
          {!detail && (
            <div className="empty-state">
              <p>캐릭터를 선택하면 상세 패널이 열립니다.</p>
            </div>
          )}
          {detail && (
            <div className="card">
              <div
                className="row"
                style={{ justifyContent: "space-between", marginBottom: 8 }}
              >
                <h2 className="section-title" style={{ margin: 0 }}>
                  {detail.name}
                </h2>
                <span className="badge gray">v{detail.version}</span>
              </div>
              <div style={{ marginBottom: 10 }}>
                <MasterThumb c={detail} />
              </div>
              {pendingJob && (
                <div className="notice">
                  마스터 생성 중… {pendingJob.progress_pct}% ({pendingJob.stage})
                </div>
              )}

              <div className="row" style={{ flexWrap: "wrap", marginBottom: 10 }}>
                <button className="btn" onClick={s.openEdit}>
                  외형 수정
                </button>
                <Link
                  className="btn"
                  href={`/projects/${projectId}/characters/${detail.id}/sheet`}
                >
                  시트 보기
                </Link>
                <button
                  className="btn btn-ghost small"
                  style={{ color: "var(--danger)" }}
                  onClick={() => s.openDelete(detail)}
                >
                  삭제
                </button>
              </div>

              {/* master generation / upload */}
              <div className="card" style={{ marginBottom: 10 }}>
                <h3 className="section-title">마스터 초상</h3>
                <div className="field">
                  <label>모델 (선택)</label>
                  <select
                    className="select"
                    value={s.masterModelId}
                    onChange={(e) => s.setMasterModelId(e.target.value)}
                  >
                    <option value="">미지정 (라우터가 선택)</option>
                    {s.imageModels.map((m) => (
                      <option key={m.model_id} value={m.model_id}>
                        {m.name ?? m.model_id}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label>프롬프트 (선택)</label>
                  <input
                    className="input"
                    value={s.masterPrompt}
                    onChange={(e) => s.setMasterPrompt(e.target.value)}
                    placeholder="비우면 외형 앵커를 사용"
                  />
                </div>
                <div className="row" style={{ flexWrap: "wrap" }}>
                  <button
                    className="btn btn-primary"
                    onClick={() => void s.generateMaster()}
                    disabled={s.masterGenerating || pendingMasterJob !== undefined}
                  >
                    {s.masterGenerating ? "요청 중…" : "AI 생성"}
                  </button>
                  <button
                    className="btn"
                    onClick={() => masterFileRef.current?.click()}
                    disabled={s.uploadingMaster}
                  >
                    {s.uploadingMaster ? "업로드 중…" : "업로드"}
                  </button>
                  <input
                    ref={masterFileRef}
                    type="file"
                    accept=".png,.jpg,.jpeg,.webp"
                    style={{ display: "none" }}
                    onChange={(e) => {
                      onMasterFile(e.target.files?.[0]);
                      e.target.value = "";
                    }}
                  />
                </div>
                <p className="small muted" style={{ marginTop: 8 }}>
                  예상 비용은 생성 요청(202) 응답의 estimated_cost_usd로 확정
                  표시됩니다.
                </p>
              </div>

              <LoraTrainingCard characterId={detail.id} references={s.references} />

              {/* references / wardrobe tabs */}
              <div className="tabs">
                <button
                  className={s.detailTab === "references" ? "tab active" : "tab"}
                  onClick={() => s.setDetailTab("references")}
                >
                  레퍼런스 ({s.references.length})
                </button>
                <button
                  className={s.detailTab === "wardrobe" ? "tab active" : "tab"}
                  onClick={() => s.setDetailTab("wardrobe")}
                >
                  의상 ({s.wardrobe.length})
                </button>
              </div>

              {s.detailTab === "references" && (
                <ReferenceList
                  references={s.references}
                  onToggle={(r) => void s.togglePrimary(r)}
                  onDelete={(id) => void s.deleteReference(id)}
                />
              )}
              {s.detailTab === "wardrobe" && (
                <div>
                  {s.wardrobe.map((w) => (
                    <div key={w.id} className="list-row">
                      <div>
                        <div style={{ fontWeight: 600 }}>{w.name}</div>
                        <div className="small muted">{w.prompt_fragment}</div>
                        {w.is_default && (
                          <span className="badge green">기본</span>
                        )}
                      </div>
                    </div>
                  ))}
                  {s.wardrobe.length === 0 && (
                    <p className="small muted">등록된 의상이 없습니다.</p>
                  )}
                  <div className="card" style={{ marginTop: 10 }}>
                    <h4 className="section-title">의상 추가</h4>
                    <div className="field">
                      <label>이름</label>
                      <input
                        className="input"
                        value={s.wardrobeName}
                        onChange={(e) => s.setWardrobeName(e.target.value)}
                      />
                    </div>
                    <div className="field">
                      <label>프롬프트 조각 (필수)</label>
                      <input
                        className="input"
                        value={s.wardrobePromptFragment}
                        onChange={(e) =>
                          s.setWardrobePromptFragment(e.target.value)
                        }
                        placeholder="예: navy wool coat, brass buttons"
                      />
                    </div>
                    <button
                      className="btn"
                      onClick={() => void s.addWardrobe()}
                      disabled={s.addingWardrobe}
                    >
                      {s.addingWardrobe ? "추가 중…" : "추가"}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </aside>
      </div>

      {/* ---- new character dialog ---- */}
      {s.dialogOpen && (
        <Modal title="새 캐릭터" onClose={s.closeDialog}>
          <div className="field">
            <label>이름</label>
            <input
              className="input"
              value={s.form.name}
              onChange={(e) => s.setForm({ name: e.target.value })}
            />
          </div>
          <div className="field">
            <label>설명 (선택)</label>
            <textarea
              className="textarea"
              value={s.form.description}
              onChange={(e) => s.setForm({ description: e.target.value })}
            />
          </div>
          <div className="field">
            <label>외형 앵커 (appearance_prompt) — 필수</label>
            <textarea
              className="textarea"
              value={s.form.appearance_prompt}
              onChange={(e) => s.setForm({ appearance_prompt: e.target.value })}
              placeholder="나이, 체형, 헤어스타일, 눈 색, 대표 의상 등"
            />
          </div>
          <div className="field">
            <label>일관성 방식</label>
            <select
              className="select"
              value={s.form.consistency_method}
              onChange={(e) => s.setForm({ consistency_method: e.target.value })}
            >
              {CONSISTENCY_METHODS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
          <div className="modal-actions">
            <button className="btn" onClick={s.closeDialog} disabled={s.creating}>
              취소
            </button>
            <button
              className="btn btn-primary"
              onClick={() => void s.createCharacter()}
              disabled={s.creating}
            >
              {s.creating ? "생성 중…" : "만들기"}
            </button>
          </div>
        </Modal>
      )}

      {/* ---- edit appearance (version bump) ---- */}
      {s.editOpen && (
        <ConfirmModal
          title="외형 수정"
          message={`외형이 바뀌면 version이 증가하고, 이 캐릭터를 쓰는 샷에 "구식" 배지가 붙습니다.\n(현재 사용 샷: ${detail?.referencing_shots ?? 0}개)`}
          confirmLabel="수정 적용"
          busy={s.savingEdit}
          onConfirm={() => void s.saveEdit()}
          onClose={s.closeEdit}
        >
          <div className="field">
            <label>외형 앵커 (appearance_prompt)</label>
            <textarea
              className="textarea"
              value={s.editAppearancePrompt}
              onChange={(e) => s.setEditAppearancePrompt(e.target.value)}
              disabled={s.savingEdit}
            />
          </div>
          <div className="field">
            <label>설명</label>
            <textarea
              className="textarea"
              value={s.editDescription}
              onChange={(e) => s.setEditDescription(e.target.value)}
              disabled={s.savingEdit}
            />
          </div>
        </ConfirmModal>
      )}

      {/* ---- delete ---- */}
      {s.deleteTarget && (
        <ConfirmModal
          title="캐릭터 삭제"
          message={`"${s.deleteTarget.name}" 캐릭터를 삭제할까요? 참조 중인 샷이 ${s.deleteTarget.referencing_shots ?? 0}개 있습니다. 삭제 후 해당 샷의 레퍼런스에서 제거됩니다.`}
          confirmLabel="삭제"
          danger
          busy={s.deleting}
          onConfirm={() => void s.confirmDelete()}
          onClose={s.closeDelete}
        />
      )}
    </div>
  );
}

function ReferenceList({
  references,
  onToggle,
  onDelete,
}: {
  references: Reference[];
  onToggle: (r: Reference) => void;
  onDelete: (id: string) => void;
}) {
  if (references.length === 0) {
    return <p className="small muted">레퍼런스가 없습니다.</p>;
  }
  return (
    <div className="grid-cards" style={{ gridTemplateColumns: "1fr 1fr" }}>
      {references.map((r) => {
        const url = resolveFileUrl(r.image_path);
        return (
          <div key={r.id} className="card">
            {url ? (
              <img src={url} alt={r.role} className="thumb square" />
            ) : (
              <div className="placeholder-box square">
                이미지 서빙 준비 중
              </div>
            )}
            <div className="row" style={{ marginTop: 6, flexWrap: "wrap" }}>
              <span className="badge gray">{r.role}</span>
              {r.is_primary && <span className="badge green">주 레퍼런스</span>}
              {r.source && <span className="small muted">{r.source}</span>}
            </div>
            <div className="row" style={{ marginTop: 6 }}>
              <button className="btn btn-ghost small" onClick={() => onToggle(r)}>
                {r.is_primary ? "주 지정 해제" : "주로 지정"}
              </button>
              <button
                className="btn btn-ghost small"
                style={{ color: "var(--danger)" }}
                onClick={() => {
                  if (window.confirm("이 레퍼런스를 삭제할까요?")) onDelete(r.id);
                }}
              >
                삭제
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
