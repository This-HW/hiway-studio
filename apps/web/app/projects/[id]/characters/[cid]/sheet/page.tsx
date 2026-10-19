// Character sheet: generate section grids, mark primary references.
// No approval concept: designating a primary reference IS the "use it live" choice.
"use client";

import { useParams } from "next/navigation";
import { useEffect } from "react";
import { formatEstimatedCost } from "@/lib/format";
import { resolveFileUrl } from "@/lib/files";
import {
  groupReferencesBySection,
  useCharacterSheetStore,
  type SheetOptions,
} from "@/stores/character-sheet";
import { toast } from "@/stores/toast";
import { useWorkspaceStore } from "@/stores/workspace";
import type { Reference } from "@/lib/types";

const OPTION_LABELS: { key: keyof SheetOptions; label: string }[] = [
  { key: "views", label: "턴어라운드 (4뷰)" },
  { key: "expressions", label: "표정 (7종)" },
  { key: "outfits", label: "의상 (기본+대체 2종)" },
  { key: "poses", label: "포즈 (3종)" },
];

export default function CharacterSheetPage() {
  const params = useParams();
  const characterId = params.cid as string;
  const s = useCharacterSheetStore();

  const eventSeq = useWorkspaceStore((st) => st.eventSeq);
  const jobs = useWorkspaceStore((st) => st.jobs);

  useEffect(() => {
    void s.init(characterId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [characterId]);

  // Watch SSE completions for pending sheet jobs.
  useEffect(() => {
    for (const jobId of s.pendingJobIds) {
      const job = jobs[jobId];
      if (!job) continue;
      if (job.status === "completed") {
        s.markJobDone(jobId, false);
      } else if (job.status === "failed") {
        s.markJobDone(jobId, true);
        toast(`시트 이미지 생성 실패: ${job.error_code ?? "알 수 없는 오류"}`, {
          kind: "error",
        });
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventSeq]);

  const sections = groupReferencesBySection(s.references);
  const total = s.doneJobIds.length + s.failedJobIds.length + s.pendingJobIds.length;
  const done = s.doneJobIds.length;

  return (
    <div>
      <div className="notice">
        주 레퍼런스 지정이 곧 실전 사용 선택입니다. 별도의 승인 절차는
        없습니다. 용도별(대표·표정·의상)로 주 레퍼런스를 지정하면 샷 생성 시
        자동 주입됩니다.
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="row">
            {s.character?.master_thumbnail_url ? (
              <img
                src={s.character.master_thumbnail_url}
                alt=""
                style={{
                  width: 56,
                  height: 56,
                  borderRadius: 8,
                  objectFit: "cover",
                }}
              />
            ) : (
              <div
                style={{
                  width: 56,
                  height: 56,
                  borderRadius: 8,
                  background: "#f3f4f6",
                }}
              />
            )}
            <div>
              <h1 className="shell-title">
                {s.character?.name ?? "불러오는 중…"}
              </h1>
              <span className="small muted">
                캐릭터 시트 · v{s.character?.version ?? "—"}
              </span>
            </div>
          </div>
          <button
            className="btn btn-primary"
            onClick={() => void s.generateSheet()}
            disabled={s.generating}
          >
            {s.generating ? "요청 중…" : "시트 생성"}
          </button>
        </div>

        <div className="row" style={{ marginTop: 12, flexWrap: "wrap" }}>
          {OPTION_LABELS.map((o) => (
            <label key={o.key} className="row small" style={{ gap: 6 }}>
              <input
                type="checkbox"
                checked={s.options[o.key]}
                onChange={(e) => s.setOption(o.key, e.target.checked)}
              />
              {o.label}
            </label>
          ))}
        </div>

        {s.estimatedCost !== null && (
          <p className="small muted" style={{ marginTop: 8 }}>
            예상 비용 {formatEstimatedCost(s.estimatedCost)}
          </p>
        )}

        {total > 0 && (
          <div style={{ marginTop: 8 }}>
            <div className="small muted">
              진행률: 완료 {done} / 전체 {total}
              {s.failedJobIds.length > 0 && (
                <span> (실패 {s.failedJobIds.length})</span>
              )}
            </div>
            <div className="progress-bar">
              <div style={{ width: `${(done / total) * 100}%` }} />
            </div>
          </div>
        )}
      </div>

      {s.loading && <p className="muted">불러오는 중…</p>}

      {!s.loading && s.references.length === 0 && total === 0 && (
        <div className="empty-state">
          <p>시트를 생성하면 캐릭터의 모든 각도를 레퍼런스로 쓸 수 있습니다.</p>
        </div>
      )}

      {sections.map(
        (sec) =>
          sec.items.length > 0 && (
            <section key={sec.key} style={{ marginBottom: 20 }}>
              <h2 className="section-title">
                {sec.title} ({sec.items.length})
              </h2>
              <div
                className="grid-cards"
                style={{ gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))" }}
              >
                {sec.items.map((r) => (
                  <ReferenceCard
                    key={r.id}
                    ref_={r}
                    onToggle={() => void s.togglePrimary(r)}
                  />
                ))}
              </div>
            </section>
          )
      )}
    </div>
  );
}

function ReferenceCard({
  ref_,
  onToggle,
}: {
  ref_: Reference;
  onToggle: () => void;
}) {
  const url = resolveFileUrl(ref_.image_path);
  return (
    <div className="card">
      {url ? (
        <img src={url} alt={ref_.role} className="thumb square" />
      ) : (
        <div className="placeholder-box square">이미지 서빙 준비 중</div>
      )}
      <div className="row" style={{ marginTop: 6, flexWrap: "wrap" }}>
        <span className="badge gray">{ref_.role}</span>
        {ref_.is_primary && <span className="badge green">주 레퍼런스</span>}
      </div>
      <button
        className="btn btn-ghost small"
        style={{ marginTop: 6 }}
        onClick={onToggle}
      >
        {ref_.is_primary ? "주 지정 해제" : "주 레퍼런스로 지정"}
      </button>
    </div>
  );
}
