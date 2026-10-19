// Episodes tab: list, create, rename, status, template clone, delete.
// P1-10 (design §8.1). Cloned episodes deep-copy scenes/shots; copied shots
// are draft and regenerate — generations/videos/audio are never copied.
"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  cloneEpisode,
  createEpisode,
  deleteEpisode,
  listEpisodes,
  updateEpisode,
  type Episode,
} from "@/lib/episodes-api";

export default function EpisodesPage() {
  const params = useParams();
  const projectId = params.id as string;
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [newIsTemplate, setNewIsTemplate] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [deleteId, setDeleteId] = useState<string | null>(null);

  const reload = async () => {
    try {
      setLoading(true);
      setEpisodes(await listEpisodes(projectId));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const onCreate = async () => {
    await createEpisode(projectId, {
      title: newTitle.trim() || undefined,
      is_template: newIsTemplate,
    });
    setNewTitle("");
    setNewIsTemplate(false);
    await reload();
  };

  const onClone = async (id: string) => {
    await cloneEpisode(id);
    await reload();
  };

  const onSaveTitle = async (id: string) => {
    await updateEpisode(id, { title: editTitle.trim() || null });
    setEditingId(null);
    await reload();
  };

  const onStatus = async (id: string, status: Episode["status"]) => {
    await updateEpisode(id, { status });
    await reload();
  };

  const onDelete = async () => {
    if (!deleteId) return;
    await deleteEpisode(deleteId);
    setDeleteId(null);
    await reload();
  };

  return (
    <div>
      <h2 className="section-title">에피소드</h2>
      <p className="muted">
        복제는 씬·샷을 통째로 복사합니다. 복사된 샷은 draft 상태로 재생성
        대기하며, 기존 생성물(이미지·영상·오디오)은 복사되지 않습니다.
      </p>

      {loading && <p className="muted">불러오는 중…</p>}
      {error && <p style={{ color: "var(--danger)" }}>{error}</p>}

      <section className="card" aria-label="에피소드 생성">
        <div className="field">
          <label>새 에피소드 제목</label>
          <input
            className="input"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="예: 1화 — 첫 만남"
          />
        </div>
        <label className="muted">
          <input
            type="checkbox"
            checked={newIsTemplate}
            onChange={(e) => setNewIsTemplate(e.target.checked)}
          />{" "}
          템플릿으로 저장 (나중에 복제해서 재사용)
        </label>
        <div style={{ marginTop: 8 }}>
          <button className="btn" onClick={() => void onCreate()}>
            에피소드 추가
          </button>
        </div>
      </section>

      <section className="card" aria-label="에피소드 목록">
        {episodes.length === 0 && !loading && (
          <p className="muted">에피소드가 없습니다.</p>
        )}
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {episodes.map((ep) => (
            <li
              key={ep.id}
              className="card"
              style={{ marginBottom: 8 }}
              aria-label={`에피소드 ${ep.title ?? ep.id}`}
            >
              {editingId === ep.id ? (
                <div className="field">
                  <input
                    className="input"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    autoFocus
                  />
                  <div style={{ marginTop: 8 }}>
                    <button
                      className="btn"
                      onClick={() => void onSaveTitle(ep.id)}
                    >
                      저장
                    </button>{" "}
                    <button
                      className="btn"
                      onClick={() => setEditingId(null)}
                    >
                      취소
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <strong>{ep.title ?? "(제목 없음)"}</strong>
                  <span className="muted">
                    {" "}
                    · 순서 {ep.order_index} · 상태 {ep.status}
                    {ep.is_template && " · 템플릿"}
                    {ep.template_source_episode_id && " · 복제본"}
                  </span>
                  <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
                    <button
                      className="btn"
                      onClick={() => {
                        setEditingId(ep.id);
                        setEditTitle(ep.title ?? "");
                      }}
                    >
                      이름 변경
                    </button>
                    <select
                      className="input"
                      value={ep.status}
                      onChange={(e) =>
                        void onStatus(ep.id, e.target.value as Episode["status"])
                      }
                      aria-label="상태 변경"
                    >
                      <option value="draft">draft</option>
                      <option value="in_progress">in_progress</option>
                      <option value="done">done</option>
                    </select>
                    <button
                      className="btn"
                      onClick={() => void onClone(ep.id)}
                    >
                      복제
                    </button>
                    <button
                      className="btn"
                      style={{ color: "var(--danger)" }}
                      onClick={() => setDeleteId(ep.id)}
                    >
                      삭제
                    </button>
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      </section>

      {deleteId && (
        <ConfirmModal
          title="에피소드 삭제"
          message="에피소드를 삭제합니다. 씬은 에피소드 연결이 해제되고 남습니다."
          confirmLabel="삭제"
          danger
          onClose={() => setDeleteId(null)}
          onConfirm={() => void onDelete()}
        />
      )}
    </div>
  );
}
