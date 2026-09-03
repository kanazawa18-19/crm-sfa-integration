import type { ReplyTiming, ReplyTimingConfidence } from "@/lib/backend";

// 顧客360度ビューの連絡先テーブルに出す「返信までの時間」「返ってきやすい時間帯」の
// 表示文字列を組み立てる(2026-09-03)。page.tsxのJSXから条件分岐を追い出して、
// lib/__tests__の既存パターンでテストできるようにしている。
//
// **数字の横に必ず件数を出す**のがこのファイルの一番の役目。何件から出した数字かを
// 隠すと、1〜2件の偶然を断定として読ませてしまう(src/analytics/reply_timing.py参照)。

// 件数の注記。`high`(十分)のときだけ、件数以外の言葉を足さない
// (「十分」と書いても読み手の判断は変わらないため、表を狭く保つ)。
export function sampleCountLabel(
  count: number,
  confidence: ReplyTimingConfidence,
  confidenceLabel: string
): string {
  if (confidence === "high") return `${count}件`;
  return `${count}件・${confidenceLabel}`;
}

export interface ReplyCellText {
  value: string;
  sample: string;
  title: string;
}

// 返信までの時間。中央値を主役にする(平均は数件の長期放置に引っ張られるため)。
// 平均・最速・最遅はホバー(title)へ逃がす。
export function replyLagCellText(timing: ReplyTiming | undefined): ReplyCellText | null {
  if (!timing || timing.sample_size === 0) return null;
  return {
    value: timing.median_lag_label,
    sample: sampleCountLabel(timing.sample_size, timing.confidence, timing.confidence_label),
    title:
      `返信${timing.sample_size}件の中央値。` +
      `平均 ${timing.mean_lag_label} / 最速 ${timing.fastest_lag_label} / 最遅 ${timing.slowest_lag_label}` +
      `｜${timing.note}`,
  };
}

export interface ReplyWindowCellText extends ReplyCellText {
  weekdays: string;
}

// 返ってきやすい時間帯。**返信ラグとはサンプル数が別物**(こちらは受信の総数、
// 向こうは送信→受信のペア数)なので、件数の意味をtitleにも書いておく。
export function replyWindowCellText(
  timing: ReplyTiming | undefined
): ReplyWindowCellText | null {
  const window = timing?.timing;
  if (!timing || !window || window.sample_size === 0) return null;
  return {
    // 受信が散らばっているときは順位に意味が無いので「傾向なし」と書く。
    // 同数の先頭を機械的にトップとして見せると、無い傾向をあると読ませてしまう。
    value: window.is_flat
      ? "傾向なし"
      : window.top_buckets.map((b) => b.label).join(" / ") || "-",
    weekdays: window.top_weekdays.join("・"),
    sample: sampleCountLabel(window.sample_size, window.confidence, window.confidence_label),
    title:
      `受信${window.sample_size}件の内訳から算出(時間帯は日本時間)。` +
      `こちらの件数は受信の総数で、返信までの時間の件数(返信ペア数)とは別物です。` +
      `｜${timing.note}`,
  };
}

// ログが1件も無いときにホバーで出す説明。バックエンドの`note`が無いケース
// (該当ページIDのエントリ自体が返らない)専用のフォールバック。
export const NO_EMAIL_LOG_TITLE = "この連絡先の送受信ログがまだありません。";
