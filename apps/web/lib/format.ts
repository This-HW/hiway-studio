// Display formatting helpers (Korean UI text, USD money rules per §0.3).

/** `~$1.24 (예상)` — estimated cost with tilde prefix and 2 decimals. */
export function formatEstimatedCost(usd: number | string | null | undefined): string {
  const n = typeof usd === "string" ? Number(usd) : usd;
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `~$${n.toFixed(2)} (예상)`;
}

/** `$1.18 (실제)` — actual cost, no prefix, 2 decimals. */
export function formatActualCost(usd: number | string | null | undefined): string {
  const n = typeof usd === "string" ? Number(usd) : usd;
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `$${n.toFixed(2)} (실제)`;
}

/** Project-card monthly summary: estimated + actual on one line. */
export function formatMonthlyCost(
  estimated: number | null | undefined,
  actual: number | null | undefined
): string {
  const est = estimated ?? 0;
  const act = actual ?? 0;
  return `~$${est.toFixed(2)} / $${act.toFixed(2)}`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export const ASPECT_RATIOS = ["16:9", "9:16", "1:1", "2.39:1", "4:5"] as const;

/** §0.4: Korean messages for the backend error codes Phase 1 screens handle. */
export function koreanErrorMessage(code: string, fallback: string): string {
  switch (code) {
    case "MODERATION_BLOCKED":
      return "제공자 정책으로 차단되었습니다. 프롬프트를 수정해 주세요.";
    case "BUDGET_EXCEEDED":
      return "월 예산을 초과했습니다. 설정 탭에서 예산을 상향해 주세요.";
    case "QUEUE_FULL":
      return "작업 큐가 가득 찼습니다. 잠시 후 다시 시도해 주세요.";
    case "APPROVAL_REQUIRED":
      return "승인이 필요합니다.";
    default:
      return fallback;
  }
}
