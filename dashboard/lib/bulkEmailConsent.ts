// 「送ってよい根拠」の種類の定義と入力検証(2026-09-03)。
//
// ★正本は src/bulk_email/consent.py の BASIS_LABELS。
// こちら側はブラウザから来た値を検証するためだけに持つ。値の一致は
// tests/bulk_email/test_consent.py が両ファイルを突き合わせて固定している
// (コメントで「両方直すこと」と書くだけでは守れないため)。
//
// 判定(有効か・古いか)はこちらには置かない。Pythonの consent.py が唯一の判定者。

export const CONSENT_BASES = ["opt_in", "notified", "transaction", "published"] as const;

export type ConsentBasis = (typeof CONSENT_BASES)[number];

export function isConsentBasis(value: unknown): value is ConsentBasis {
  return typeof value === "string" && (CONSENT_BASES as readonly string[]).includes(value);
}

// 連絡先ページIDの正規化。ContactMailPreference と同じ形(ハイフン無し・小文字の32桁hex)で
// 保存する。DB側にも同じ形のCHECK制約がある。
// bulkEmailUnsubscribe.ts が同じ正規化を持っているが、あちらは配信停止リンクの署名検証用で
// 用途が違うため、import して結合させずにそれぞれで完結させている。
export function normalizeContactPageIdForConsent(value: string): string {
  return (value ?? "").trim().toLowerCase().replace(/-/g, "");
}

export function isNormalizedContactPageId(value: string): boolean {
  return /^[0-9a-f]{32}$/.test(value);
}

// 業務上の「今日」を決めるタイムゾーン(JST)。src/bulk_email/consent.py の
// BUSINESS_TIMEZONE と同じ基準にそろえる。
const BUSINESS_UTC_OFFSET_MINUTES = 9 * 60;

/** 業務上の今日を "YYYY-MM-DD" で返す。 */
export function businessToday(now: Date = new Date()): string {
  const shifted = new Date(now.getTime() + BUSINESS_UTC_OFFSET_MINUTES * 60 * 1000);
  return shifted.toISOString().slice(0, 10);
}

/**
 * 取得日の検証。**時刻ではなく暦の日として扱う。**
 *
 * 以前は `Date` に変換して「今+24時間」と比べていたが、これは「今日まで」ではない。
 * 日本時間の午前中には翌日の日付が24時間以内に入るため**明日を登録できてしまい**、
 * しかもその日のうちは Python 側が未来日として弾くので、翌日になった瞬間に
 * 人の再確認なしで有効化される、という壊れ方をしていた
 * (ChatGPT・Gemini が独立に指摘、2026-09-03)。
 *
 * 文字列のまま業務上の今日と比べれば、タイムゾーンにも `Date` の日付正規化
 * (2026-02-31 が 3月に繰り上がる等)にも振り回されない。
 *
 * 3年より古い根拠は警告になるだけで、登録は妨げない
 * (古い名刺が根拠として無効、と機械が決められる話ではない)。
 */
export function parseObtainedAt(value: string, now: Date = new Date()): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const [year, month, day] = value.split("-").map(Number);
  // 実在しない日付(2026-02-31 など)を弾く。Date の正規化に頼ると別の日として通る。
  const probe = new Date(Date.UTC(year, month - 1, day));
  if (
    probe.getUTCFullYear() !== year ||
    probe.getUTCMonth() !== month - 1 ||
    probe.getUTCDate() !== day
  ) {
    return null;
  }
  if (value > businessToday(now)) return null;
  return value;
}
