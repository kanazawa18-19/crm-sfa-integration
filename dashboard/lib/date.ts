// 「今日」の判定はサーバーの実行環境(Vercelは基本UTC)ではなくJSTで行う。UTCのまま
// new Date().toISOString().slice(0, 10)を使うと、JST 0:00〜8:59の間は前日の日付に
// なってしまう(2026-08-16、日報・マネージャー通知画面の「今日」ズレとして発覚)。
export function todayDateStringJst(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Tokyo" }).format(new Date());
}

// toLocaleString("ja-JP")だけでは書式(区切り文字)が日本語になるだけでタイムゾーンは
// サーバーの実行環境(Vercelは基本UTC)のままになる — timeZoneを明示してJST表示にする。
// settings/gmail・settings/drive・settings/googleの3画面で一字一句同じ実装が重複して
// いたため共通化した(2026-08-27、obasan-qualityレビュー指摘)。
export function formatJst(date: Date): string {
  return date.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" });
}
