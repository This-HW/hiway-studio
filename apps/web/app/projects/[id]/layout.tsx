// Workspace shell: left tab nav + top header (project title, job badge,
// SSE status dot). Owns the single SSE subscription for this project and
// funnels all events into the workspace store.
"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect } from "react";
import type { ReactNode } from "react";
import { JobBadge } from "@/components/JobBadge";
import { ToastStack } from "@/components/Toast";
import { apiGet } from "@/lib/api-client";
import { useSse } from "@/lib/sse";
import type { Project } from "@/lib/types";
import { useWorkspaceStore } from "@/stores/workspace";

const TABS = [
  { key: "story", label: "스토리", ready: true },
  { key: "characters", label: "캐릭터", ready: true },
  { key: "board", label: "보드", ready: true },
  { key: "review", label: "리뷰", ready: true },
  { key: "timeline", label: "타임라인", ready: true },
  { key: "dialogue", label: "대사·음성", ready: true },
  { key: "subtitle-edit", label: "자막 편집", ready: true },
  { key: "qc", label: "QC·출력", ready: true },
  { key: "costs", label: "비용", ready: true },
  { key: "episodes", label: "에피소드", ready: true },
  { key: "bible", label: "바이블", ready: true },
  { key: "previz", label: "프리비즈", ready: true },
  { key: "settings", label: "설정", ready: true },
] as const;

export default function ProjectLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: { id: string };
}) {
  const pathname = usePathname();
  const router = useRouter();
  const projectId = params.id;

  const setProjectId = useWorkspaceStore((s) => s.setProjectId);
  const setProject = useWorkspaceStore((s) => s.setProject);
  const project = useWorkspaceStore((s) => s.project);
  const setSseState = useWorkspaceStore((s) => s.setSseState);
  const applySseEvent = useWorkspaceStore((s) => s.applySseEvent);

  const handleSse = useCallback(
    (name: string, data: unknown) => {
      applySseEvent(name, data);
    },
    [applySseEvent]
  );
  const sseState = useSse(handleSse, { enabled: true });

  useEffect(() => {
    setProjectId(projectId);
    setProject(null);
    apiGet<Project>(`/api/v1/projects/${projectId}`)
      .then(setProject)
      .catch(() => {
        // Unknown project id: back to the list (§0.1).
        router.replace("/projects");
      });
    return () => {
      setProjectId(null);
      setProject(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    setSseState(sseState);
  }, [sseState, setSseState]);

  return (
    <div className="shell">
      <nav className="shell-nav" aria-label="워크스페이스 탭">
        <div style={{ padding: "4px 12px 12px" }}>
          <Link href="/projects" className="small muted">
            ← 프로젝트 목록
          </Link>
        </div>
        {TABS.map((t) => {
          const href = `/projects/${projectId}/${t.key}`;
          const active =
            pathname === href || pathname.startsWith(`${href}/`);
          return (
            <Link
              key={t.key}
              href={href}
              className={active ? "nav-link active" : "nav-link"}
            >
              {t.label}
              {!t.ready && (
                <span className="small muted" style={{ marginLeft: 6 }}>
                  Phase 2
                </span>
              )}
            </Link>
          );
        })}
        <div style={{ padding: "12px" }}>
          <Link href="/library" className="small muted">
            전역 라이브러리 →
          </Link>
        </div>
      </nav>
      <div className="shell-main">
        <header className="shell-header">
          <h1 className="shell-title">
            {project ? project.title : "불러오는 중…"}
          </h1>
          <JobBadge />
        </header>
        <div className="shell-body">{children}</div>
      </div>
      <ToastStack />
    </div>
  );
}
