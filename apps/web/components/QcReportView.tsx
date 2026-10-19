// Shared QC report view (Phase 4, F-14). Used by the timeline render card
// and the project QC page.
"use client";

import type { QCReport } from "@/lib/types";

const STATUS_STYLE: Record<string, { label: string; cls: string }> = {
  pass: { label: "통과", cls: "green" },
  warn: { label: "경고", cls: "" },
  fail: { label: "실패", cls: "red" },
  pending: { label: "검사 중…", cls: "" },
};

const CHECK_LABELS: Record<string, string> = {
  text_ocr_match: "화면 텍스트 일치",
  text_artifact: "텍스트 깨짐 검사",
  subtitle_content: "자막 내용 일치",
  subtitle_timing: "자막 타이밍",
  subtitle_cps: "자막 CPS",
  subtitle_safe_area: "자막 안전 영역",
  continuity_first_frame: "첫 프레임 연속성",
  continuity_between_shots: "샷 간 연속성",
  audio_peak: "오디오 피크",
  audio_loudness: "오디오 음량",
  lipsync_drift: "더빙 립싱크",
  encoding_match: "인코딩 일치",
  black_frame: "블랙 프레임",
  story_fidelity: "스토리 충실도",
  performance_quality: "연기 품질",
};

export function QcReportView({ report }: { report: QCReport }) {
  const overall = STATUS_STYLE[report.overall] ?? STATUS_STYLE.pending;
  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10 }}>
        <span className="card-title" style={{ margin: 0 }}>QC 리포트</span>
        <span className={`badge ${overall.cls}`}>{overall.label}</span>
        <span className="small muted">버전 v{report.timeline_content_version}</span>
      </div>
      {report.overall === "pending" ? (
        <p className="small muted">검사가 진행 중입니다. 완료되면 자동으로 갱신됩니다.</p>
      ) : (
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--muted)" }}>
              <th style={{ padding: "4px 6px" }}>검사</th>
              <th style={{ padding: "4px 6px" }}>결과</th>
              <th style={{ padding: "4px 6px" }}>상세</th>
            </tr>
          </thead>
          <tbody>
            {report.checks.map((c) => {
              const st = STATUS_STYLE[c.status] ?? STATUS_STYLE.pending;
              return (
                <tr key={c.code} style={{ borderTop: "1px solid var(--border)" }}>
                  <td style={{ padding: "4px 6px" }}>
                    {CHECK_LABELS[c.code] ?? c.code}
                    {c.stub && (
                      <span className="badge" style={{ marginLeft: 6 }} title="엔진 미설치로 스텁 처리">
                        stub
                      </span>
                    )}
                  </td>
                  <td style={{ padding: "4px 6px" }}>
                    <span className={`badge ${st.cls}`}>{st.label}</span>
                  </td>
                  <td style={{ padding: "4px 6px", color: "var(--muted)", fontSize: 12 }}>
                    {c.details}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
