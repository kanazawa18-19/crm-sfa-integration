import type { Prisma } from "@/generated/prisma/client";

// audit-log/page.tsxとclients/[id]/page.tsxの両方でAuditLog表示に使う共通フォーマッタ
// (obasan-qualityレビューWARN対応、2026-08-18。元は両ファイルに同一実装がコピーされていた)。

export function formatDateTime(value: Date): string {
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(value);
}

export function formatChangedFields(changedFields: Prisma.JsonValue): string {
  if (
    typeof changedFields !== "object" ||
    changedFields === null ||
    Array.isArray(changedFields)
  ) {
    return String(changedFields);
  }
  return Object.entries(changedFields as Record<string, unknown>)
    .map(([name, diff]) => {
      const before = diff && typeof diff === "object" ? (diff as Record<string, unknown>).before : undefined;
      const after = diff && typeof diff === "object" ? (diff as Record<string, unknown>).after : undefined;
      return `${name}: ${JSON.stringify(before)} → ${JSON.stringify(after)}`;
    })
    .join("\n");
}
