"use client";

export function PhasePlaceholder({ title }: { title: string }) {
  return (
    <div className="empty-state">
      <h2 className="section-title">{title}</h2>
      <p>이 화면은 Phase 2에서 제공됩니다.</p>
    </div>
  );
}
