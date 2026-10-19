// Prompt snippet library page (P1-8). Standalone CRUD page: global snippets
// plus an optional project filter. Existing screens embed PromptSnippetPicker
// instead of linking here; this page is the management surface.
"use client";

import { useCallback, useEffect, useState } from "react";
import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api-client";
import type { PromptSnippet } from "@/components/PromptSnippetPicker";

interface Draft {
  title: string;
  body: string;
  negative: string;
  category: string;
  tags: string;
  project_id: string;
}

const EMPTY: Draft = {
  title: "",
  body: "",
  negative: "",
  category: "",
  tags: "",
  project_id: "",
};

export default function SnippetsPage() {
  const [snippets, setSnippets] = useState<PromptSnippet[]>([]);
  const [scope, setScope] = useState<"" | "global" | "project">("");
  const [projectFilter, setProjectFilter] = useState("");
  const [category, setCategory] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [editingId, setEditingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const params = new URLSearchParams();
      if (scope) params.set("scope", scope);
      if (scope === "project") {
        if (!projectFilter.trim()) {
          setSnippets([]);
          return;
        }
        params.set("project_id", projectFilter.trim());
      }
      if (category.trim()) params.set("category", category.trim());
      setSnippets(
        await apiGet<PromptSnippet[]>(`/api/v1/prompt-snippets?${params.toString()}`),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "불러오기에 실패했습니다.");
    }
  }, [scope, projectFilter, category]);

  useEffect(() => {
    load();
  }, [load]);

  function toPayload(d: Draft) {
    return {
      title: d.title.trim(),
      body: d.body,
      negative: d.negative || null,
      category: d.category.trim() || null,
      tags: d.tags.split(",").map((t) => t.trim()).filter(Boolean),
      project_id: d.project_id.trim() || null,
    };
  }

  async function submit() {
    setError(null);
    try {
      if (editingId) {
        const updated = await apiPatch<PromptSnippet>(
          `/api/v1/prompt-snippets/${editingId}`,
          toPayload(draft),
        );
        setSnippets((rows) => rows.map((r) => (r.id === editingId ? updated : r)));
      } else {
        const created = await apiPost<PromptSnippet>("/api/v1/prompt-snippets", toPayload(draft));
        setSnippets((rows) => [created, ...rows]);
      }
      setDraft(EMPTY);
      setEditingId(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "저장에 실패했습니다.");
    }
  }

  async function remove(id: string) {
    if (!window.confirm("이 스니펫을 삭제할까요?")) return;
    try {
      await apiDelete(`/api/v1/prompt-snippets/${id}`);
      setSnippets((rows) => rows.filter((r) => r.id !== id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "삭제에 실패했습니다.");
    }
  }

  function startEdit(s: PromptSnippet) {
    setEditingId(s.id);
    setDraft({
      title: s.title,
      body: s.body,
      negative: s.negative ?? "",
      category: s.category ?? "",
      tags: s.tags.join(", "),
      project_id: s.project_id ?? "",
    });
    window.scrollTo({ top: 0 });
  }

  return (
    <div className="page">
      <h1 className="page-title">프롬프트 스니펫</h1>

      <div className="card">
        <h2 className="section-title">{editingId ? "스니펫 수정" : "새 스니펫"}</h2>
        <div className="form-grid">
          <input
            placeholder="제목"
            value={draft.title}
            onChange={(e) => setDraft({ ...draft, title: e.target.value })}
          />
          <input
            placeholder="카테고리 (예: 조명)"
            value={draft.category}
            onChange={(e) => setDraft({ ...draft, category: e.target.value })}
          />
        </div>
        <textarea
          placeholder="프롬프트 조각"
          value={draft.body}
          onChange={(e) => setDraft({ ...draft, body: e.target.value })}
          rows={3}
        />
        <textarea
          placeholder="네거티브 프롬프트 (선택)"
          value={draft.negative}
          onChange={(e) => setDraft({ ...draft, negative: e.target.value })}
          rows={2}
        />
        <div className="form-grid">
          <input
            placeholder="태그 (쉼표 구분)"
            value={draft.tags}
            onChange={(e) => setDraft({ ...draft, tags: e.target.value })}
          />
          <input
            placeholder="프로젝트 ID (비우면 전역)"
            value={draft.project_id}
            onChange={(e) => setDraft({ ...draft, project_id: e.target.value })}
          />
        </div>
        <div className="form-actions">
          <button type="button" onClick={submit} disabled={!draft.title.trim() || !draft.body.trim()}>
            {editingId ? "수정" : "추가"}
          </button>
          {editingId && (
            <button
              type="button"
              onClick={() => {
                setEditingId(null);
                setDraft(EMPTY);
              }}
            >
              취소
            </button>
          )}
        </div>
      </div>

      <div className="toolbar">
        <select value={scope} onChange={(e) => setScope(e.target.value as "" | "global" | "project")}>
          <option value="">전체</option>
          <option value="global">전역</option>
          <option value="project">프로젝트</option>
        </select>
        <input
          placeholder="카테고리 필터"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
        />
        {scope === "project" && (
          <input
            placeholder="프로젝트 ID"
            value={projectFilter}
            onChange={(e) => setProjectFilter(e.target.value)}
          />
        )}
      </div>

      {error && <p className="error-text">{error}</p>}

      <ul className="snippet-list">
        {snippets.map((s) => (
          <li key={s.id} className="card snippet-item">
            <div className="snippet-head">
              <strong>{s.title}</strong>
              {s.project_id ? <span className="badge">프로젝트</span> : <span className="badge">전역</span>}
              {s.category && <span className="badge">{s.category}</span>}
            </div>
            <p className="snippet-body">{s.body}</p>
            {s.negative && <p className="snippet-negative">− {s.negative}</p>}
            {s.tags.length > 0 && (
              <p className="snippet-tags">{s.tags.map((t) => `#${t}`).join(" ")}</p>
            )}
            <div className="form-actions">
              <button type="button" onClick={() => startEdit(s)}>
                수정
              </button>
              <button type="button" onClick={() => remove(s.id)}>
                삭제
              </button>
            </div>
          </li>
        ))}
      </ul>
      {snippets.length === 0 && <p className="empty-text">스니펫이 없습니다.</p>}
    </div>
  );
}
