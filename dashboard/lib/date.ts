// 「今日」の判定はサーバーの実行環境(Vercelは基本UTC)ではなくJSTで行う。UTCのまま
// new Date().toISOString().slice(0, 10)を使うと、JST 0:00〜8:59の間は前日の日付に
// なってしまう(2026-08-16、日報・マネージャー通知画面の「今日」ズレとして発覚)。
export function todayDateStringJst(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Tokyo" }).format(new Date());
}
