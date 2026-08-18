import prisma from "@/lib/prisma";
import { Prisma } from "@/generated/prisma/client";
import { requireRole } from "@/lib/auth";

// Gmail連携(src/gmail_sync/)が書き込むだけで表示画面が無かった`EmailLog`の一覧画面
// (2026-08-18新設)。件名・スニペットという実際のメール内容の断片を横断的に閲覧できて
// しまうため、audit-log/page.tsxと同じ理由(誰の・どのやり取りかという個人単位の
// コミュニケーション履歴そのもの)でmaster限定とする。
export const dynamic = "force-dynamic";

const DIRECTION_LABELS: Record<string, string> = { inbound: "受信", outbound: "送信" };
const DIRECTION_OPTIONS = Object.keys(DIRECTION_LABELS);

const INCIDENT_PRIORITY_LABELS: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
};
const INCIDENT_PRIORITY_OPTIONS = Object.keys(INCIDENT_PRIORITY_LABELS);

const MAX_ROWS = 200;

function singleParam(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

function formatDateTime(value: Date): string {
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(value);
}

function priorityBadgeClass(priority: string | null): string {
  // globals.cssに定義済みの3種(badge-blue/badge-gold/badge-muted、いずれも自己完結
  // した単独クラス)のみを使う。専用の「危険」色チップは無いため、badge-goldを
  // 最も注意を引く色として"high"に割り当てている。
  if (priority === "high") return "badge-gold";
  if (priority === "medium") return "badge-blue";
  return "badge-muted";
}

export default async function EmailLogPage(props: PageProps<"/email-log">) {
  await requireRole("master");

  const searchParams = await props.searchParams;
  const direction = singleParam(searchParams.direction);
  const incidentPriority = singleParam(searchParams.incidentPriority);
  const contactEmail = singleParam(searchParams.contactEmail);
  const from = singleParam(searchParams.from);
  const to = singleParam(searchParams.to);

  const where: Prisma.EmailLogWhereInput = {};
  if (direction) where.direction = direction;
  if (incidentPriority) where.incidentPriority = incidentPriority;
  if (contactEmail) where.contactEmail = { contains: contactEmail, mode: "insensitive" };
  if (from || to) {
    where.sentAt = {
      // 日付単体(YYYY-MM-DD)はJSTの1日として扱う(audit-log/page.tsxと同じ方針)。
      ...(from ? { gte: new Date(`${from}T00:00:00+09:00`) } : {}),
      ...(to ? { lte: new Date(`${to}T23:59:59.999+09:00`) } : {}),
    };
  }

  const logs = await prisma.emailLog.findMany({
    where,
    orderBy: { sentAt: "desc" },
    take: MAX_ROWS,
  });

  return (
    <div>
      <h1 className="page-title">メールログ</h1>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        Gmail連携(営業担当者ごとの個人接続)で同期された、Notion連絡先とのメールのやり取り履歴です。返信速度・返信の有無はMA(Web接客ツール)のリードスコアリングにも連携されています。
      </p>
      {logs.length === MAX_ROWS && (
        <p className="alert-warning mt-3">
          表示件数の上限({MAX_ROWS}件)に達しています。実際にはこれ以上のメールがある可能性があります。条件で絞り込んでください。
        </p>
      )}

      <form className="surface-card mt-6 flex flex-wrap items-end gap-3 p-4" method="get">
        <label className="flex flex-col gap-1 text-xs text-(--color-foreground)/60">
          送受信
          <select name="direction" defaultValue={direction} className="input">
            <option value="">すべて</option>
            {DIRECTION_OPTIONS.map((key) => (
              <option key={key} value={key}>
                {DIRECTION_LABELS[key]}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs text-(--color-foreground)/60">
          インシデント優先度
          <select name="incidentPriority" defaultValue={incidentPriority} className="input">
            <option value="">すべて</option>
            {INCIDENT_PRIORITY_OPTIONS.map((key) => (
              <option key={key} value={key}>
                {INCIDENT_PRIORITY_LABELS[key]}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs text-(--color-foreground)/60">
          連絡先メールアドレス
          <input
            type="text"
            name="contactEmail"
            defaultValue={contactEmail}
            placeholder="部分一致"
            className="input"
          />
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
        {(direction || incidentPriority || contactEmail || from || to) && (
          <a href="/email-log" className="link text-xs">
            条件をクリア
          </a>
        )}
      </form>

      <div className="surface-card mt-6 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-(--border-subtle) bg-(--color-surface-muted)/60 text-left text-(--color-foreground)/50">
                <th className="px-4 py-2 font-medium">日時</th>
                <th className="px-4 py-2 font-medium">送受信</th>
                <th className="px-4 py-2 font-medium">連絡先</th>
                <th className="px-4 py-2 font-medium">担当者</th>
                <th className="px-4 py-2 font-medium">件名</th>
                <th className="px-4 py-2 font-medium">インシデント</th>
              </tr>
            </thead>
            <tbody>
              {logs.length === 0 ? (
                <tr>
                  <td className="px-4 py-3 text-(--color-foreground)/50" colSpan={6}>
                    該当するメールがありません
                  </td>
                </tr>
              ) : (
                logs.map((log) => (
                  <tr key={log.id} className="border-b border-(--border-subtle) align-top last:border-0">
                    <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                      {formatDateTime(log.sentAt)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                      {DIRECTION_LABELS[log.direction] ?? log.direction}
                    </td>
                    <td className="px-4 py-2 text-(--color-foreground)/80">
                      <a
                        href={`https://www.notion.so/${log.contactPageId.replace(/-/g, "")}`}
                        target="_blank"
                        rel="noreferrer"
                        className="link"
                      >
                        {log.contactEmail}
                      </a>
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">{log.repEmail}</td>
                    <td className="px-4 py-2 text-(--color-foreground)/80">
                      {log.subject ?? <span className="text-(--color-foreground)/40">(件名なし)</span>}
                      {log.snippet && (
                        <p className="mt-0.5 line-clamp-2 text-xs text-(--color-foreground)/50">{log.snippet}</p>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2">
                      {log.incidentPriority ? (
                        <span className={priorityBadgeClass(log.incidentPriority)}>
                          {INCIDENT_PRIORITY_LABELS[log.incidentPriority] ?? log.incidentPriority}
                          {typeof log.incidentScore === "number" && (
                            <span className="ml-1 opacity-70">({log.incidentScore})</span>
                          )}
                        </span>
                      ) : (
                        <span className="text-(--color-foreground)/30">-</span>
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
