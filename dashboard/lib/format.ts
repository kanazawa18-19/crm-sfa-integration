export function formatYen(value: number): string {
  return `¥${value.toLocaleString("ja-JP")}`;
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "-";
  }
  return `${(value * 100).toFixed(1)}%`;
}

// バックエンドから渡される"YYYY-MM-DD"形式の日付文字列を、非エンジニアにも読みやすい
// 日本語表記（例: "2026年12月1日"）に変換する。
export function formatDate(value: string): string {
  const [year, month, day] = value.split("-");
  return `${year}年${Number(month)}月${Number(day)}日`;
}

export function formatDateRange(start: string, end: string): string {
  return `${formatDate(start)} 〜 ${formatDate(end)}`;
}
