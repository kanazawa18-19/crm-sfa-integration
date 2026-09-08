import { getIntegrationDiagnostic } from "@/lib/backend";
import type { DiagnosticResult, IntegrationTarget } from "@/lib/integrationDiagnostics";

const DB_LABELS = { client_master: "取引先", chain: "チェーン", contact: "連絡先", project: "案件", product: "商品", action: "アクション" } as const;
const SOURCE_LABELS = { notion: "Notion", kintone: "kintone", zoho: "Zoho", spreadsheet: "シート" } as const;
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("診断形式不正");
  return value as Record<string, unknown>;
}
function count(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) throw new Error("件数不正");
  return value;
}
function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new Error("数値不正");
  return value;
}
function timestamp(value: unknown): string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/.test(value)) throw new Error("日時不正");
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error("日時不正");
  return date.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" }) + "（日本時間）";
}

// 生のdetail/extra/例外文は返さない。固定キーと型の確認を通った集計値だけを組み立てる。
export function sanitizeDiagnostic(target: IntegrationTarget, payload: unknown, elapsedMs: number): DiagnosticResult {
  const body = record(payload);
  if (!Array.isArray(body.results) || body.results.length !== 1) throw new Error("診断結果不足");
  const result = record(body.results[0]);
  if (result.name !== target || (typeof result.status !== "string" || !["ok", "failed", "not_configured"].includes(result.status))) throw new Error("診断結果不正");
  count(result.elapsed_ms);
  const status = result.status as "ok" | "failed" | "not_configured";
  const facts: DiagnosticResult["facts"] = [];
  if ((status !== "not_configured" || target === "spreadsheet_row_creation") && ["spreadsheet_row_creation", "webhook_receipts", "spreadsheet_outbox"].includes(target)) {
    // 通信例外でfailedの場合はextraが無い。未確認へ倒し、欠損を0件と扱わない。
    const extra = record(result.extra);
    if (target === "spreadsheet_row_creation") {
      const perDb = record(extra.per_db);
      for (const [key, label] of Object.entries(DB_LABELS)) {
        if (typeof perDb[key] !== "boolean") throw new Error("設定不足");
        facts.push({ label, value: perDb[key] ? "設定上許可" : "無効" });
      }
      facts.push({ label: "不明な対象指定", value: `${count(extra.unknown_db_keys_count)}件` });
      if (typeof extra.wildcard !== "boolean") throw new Error("設定不足");
      facts.push({ label: "今後追加する対象も許可", value: extra.wildcard ? "はい" : "いいえ" });
    }
    if (target === "webhook_receipts") {
      const sources = record(extra.sources);
      for (const [key, label] of Object.entries(SOURCE_LABELS)) {
        if (!Object.hasOwn(sources, key)) { facts.push({ label, value: "記録開始以降の受信なし" }); continue; }
        const source = record(sources[key]);
        facts.push({ label, value: `${count(source.count)}回・最終 ${timestamp(source.last_received_at)}・${number(source.hours_since)}時間前` });
      }
    }
    if (target === "spreadsheet_outbox") {
      const byStatus = record(extra.by_status);
      // 既存SQLはGROUP BY集計で、0件の状態キーを返さない。キー省略は0件の契約。
      // by_status自体や滞留日数の欠損は別で、形式不正として未確認にする。
      for (const [key, label] of Object.entries({ pending: "未処理", done: "処理完了（行作成済みとは限りません）", failed: "再試行打ち切り" })) {
        const entry = Object.hasOwn(byStatus, key) ? record(byStatus[key]) : null;
        facts.push({ label, value: `${entry ? count(entry.count) : 0}件` });
      }
      if (extra.pending_oldest_age_days !== null) facts.push({ label: "最も古い未処理", value: `${number(extra.pending_oldest_age_days)}日前` });
    }
  }
  return { target, status, checkedAt: new Date().toISOString(), elapsedMs, facts, message: status === "ok" ? "この診断範囲の確認に成功しました。同期や通知の到達を保証するものではありません。" : status === "failed" ? target === "spreadsheet_outbox" ? "再試行の打ち切りや長期の滞留は管理担当者による復旧が必要です。再確認では再処理しません。" : "診断で問題を検出しました。管理担当者が認証・設定・実行記録を確認してください。" : "必要な設定がないか、機能が無効です。意図した設定か管理担当者に確認してください。" };
}

export async function runSafeDiagnostic(target: IntegrationTarget): Promise<DiagnosticResult> {
  const start = performance.now();
  try {
    const payload = await getIntegrationDiagnostic(target);
    return sanitizeDiagnostic(target, payload, Math.round(performance.now() - start));
  } catch {
    return { target, status: "unknown", checkedAt: new Date().toISOString(), elapsedMs: Math.round(performance.now() - start), facts: [], message: "時間切れ・接続失敗・応答形式の問題で確認できませんでした。時間をおいて再確認してください。" };
  }
}
