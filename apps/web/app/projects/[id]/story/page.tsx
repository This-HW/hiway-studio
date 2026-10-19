// Story/plot editor: story card (left), beat list (center), beat detail (right).
"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { ConfirmModal } from "@/components/ConfirmModal";
import { useStoryStore } from "@/stores/story";
import { useWorkspaceStore } from "@/stores/workspace";

export default function StoryPage() {
  const params = useParams();
  const projectId = params.id as string;
  const s = useStoryStore();
  const project = useWorkspaceStore((st) => st.project);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [deleteBeatId, setDeleteBeatId] = useState<string | null>(null);

  useEffect(() => {
    void s.init(projectId);
    void s.loadModels();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const selectedBeat = s.beats.find((b) => b.id === s.selectedBeatId) ?? null;

  const onImportFile = (file: File | undefined) => {
    if (!file) return;
    const ext = file.name.split(".").pop()?.toLowerCase();
    const format =
      ext === "fountain" ? "fountain" : ext === "fdx" ? "fdx" : "txt";
    void s.importScript(file, format);
  };

  return (
    <div>
      {project?.category_pack_name && (
        <div className="notice">
          카테고리 팩: <strong>{project.category_pack_name}</strong>
          {project.category_pack_code && (
            <span className="muted"> ({project.category_pack_code})</span>
          )}
          <span className="muted"> — 읽기 전용 (생성 시점에만 지정)</span>
        </div>
      )}

      <div className="three-col">
        {/* ---- left: story card ---- */}
        <section className="card" aria-label="스토리 카드">
          <h2 className="section-title">스토리</h2>
          {s.loading && <p className="muted">불러오는 중…</p>}
          {s.error && <p style={{ color: "var(--danger)" }}>{s.error}</p>}
          {s.story && (
            <>
              <div className="field">
                <label>로그라인</label>
                <textarea
                  className="textarea"
                  style={{ minHeight: 64 }}
                  value={s.story.logline ?? ""}
                  onChange={(e) => s.setLogline(e.target.value)}
                  placeholder="로그라인 한 줄이면 시작할 수 있습니다."
                />
              </div>
              <div className="field">
                <label>시놉시스</label>
                {s.generatingSynopsis ? (
                  <div className="placeholder-box" style={{ minHeight: 120 }}>
                    생성 중…
                  </div>
                ) : (
                  <textarea
                    className="textarea"
                    style={{ minHeight: 160 }}
                    value={s.synopsisDraft ?? s.story.synopsis ?? ""}
                    onChange={(e) =>
                      s.synopsisDraft !== null
                        ? s.setSynopsisDraft(e.target.value)
                        : s.setSynopsis(e.target.value)
                    }
                    placeholder="시놉시스를 입력하거나 LLM으로 생성하세요."
                  />
                )}
                {s.synopsisDraft !== null && (
                  <div className="warning-box" style={{ marginTop: 8 }}>
                    초안 상태입니다. 확정 버튼을 눌러야 저장됩니다.
                    <div className="row" style={{ marginTop: 8 }}>
                      <button
                        className="btn"
                        onClick={s.discardSynopsisDraft}
                      >
                        초안 버리기
                      </button>
                    </div>
                  </div>
                )}
              </div>

              <div className="field">
                <label>LLM 모델 (선택)</label>
                <select
                  className="select"
                  value={s.llmModelId}
                  onChange={(e) => s.setLlmModelId(e.target.value)}
                >
                  <option value="">미지정 (라우터가 선택)</option>
                  {s.textModels.map((m) => (
                    <option key={m.model_id} value={m.model_id}>
                      {m.name ?? m.model_id}
                    </option>
                  ))}
                </select>
              </div>

              <div className="row" style={{ flexWrap: "wrap" }}>
                <button
                  className="btn"
                  onClick={() => void s.generateSynopsis()}
                  disabled={s.generatingSynopsis || s.generatingBeats}
                >
                  {s.generatingSynopsis ? "시놉시스 생성 중…" : "시놉시스 생성"}
                </button>
                <button
                  className="btn"
                  onClick={() => void s.generateBeats()}
                  disabled={s.generatingSynopsis || s.generatingBeats}
                >
                  {s.generatingBeats ? "비트 생성 중…" : "비트 생성"}
                </button>
              </div>
              <p className="small muted" style={{ marginTop: 8 }}>
                동기 호출입니다. 생성 중 취소는 제공하지 않으며, 결과는 초안으로
                들어갑니다. 비트 생성 결과는 서버에 바로 저장됩니다.
              </p>

              <div className="row" style={{ marginTop: 12 }}>
                <button
                  className="btn btn-primary"
                  onClick={() => void s.saveStory()}
                  disabled={s.saving || (!s.dirty && s.synopsisDraft === null)}
                >
                  {s.saving ? "저장 중…" : "확정 · 저장"}
                </button>
              </div>
            </>
          )}
        </section>

        {/* ---- center: beat list ---- */}
        <section aria-label="비트 목록">
          <h2 className="section-title">비트 ({s.beats.length})</h2>
          {s.generatingBeats && (
            <div className="placeholder-box" style={{ marginBottom: 8 }}>
              비트 생성 중…
            </div>
          )}
          {s.beats.map((b, i) => {
            const summary = b.summary ?? "";
            return (
            <div
              key={b.id}
              className={
                b.id === s.selectedBeatId ? "list-row selected" : "list-row"
              }
              onClick={() => s.selectBeat(b.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter") s.selectBeat(b.id);
              }}
            >
              <div>
                <div style={{ fontWeight: 600 }}>
                  <span className="badge gray" style={{ marginRight: 6 }}>
                    {i + 1}
                  </span>
                  {b.title ?? "제목 없음"}
                </div>
                <div className="small muted">
                  {summary.length > 80 ? `${summary.slice(0, 80)}…` : summary}
                </div>
                {b.scene_ids && b.scene_ids.length > 0 && (
                  <div className="small muted">
                    연결된 씬 {b.scene_ids.length}개
                  </div>
                )}
              </div>
              <div className="row">
                <button
                  className="btn btn-ghost small"
                  onClick={(e) => {
                    e.stopPropagation();
                    s.startBeatEdit(b);
                  }}
                >
                  수정
                </button>
                <button
                  className="btn btn-ghost small"
                  style={{ color: "var(--danger)" }}
                  onClick={(e) => {
                    e.stopPropagation();
                    setDeleteBeatId(b.id);
                  }}
                >
                  삭제
                </button>
              </div>
            </div>
            );
          })}
          {s.beats.length === 0 && !s.generatingBeats && (
            <div className="empty-state">
              <p>비트가 없습니다. 직접 추가하거나 LLM으로 생성하세요.</p>
            </div>
          )}

          <div className="card" style={{ marginTop: 12 }} aria-label="비트 추가">
            <h3 className="section-title">비트 추가</h3>
            <div className="field">
              <label>제목 (선택)</label>
              <input
                className="input"
                value={s.beatFormTitle}
                onChange={(e) =>
                  s.setBeatForm(e.target.value, s.beatFormSummary)
                }
                disabled={s.editingBeatId !== null}
              />
            </div>
            <div className="field">
              <label>요약</label>
              <textarea
                className="textarea"
                value={s.beatFormSummary}
                onChange={(e) =>
                  s.setBeatForm(s.beatFormTitle, e.target.value)
                }
                disabled={s.editingBeatId !== null}
              />
            </div>
            <button
              className="btn"
              onClick={() => void s.addBeat()}
              disabled={s.editingBeatId !== null}
            >
              추가
            </button>
          </div>
        </section>

        {/* ---- right: beat detail + import ---- */}
        <section aria-label="비트 상세">
          <h2 className="section-title">비트 상세</h2>
          {selectedBeat ? (
            <div className="card">
              {s.editingBeatId === selectedBeat.id ? (
                <>
                  <div className="field">
                    <label>제목</label>
                    <input
                      className="input"
                      value={s.beatFormTitle}
                      onChange={(e) =>
                        s.setBeatForm(e.target.value, s.beatFormSummary)
                      }
                    />
                  </div>
                  <div className="field">
                    <label>요약</label>
                    <textarea
                      className="textarea"
                      value={s.beatFormSummary}
                      onChange={(e) =>
                        s.setBeatForm(s.beatFormTitle, e.target.value)
                      }
                    />
                  </div>
                  <div className="row">
                    <button
                      className="btn btn-primary"
                      onClick={() => void s.updateBeat()}
                    >
                      저장
                    </button>
                    <button className="btn" onClick={s.cancelBeatEdit}>
                      취소
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <h3 className="card-title">{selectedBeat.title ?? "제목 없음"}</h3>
                  <p>{selectedBeat.summary}</p>
                  <button
                    className="btn"
                    onClick={() => s.startBeatEdit(selectedBeat)}
                  >
                    수정
                  </button>
                </>
              )}
            </div>
          ) : (
            <div className="empty-state">
              <p>비트를 선택하면 상세 요약을 볼 수 있습니다.</p>
            </div>
          )}

          <div className="card" style={{ marginTop: 12 }} aria-label="스크립트 임포트">
            <h3 className="section-title">스크립트 임포트</h3>
            <div
              className="dropzone"
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                onImportFile(e.dataTransfer.files[0]);
              }}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter") fileInputRef.current?.click();
              }}
            >
              {s.importing
                ? "임포트 중…"
                : "fountain / fdx / txt 파일을 드래그하거나 클릭하세요"}
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept=".fountain,.fdx,.txt"
              style={{ display: "none" }}
              onChange={(e) => {
                onImportFile(e.target.files?.[0]);
                e.target.value = "";
              }}
            />
            <p className="small muted" style={{ marginTop: 8 }}>
              임포트된 씬의 비트 연결은 비어 있습니다. 비트에 연결해 주세요.
            </p>
          </div>
        </section>
      </div>

      {deleteBeatId && (
        <ConfirmModal
          title="비트 삭제"
          message="이 비트를 삭제할까요? 연결된 씬은 삭제되지 않으며 beat 연결만 해제됩니다."
          confirmLabel="삭제"
          danger
          onConfirm={() => {
            const id = deleteBeatId;
            setDeleteBeatId(null);
            void s.deleteBeat(id);
          }}
          onClose={() => setDeleteBeatId(null)}
        />
      )}
    </div>
  );
}
