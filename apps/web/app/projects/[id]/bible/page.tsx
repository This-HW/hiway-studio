// World bible tab: narrative bible entries (character/faction/location/
// rule/event/item). Phase 6, P2 (ADR-0010).
"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import {
  BIBLE_CATEGORIES,
  createBibleEntry,
  deleteBibleEntry,
  listBibleEntries,
  updateBibleEntry,
  type BibleCategory,
  type BibleEntry,
} from "@/lib/bible-api";

const BIBLE_CATEGORY_LABELS: Record<BibleCategory, string> = {
  character: "캐릭터",
  faction: "세력",
  location: "장소",
  rule: "규칙",
  event: "사건",
  item: "아이템",
};

export default function BiblePage() {
  const params = useParams();
  const projectId = params.id as string;
  const [entries, setEntries] = useState<BibleEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("");
  const [query, setQuery] = useState<string>("");
  const [newTitle, setNewTitle] = useState("");
  const [newCategory, setNewCategory] = useState<BibleCategory>("character");
  const [newBody, setNewBody] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editBody, setEditBody] = useState("");

  const reload = async () => {
    try {
      setLoading(true);
      setEntries(
        await listBibleEntries(projectId, {
          category: filter || undefined,
          q: query || undefined,
        }),
      );
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
  }, [projectId, filter]);

  const onCreate = async () => {
    if (!newTitle.trim()) return;
    await createBibleEntry(projectId, {
      category: newCategory,
      title: newTitle.trim(),
      body: newBody,
    });
    setNewTitle("");
    setNewBody("");
    await reload();
  };

  const onDelete = async (id: string) => {
    await deleteBibleEntry(id);
    await reload();
  };

  const onSaveEdit = async (id: string) => {
    await updateBibleEntry(id, { body: editBody });
    setEditingId(null);
    await reload();
  };

  if (loading) return <div className="page">불러오는 중…</div>;
  if (error) return <div className="page">오류: {error}</div>;

  return (
    <div className="page">
      <h2 className="section-title">월드 바이블</h2>
      <section className="card" aria-label="바이블 검색">
        <div className="row">
          <select
            className="input"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            <option value="">전체 카테고리</option>
            {BIBLE_CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {BIBLE_CATEGORY_LABELS[c]}
              </option>
            ))}
          </select>
          <input
            className="input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="검색"
          />
          <button className="btn" onClick={() => void reload()}>
            검색
          </button>
        </div>
      </section>
      <section className="card" aria-label="바이블 항목">
        {entries.length === 0 ? (
          <p className="muted">항목이 없습니다.</p>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {entries.map((e) => (
              <li
                key={e.id}
                className="card"
                style={{ marginBottom: 8, padding: 12 }}
              >
                <strong>
                  [{BIBLE_CATEGORY_LABELS[e.category]}] {e.title}
                </strong>
                {editingId === e.id ? (
                  <>
                    <textarea
                      className="input"
                      value={editBody}
                      onChange={(ev) => setEditBody(ev.target.value)}
                    />
                    <div className="row">
                      <button className="btn btn-primary" onClick={() => void onSaveEdit(e.id)}>
                        저장
                      </button>
                      <button className="btn" onClick={() => setEditingId(null)}>
                        취소
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    <p className="muted">{e.body}</p>
                    <div className="row">
                      <button
                        className="btn"
                        onClick={() => {
                          setEditingId(e.id);
                          setEditBody(e.body);
                        }}
                      >
                        수정
                      </button>
                      <button
                        className="btn btn-ghost"
                        onClick={() => void onDelete(e.id)}
                      >
                        삭제
                      </button>
                    </div>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="card" aria-label="새 바이블 항목">
        <h3 className="section-title">새 항목</h3>
        <div className="row">
          <select
            className="input"
            value={newCategory}
            onChange={(e) => setNewCategory(e.target.value as BibleCategory)}
          >
            {BIBLE_CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {BIBLE_CATEGORY_LABELS[c]}
              </option>
            ))}
          </select>
          <input
            className="input"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="제목"
          />
        </div>
        <div className="field" style={{ marginTop: 8 }}>
          <textarea
            className="input"
            value={newBody}
            onChange={(e) => setNewBody(e.target.value)}
            placeholder="내용"
          />
        </div>
        <div style={{ marginTop: 8 }}>
          <button className="btn btn-primary" onClick={() => void onCreate()}>
            추가
          </button>
        </div>
      </section>
    </div>
  );
}
