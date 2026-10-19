// Prompt snippet picker (P1-8). Standalone: lists global + project snippets,
// filters by category/tag, and hands the chosen body text to the caller
// (e.g. a prompt textarea) via onInsert.
"use client";

import { useEffect, useState } from "react";
import { apiGet } from "@/lib/api-client";

export interface PromptSnippet {
  id: string;
  project_id: string | null;
  title: string;
  body: string;
  negative: string | null;
  category: string | null;
  tags: string[];
  created_at: string;
  updated_at: string;
}

interface Props {
  /** When set, project-scoped snippets are listed alongside global ones. */
  projectId?: string;
  onInsert: (body: string) => void;
}

export function PromptSnippetPicker({ projectId, onInsert }: Props) {
  const [snippets, setSnippets] = useState<PromptSnippet[]>([]);
  const [category, setCategory] = useState("");
  const [tag, setTag] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (category.trim()) params.set("category", category.trim());
      if (tag.trim()) params.set("tag", tag.trim());
      if (projectId) params.set("project_id", projectId);
      const rows = await apiGet<PromptSnippet[]>(
        `/api/v1/prompt-snippets?${params.toString()}`,
      );
      setSnippets(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : "스니펫을 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  return (
    <div className="snippet-picker">
      <div className="snippet-picker-filters">
        <input
          placeholder="카테고리 (예: 조명)"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
        />
        <input
          placeholder="태그"
          value={tag}
          onChange={(e) => setTag(e.target.value)}
        />
        <button type="button" onClick={load} disabled={loading}>
          검색
        </button>
      </div>
      {error && <p className="error-text">{error}</p>}
      {loading ? (
        <p>불러오는 중…</p>
      ) : snippets.length === 0 ? (
        <p className="empty-text">스니펫이 없습니다.</p>
      ) : (
        <ul className="snippet-list">
          {snippets.map((s) => (
            <li key={s.id} className="snippet-item">
              <div className="snippet-head">
                <strong>{s.title}</strong>
                {s.project_id ? (
                  <span className="badge">프로젝트</span>
                ) : (
                  <span className="badge">전역</span>
                )}
                {s.category && <span className="badge">{s.category}</span>}
              </div>
              <p className="snippet-body">{s.body}</p>
              {s.tags.length > 0 && (
                <p className="snippet-tags">{s.tags.map((t) => `#${t}`).join(" ")}</p>
              )}
              <button type="button" onClick={() => onInsert(s.body)}>
                삽입
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
