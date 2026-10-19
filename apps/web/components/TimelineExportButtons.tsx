// Export buttons for a timeline: FCP7 XML / CMX3600 EDL download.
// New file only — the parent wires it into the timeline page.
"use client";

import { timelineExportUrl } from "@/lib/bible-api";

export default function TimelineExportButtons({
  timelineId,
}: {
  timelineId: string;
}) {
  return (
    <div>
      <a href={timelineExportUrl(timelineId, "xml")} download>
        <button>FCP7 XML 내보내기</button>
      </a>
      <a href={timelineExportUrl(timelineId, "edl")} download>
        <button>EDL 내보내기</button>
      </a>
    </div>
  );
}
