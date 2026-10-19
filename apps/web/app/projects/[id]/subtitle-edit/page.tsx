// Subtitle-driven edit analysis page (P1-6). Wraps the shared panel;
// the timeline tab can embed <SubtitleEditPanel> later without changes.
"use client";

import { useParams } from "next/navigation";
import { SubtitleEditPanel } from "@/components/SubtitleEditPanel";

export default function SubtitleEditPage() {
  const params = useParams();
  const projectId = params.id as string;
  return (
    <div>
      <SubtitleEditPanel projectId={projectId} />
    </div>
  );
}
