// この一覧はバックエンドの PROBES に対応する。URLを利用者から受け取らない。
export const INTEGRATION_TARGETS = [
  { id: "postgres", label: "データベース", scope: "接続して読み取りできるか" },
  { id: "advisory_lock", label: "二重実行の防止", scope: "診断専用の鍵を取得・解放できるか" },
  { id: "notion", label: "Notion", scope: "連携先のデータベースを参照できるか" },
  { id: "kintone", label: "kintone", scope: "連携先のアプリを参照できるか" },
  { id: "zoho", label: "Zoho CRM", scope: "連携先の機能を参照できるか" },
  { id: "google_auth", label: "Google認証", scope: "認証に必要なアクセス権を取得できるか" },
  { id: "spreadsheet", label: "スプレッドシート", scope: "同期先のシートとデータ行が存在するか" },
  { id: "spreadsheet_row_creation", label: "シートの行作成設定", scope: "各データの行作成が設定上許可されているか" },
  { id: "slack", label: "Slack", scope: "通知用の認証が通るか（通知は送信しません）" },
  { id: "web_engagement_tool", label: "MA", scope: "トップページが応答するか（認証・同期は対象外）" },
  { id: "webhook_receipts", label: "変更通知の受信記録", scope: "各サービスからの最終受信と受信回数" },
  { id: "spreadsheet_outbox", label: "シート行作成の待ち状況", scope: "未処理・完了・再試行打ち切りの件数" },
] as const;
export type IntegrationTarget = typeof INTEGRATION_TARGETS[number]["id"];
export type DiagnosticStatus = "ok" | "failed" | "not_configured" | "unknown";
export interface DiagnosticResult {
  target: IntegrationTarget;
  status: DiagnosticStatus;
  message: string;
  checkedAt: string;
  elapsedMs: number;
  facts: Array<{ label: string; value: string }>;
}
export function isIntegrationTarget(value: unknown): value is IntegrationTarget {
  return INTEGRATION_TARGETS.some((target) => target.id === value);
}
export const STATUS_LABELS: Record<DiagnosticStatus, string> = {
  ok: "正常（診断範囲内）", failed: "要確認", not_configured: "未設定・無効", unknown: "未確認",
};

export const SUCCESS_LABELS: Record<IntegrationTarget, string> = {
  postgres: "接続確認済み", advisory_lock: "鍵の取得確認済み", notion: "参照確認済み",
  kintone: "設定済み対象の参照確認済み", zoho: "参照確認済み", google_auth: "認証確認済み",
  spreadsheet: "シート・行の存在確認済み", spreadsheet_row_creation: "設定上許可",
  slack: "認証確認済み", web_engagement_tool: "応答確認済み",
  webhook_receipts: "記録取得済み", spreadsheet_outbox: "滞留異常なし",
};
