import prisma from "@/lib/prisma";
import { Prisma } from "@/generated/prisma/client";
import { requireRole } from "@/lib/auth";
import { formatDateTime, formatChangedFields } from "@/lib/auditLogFormat";

// バックエンド(Python)が書き込んだ最新のログを毎リクエスト取得するため、静的
// プリレンダリングを無効化する(他の一覧ページと同じ方針)。
export const dynamic = "force-dynamic";

// src/db_schema/registry.pyの6DBキーと表示ラベルの対応(02_DB構成一覧の並び順を踏襲)。
const DB_KEY_LABELS: Record<string, string> = {
  client_master: "取引先マスター",
  chain: "チェーン",
  contact: "連絡先",
  project: "案件管理",
  product: "サービス商品",
  action: "アクション履歴",
};
const DB_KEY_OPTIONS = Object.keys(DB_KEY_LABELS);

// docs/audit_log_note.md「actorSourceの伝播方式」の一覧と対応させる。
const ACTOR_SOURCE_LABELS: Record<string, string> = {
  kintone_webhook: "kintone",
  zoho_webhook: "Zoho",
  spreadsheet_webhook: "スプレッドシート",
  web_engagement_webhook: "Web接客ツール",
  lead_inquiry_webhook: "問い合わせメール調査(lead-researcher)",
  slack_interaction_webhook: "Slack承認",
  gmail_sync: "Gmail連携",
  migration: "一括移行",
  unknown: "不明(記録経路の設定漏れ)",
};
const ACTOR_SOURCE_OPTIONS = Object.keys(ACTOR_SOURCE_LABELS);

const ACTION_LABELS: Record<string, string> = { create: "作成", update: "更新" };

const MAX_ROWS = 200;

function singleParam(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

export default async function AuditLogPage(props: PageProps<"/audit-log">) {
  // 監査ログは6DB全体の変更履歴(氏名・メールアドレス・電話番号等のPIIを含む)を横断的に
  // 閲覧できてしまうため、users/settings/security等と同じ「master限定」の機微情報画面として
  // 扱う(shirokuma-secレビューBLOCKER対応、2026-08-17)。alerts/reports/members等の閲覧系
  // ページはviewer以上に開放しているが、それらは特定粒度の集計・案件単位の情報である一方、
  // 監査ログは「誰が・いつ・どのレコードの・どのフィールドを変更したか」という個人の行動履歴
  // そのものであり、社内でも閲覧者を絞るべき性質のデータと判断した。
  await requireRole("master");

  const searchParams = await props.searchParams;
  const dbKey = singleParam(searchParams.dbKey);
  const actorSource = singleParam(searchParams.actorSource);
  const from = singleParam(searchParams.from);
  const to = singleParam(searchParams.to);

  const where: Prisma.AuditLogWhereInput = {};
  if (dbKey) where.dbKey = dbKey;
  if (actorSource) where.actorSource = actorSource;
  if (from || to) {
    where.createdAt = {
      // 日付単体(YYYY-MM-DD)はJSTの1日として扱う(DatePicker等、他画面のJST基準に合わせる)。
      ...(from ? { gte: new Date(`${from}T00:00:00+09:00`) } : {}),
      ...(to ? { lte: new Date(`${to}T23:59:59.999+09:00`) } : {}),
    };
  }

  const logs = await prisma.auditLog.findMany({
    where,
    orderBy: { createdAt: "desc" },
    take: MAX_ROWS,
  });

  return (
    <div>
      <h1 className="page-title">データ監査ログ</h1>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        Notionへの書き込み(取引先マスター/連絡先/案件管理/アクション履歴/サービス商品/チェーンの6DB)のうち、このバックエンド(kintone/Zoho連携・Web接客ツール連携・Gmail連携・Slack承認・一括移行)を経由したものだけを自動記録しています。
      </p>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        以下は原理的に対象外です: (1)Notion管理画面から人間が直接編集した変更(このコードを経由しないため、Notion自体にフィールド単位の変更履歴を返すAPIが無く技術的に捕捉不可能)。(2)「申込書・契約書」「見積書」「名刺交換日」等、Any-to-Any同期の対象外(NOTION_ONLY)として登録されているプロパティへの変更(このバックエンドが書き込む経路自体が存在しないため)。
      </p>
      {logs.length === MAX_ROWS && (
        <p className="alert-warning mt-3">
          表示件数の上限({MAX_ROWS}件)に達しています。実際にはこれ以上の変更があった可能性があります。対象DB・書き込み経路・日付範囲で絞り込んでください。
        </p>
      )}

      <form className="surface-card mt-6 flex flex-wrap items-end gap-3 p-4" method="get">
        <label className="flex flex-col gap-1 text-xs text-(--color-foreground)/60">
          対象DB
          <select name="dbKey" defaultValue={dbKey} className="input">
            <option value="">すべて</option>
            {DB_KEY_OPTIONS.map((key) => (
              <option key={key} value={key}>
                {DB_KEY_LABELS[key]}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs text-(--color-foreground)/60">
          書き込み経路
          <select name="actorSource" defaultValue={actorSource} className="input">
            <option value="">すべて</option>
            {ACTOR_SOURCE_OPTIONS.map((key) => (
              <option key={key} value={key}>
                {ACTOR_SOURCE_LABELS[key]}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs text-(--color-foreground)/60">
          開始日
          <input type="date" name="from" defaultValue={from} className="input" />
        </label>

        <label className="flex flex-col gap-1 text-xs text-(--color-foreground)/60">
          終了日
          <input type="date" name="to" defaultValue={to} className="input" />
        </label>

        <button type="submit" className="btn-primary">
          絞り込む
        </button>
        {(dbKey || actorSource || from || to) && (
          <a href="/audit-log" className="link text-xs">
            条件をクリア
          </a>
        )}
      </form>

      <div className="surface-card mt-6 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr className="bg-(--color-surface-muted)/60">
                <th>日時</th>
                <th>対象DB</th>
                <th>操作</th>
                <th>対象ページ</th>
                <th>変更内容</th>
                <th>経路</th>
              </tr>
            </thead>
            <tbody>
              {logs.length === 0 ? (
                <tr>
                  <td colSpan={6}>
                    該当するログがありません
                  </td>
                </tr>
              ) : (
                logs.map((log) => (
                  <tr key={log.id} className="align-top">
                    <td className="whitespace-nowrap">
                      {formatDateTime(log.createdAt)}
                    </td>
                    <td className="whitespace-nowrap">
                      {DB_KEY_LABELS[log.dbKey] ?? log.dbKey}
                    </td>
                    <td className="whitespace-nowrap">
                      {ACTION_LABELS[log.action] ?? log.action}
                    </td>
                    <td className="font-mono text-xs">
                      <a
                        href={`https://www.notion.so/${log.notionPageId.replace(/-/g, "")}`}
                        target="_blank"
                        rel="noreferrer"
                        className="link"
                      >
                        {log.notionPageId}
                      </a>
                    </td>
                    <td className="whitespace-pre-wrap text-xs text-(--color-foreground)/70">
                      {formatChangedFields(log.changedFields)}
                    </td>
                    <td className="whitespace-nowrap">
                      {ACTOR_SOURCE_LABELS[log.actorSource] ?? log.actorSource}
                      {log.actorLabel && (
                        <span className="ml-1 text-(--color-foreground)/50">({log.actorLabel})</span>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
