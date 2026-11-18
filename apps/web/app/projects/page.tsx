"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { ConfirmModal } from "@/components/ConfirmModal";
import { Modal } from "@/components/Modal";
import { ToastStack } from "@/components/Toast";
import { resolveFileUrl } from "@/lib/files";
import { ASPECT_RATIOS, formatMonthlyCost } from "@/lib/format";
import { useProjectsStore } from "@/stores/projects";

/** Card thumbnail: the API's relative frame path served via /files (P0_SCREENS §1). */
function ProjectThumb({ path }: { path: string | null }) {
  const url = resolveFileUrl(path);
  if (url) return <img src={url} alt="" className="thumb" />;
  return <div className="placeholder-box">썸네일 없음</div>;
}

export default function ProjectsPage() {
  const router = useRouter();
  const s = useProjectsStore();

  useEffect(() => {
    void s.load(s.showArchived);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectedPack = s.categoryPacks.find(
    (p) => p.code === s.form.category_pack_code
  );

  return (
    <main className="page">
      <div className="page-header">
        <h1>프로젝트</h1>
        <div className="row">
          <label className="row small muted" style={{ gap: 6 }}>
            <input
              type="checkbox"
              checked={s.showArchived}
              onChange={(e) => s.setShowArchived(e.target.checked)}
            />
            아카이브 보기
          </label>
          <button className="btn btn-primary" onClick={() => void s.openDialog()}>
            새 프로젝트
          </button>
        </div>
      </div>

      {s.loading && (
        <div className="grid-cards">
          {[0, 1, 2].map((i) => (
            <div key={i} className="card">
              <div className="skeleton" style={{ height: 120 }} />
              <div className="skeleton" style={{ height: 18, marginTop: 10 }} />
            </div>
          ))}
        </div>
      )}

      {s.error && <p style={{ color: "var(--danger)" }}>{s.error}</p>}

      {!s.loading && !s.error && s.projects.length === 0 && (
        <div className="empty-state">
          <p>첫 프로젝트를 만드세요.</p>
          <button
            className="btn btn-primary"
            onClick={() => void s.openDialog()}
          >
            새 프로젝트 만들기
          </button>
        </div>
      )}

      <div className="grid-cards">
        {s.projects.map((p) => (
          <div
            key={p.id}
            className="card clickable"
            onClick={() => router.push(`/projects/${p.id}/story`)}
            role="link"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter") router.push(`/projects/${p.id}/story`);
            }}
          >
            <ProjectThumb path={p.thumbnail} />
            <h3 className="card-title" style={{ marginTop: 10 }}>
              {p.title}
            </h3>
            <div className="row" style={{ flexWrap: "wrap", marginBottom: 6 }}>
              <span className="badge gray">{p.aspect_ratio}</span>
              {p.category_pack_name && (
                <span className="badge">{p.category_pack_name}</span>
              )}
              {p.is_archived && (
                <span className="badge gray">아카이브됨</span>
              )}
            </div>
            <p className="small muted" style={{ margin: "4px 0" }}>
              샷 {p.shot_count ?? 0} · 승인 {p.approved_shot_count ?? 0} ·
              클립 {p.clip_count ?? 0}
            </p>
            <p className="small muted" style={{ margin: "4px 0" }}>
              당월 비용{" "}
              {formatMonthlyCost(
                p.monthly_cost_estimated_usd,
                p.monthly_cost_actual_usd
              )}
            </p>
            <div
              className="row"
              style={{ marginTop: 8 }}
              onClick={(e) => e.stopPropagation()}
            >
              <button
                className="btn btn-ghost small"
                onClick={() => void s.duplicate(p.id)}
                disabled={p.is_archived}
              >
                복제
              </button>
              {!p.is_archived && (
                <button
                  className="btn btn-ghost small"
                  onClick={() => void s.archive(p.id)}
                >
                  아카이브
                </button>
              )}
              <button
                className="btn btn-ghost small"
                style={{ color: "var(--danger)" }}
                onClick={() => s.openDelete(p)}
              >
                삭제
              </button>
            </div>
          </div>
        ))}
      </div>

      {s.dialogOpen && (
        <Modal title="새 프로젝트" onClose={s.closeDialog}>
          <div className="field">
            <label>제목</label>
            <input
              className="input"
              value={s.form.title}
              onChange={(e) => s.setForm({ title: e.target.value })}
              placeholder="프로젝트 제목"
            />
          </div>
          <div className="field">
            <label>화면비</label>
            <select
              className="select"
              value={s.form.aspect_ratio}
              onChange={(e) => s.setForm({ aspect_ratio: e.target.value })}
            >
              {ASPECT_RATIOS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>카테고리 팩 (선택)</label>
            <select
              className="select"
              value={s.form.category_pack_code}
              onChange={(e) => s.setForm({ category_pack_code: e.target.value })}
            >
              <option value="">선택 안 함</option>
              {s.categoryPacks.map((p) => (
                <option key={p.code} value={p.code}>
                  {p.name}
                </option>
              ))}
            </select>
            {selectedPack?.defaults && (
              <p className="small muted" style={{ marginTop: 6 }}>
                적용되는 기본값:{" "}
                {JSON.stringify(selectedPack.defaults)}
              </p>
            )}
            <p className="small muted">
              카테고리 팩은 생성 시점에만 지정할 수 있습니다.
            </p>
          </div>
          <div className="field">
            <label>기본 텍스트 모델 (선택)</label>
            <select
              className="select"
              value={s.form.text_model_id}
              onChange={(e) => s.setForm({ text_model_id: e.target.value })}
            >
              <option value="">미지정 (라우터가 선택)</option>
              {s.textModels.map((m) => (
                <option key={m.model_id} value={m.model_id}>
                  {m.name ?? m.model_id}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>기본 이미지 모델 (선택)</label>
            <select
              className="select"
              value={s.form.image_model_id}
              onChange={(e) => s.setForm({ image_model_id: e.target.value })}
            >
              <option value="">미지정 (라우터가 선택)</option>
              {s.imageModels.map((m) => (
                <option key={m.model_id} value={m.model_id}>
                  {m.name ?? m.model_id}
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
              onClick={() => void s.createProject()}
              disabled={s.creating}
            >
              {s.creating ? "생성 중…" : "만들기"}
            </button>
          </div>
        </Modal>
      )}

      {s.deleteTarget && (
        <ConfirmModal
          title="프로젝트 삭제"
          message={`"${s.deleteTarget.title}" 프로젝트를 영구 삭제합니다. 파일 디렉토리까지 삭제되며 복구할 수 없습니다.`}
          requireTitleInput={s.deleteTarget.title}
          confirmLabel="영구 삭제"
          danger
          busy={s.deleting}
          onConfirm={() => void s.confirmDelete()}
          onClose={s.closeDelete}
        />
      )}

      <ToastStack />
    </main>
  );
}
