// Global library: assets tab (project-scoped listing + global upload) and
// global characters tab (read-only).
"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";
import { ToastStack } from "@/components/Toast";
import { formatDate } from "@/lib/format";
import { resolveFileUrl } from "@/lib/files";
import { useLibraryStore } from "@/stores/library";
import type { Asset, AssetKind } from "@/lib/types";

const KINDS: { key: string; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "background", label: "배경" },
  { key: "location", label: "장소" },
  { key: "prop", label: "소품" },
];

const KIND_LABEL: Record<string, string> = {
  background: "배경",
  location: "장소",
  prop: "소품",
};

export default function LibraryPage() {
  const s = useLibraryStore();
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    void s.init();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void s.load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.tab]);

  return (
    <main className="page">
      <div className="page-header">
        <h1>
          <Link href="/projects" className="small muted">
            ← 프로젝트
          </Link>
          <br />
          전역 라이브러리
        </h1>
        <div className="row">
          <label className="small muted">기준 프로젝트</label>
          <select
            className="select"
            style={{ width: "auto" }}
            value={s.projectId ?? ""}
            onChange={(e) => s.setProjectId(e.target.value)}
          >
            {s.projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}
              </option>
            ))}
          </select>
          <button
            className="btn btn-primary"
            onClick={() => fileRef.current?.click()}
            disabled={s.uploading}
          >
            {s.uploading ? "업로드 중…" : "업로드"}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".png,.jpg,.jpeg,.webp,.mp4,.wav,.mp3"
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void s.upload(f);
              e.target.value = "";
            }}
          />
        </div>
      </div>

      <div className="notice">
        업로드 시 저장 위치와 종류를 선택하세요. 저장 위치를 "전역"으로 하면
        project_id 없이 올려 모든 프로젝트에서 재사용할 수 있습니다.
        (20MB 제한 · png/jpg/webp/mp4/wav/mp3)
      </div>

      <div className="row" style={{ gap: 16, marginBottom: 12, flexWrap: "wrap" }}>
        <div className="field" style={{ marginBottom: 0 }}>
          <label>종류</label>
          <select
            className="select"
            style={{ width: "auto" }}
            value={s.uploadKind}
            onChange={(e) => s.setUploadKind(e.target.value as AssetKind)}
          >
            <option value="background">배경</option>
            <option value="location">장소</option>
            <option value="prop">소품</option>
          </select>
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label>저장 위치</label>
          <select
            className="select"
            style={{ width: "auto" }}
            value={s.uploadTarget}
            onChange={(e) => s.setUploadTarget(e.target.value)}
          >
            <option value="global">전역 (모든 프로젝트)</option>
            {s.projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="tabs">
        <button
          className={s.tab === "assets" ? "tab active" : "tab"}
          onClick={() => s.setTab("assets")}
        >
          에셋
        </button>
        <button
          className={s.tab === "characters" ? "tab active" : "tab"}
          onClick={() => s.setTab("characters")}
        >
          전역 캐릭터
        </button>
      </div>

      {s.tab === "assets" && (
        <section aria-label="에셋">
          <div className="row" style={{ marginBottom: 12 }}>
            {KINDS.map((k) => (
              <button
                key={k.key}
                className={s.kindFilter === k.key ? "btn btn-primary" : "btn"}
                onClick={() => s.setKindFilter(k.key)}
              >
                {k.label}
              </button>
            ))}
          </div>
          {s.loading && <p className="muted">불러오는 중…</p>}
          {!s.loading && s.assets.length === 0 && (
            <div className="empty-state">
              <p>에셋을 업로드하면 모든 프로젝트에서 재사용할 수 있습니다.</p>
            </div>
          )}
          <div className="grid-cards">
            {s.assets.map((a) => (
              <AssetCard key={a.id} asset={a} onDelete={() => void s.deleteAsset(a)} />
            ))}
          </div>
        </section>
      )}

      {s.tab === "characters" && (
        <section aria-label="전역 캐릭터">
          <div className="notice">
            전역 캐릭터는 읽기 전용입니다. 샷 레퍼런스 선택 시 전역 캐릭터가
            자동으로 노출되므로 별도의 가져오기 동작은 없습니다.
          </div>
          <div className="grid-cards">
            {s.globalCharacters.map((c) => (
              <div key={c.id} className="card">
                {c.master_thumbnail ? (
                  <img
                    src={resolveFileUrl(c.master_thumbnail) ?? undefined}
                    alt={c.name}
                    className="thumb square"
                  />
                ) : (
                  <div className="placeholder-box square">마스터 없음</div>
                )}
                <h3 className="card-title" style={{ marginTop: 8 }}>
                  {c.name}
                </h3>
                <div className="row">
                  <span className="badge gray">v{c.version}</span>
                  <span className="small muted">
                    사용 샷 {c.referencing_shots ?? 0}개
                  </span>
                </div>
              </div>
            ))}
          </div>
          {!s.loading && s.globalCharacters.length === 0 && (
            <div className="empty-state">
              <p>전역 캐릭터가 없습니다.</p>
            </div>
          )}
        </section>
      )}

      <ToastStack />
    </main>
  );
}

function AssetCard({ asset, onDelete }: { asset: Asset; onDelete: () => void }) {
  const url = resolveFileUrl(asset.image_path);
  return (
    <div className="card">
      {url ? (
        <img src={url} alt={asset.name} className="thumb" />
      ) : (
        <div className="placeholder-box">이미지 서빙 준비 중</div>
      )}
      <h3 className="card-title" style={{ marginTop: 8 }}>
        {asset.name}
      </h3>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <span className="badge">{KIND_LABEL[asset.kind] ?? asset.kind}</span>
        <span className="small muted">{formatDate(asset.created_at)}</span>
      </div>
      {(asset.referencing_shots ?? 0) > 0 && (
        <p className="small muted">
          참조 중인 샷 {asset.referencing_shots}개
        </p>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <button
          className="btn btn-ghost small"
          style={{ color: "var(--danger)" }}
          onClick={onDelete}
        >
          삭제
        </button>
      </div>
    </div>
  );
}
