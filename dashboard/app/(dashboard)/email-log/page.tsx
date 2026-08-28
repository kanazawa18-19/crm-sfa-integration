import prisma from "@/lib/prisma";
import { Prisma } from "@/generated/prisma/client";
import { requireRole } from "@/lib/auth";
import { parseEmailLogQuery, EMAIL_LOG_SEARCH_HELP, type IgnoredTerm } from "@/lib/emailLogSearch";
import { gmailMessageUrl } from "@/lib/gmailLink";

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

// 「直近100件を表示し、残りはページネーションで辿れるように」という要望
// (2026-08-26)に基づく1ページあたりの件数。
const PAGE_SIZE = 100;

function singleParam(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

// ページネーションリンク生成用。`page`以外の現在の検索条件を維持したまま
// ページ番号だけを差し替える。
function pageHref(searchParams: Record<string, string | string[] | undefined>, page: number): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(searchParams)) {
    if (key === "page") continue;
    if (Array.isArray(value)) {
      for (const v of value) params.append(key, v);
    } else if (value) {
      params.set(key, value);
    }
  }
  if (page > 1) params.set("page", String(page));
  const qs = params.toString();
  return qs ? `/email-log?${qs}` : "/email-log";
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
  const q = singleParam(searchParams.q);
  const pageParam = Number.parseInt(singleParam(searchParams.page), 10);
  const requestedPage = Number.isFinite(pageParam) && pageParam > 0 ? pageParam : 1;

  // parseEmailLogQueryは基本的に例外を投げない設計だが(不正な入力はignoredTermsに
  // 積んで無視する)、shirokuma-secレビューBLOCKER対応(2026-08-26)として万一の
  // パース失敗でもServer Component全体を巻き込んで500にしない防御を多層化する
  // (根本対策はemailLogSearch.ts側のネスト深さ上限。これは保険)。
  let searchWhere: Prisma.EmailLogWhereInput | null = null;
  let ignoredTerms: IgnoredTerm[] = [];
  let queryTruncated = false;
  let searchParseFailed = false;
  try {
    const parsed = parseEmailLogQuery(q);
    searchWhere = parsed.where;
    ignoredTerms = parsed.ignoredTerms;
    queryTruncated = parsed.truncated;
  } catch {
    searchParseFailed = true;
  }

  const where: Prisma.EmailLogWhereInput = {};
  if (direction) where.direction = direction;
  if (incidentPriority) where.incidentPriority = incidentPriority;
  if (searchWhere) where.AND = [searchWhere];

  // 総件数が分かってからページ番号をクランプする(shirokuma-secレビューWARN対応、
  // 2026-08-26)。以前はrequestedPageをそのままskip計算に使っていたため、総ページ数を
  // 超えるpageを指定すると「全50件中 99801-50件を表示」のような矛盾表示になっていた。
  const totalCount = await prisma.emailLog.count({ where });
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const page = Math.min(requestedPage, totalPages);

  const logs = await prisma.emailLog.findMany({
    where,
    orderBy: { sentAt: "desc" },
    skip: (page - 1) * PAGE_SIZE,
    take: PAGE_SIZE,
  });

  const unsupportedTerms = ignoredTerms.filter((t) => t.reason === "unsupported");
  const invalidValueTerms = ignoredTerms.filter((t): t is Extract<IgnoredTerm, { reason: "invalidValue" }> => t.reason === "invalidValue");

  return (
    <div>
      <h1 className="page-title">メールログ</h1>
      <p className="mt-1 text-sm text-(--color-foreground)/60">
        Gmail連携(営業担当者ごとの個人接続)で同期された、Notion連絡先とのメールのやり取り履歴です。返信速度・返信の有無はMA(Web接客ツール)のリードスコアリングにも連携されています。
      </p>
      {searchParseFailed && (
        <p className="alert-warning mt-3">
          検索条件の解析に失敗したため、検索条件なしの一覧を表示しています。検索ボックスの内容を見直してください(送受信・インシデント優先度の絞り込みは適用されています)。
        </p>
      )}
      {unsupportedTerms.length > 0 && (
        <p className="alert-warning mt-3">
          以下の検索条件は現在対応していないため無視されました(下の一覧はこれらの条件で絞り込まれていません): {unsupportedTerms.map((t) => t.raw).join(", ")}
        </p>
      )}
      {invalidValueTerms.length > 0 && (
        <p className="alert-warning mt-3">
          以下の検索条件は値の形式が正しくないため無視されました(下の一覧はこれらの条件で絞り込まれていません):{" "}
          {invalidValueTerms.map((t) => `${t.raw}(${t.hint})`).join(" / ")}
        </p>
      )}
      {queryTruncated && (
        <p className="alert-warning mt-3">
          検索条件が長すぎるため、先頭の一部のみを検索条件として使用しました。条件を絞ってください。
        </p>
      )}

      <form className="surface-card mt-6 flex flex-col gap-3 p-4" method="get">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-1 min-w-64 flex-col gap-1 text-xs text-(--color-foreground)/60">
            検索(Gmail風の演算子が使えます)
            <input
              type="text"
              name="q"
              defaultValue={q}
              placeholder='例: from:example.com subject:見積 -newer_than:7d'
              className="input"
            />
          </label>

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

          <button type="submit" className="btn-primary">
            絞り込む
          </button>
          {(direction || incidentPriority || q) && (
            <a href="/email-log" className="link text-xs">
              条件をクリア
            </a>
          )}
        </div>

        <details className="text-xs text-(--color-foreground)/60">
          <summary className="cursor-pointer select-none font-medium">検索で使える演算子</summary>
          <ul className="mt-2 flex flex-col gap-1">
            {EMAIL_LOG_SEARCH_HELP.map((item) => (
              <li key={item.operator}>
                <code className="rounded bg-(--color-surface-muted) px-1 py-0.5">{item.operator}</code>
                {" "}
                {item.description}
                {" "}
                <span className="text-(--color-foreground)/40">(例: {item.example})</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-(--color-foreground)/40">
            has: / label: / is: / cc: / bcc: / category: / filename: 等、添付ファイル・ラベル・既読状態のようにこの画面で保存していない情報を条件にする演算子は非対応です(入力してもエラーにはならず無視されます)。
          </p>
          <p className="mt-1 text-(--color-foreground)/40">
            「Gmailで開く」リンクは、ブラウザで最初にログインしているGoogleアカウントで開きます。別のGoogleアカウントでログイン中の場合は開けないことがあります。
          </p>
        </details>
      </form>

      <div className="surface-card mt-6 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr className="bg-(--color-surface-muted)/60">
                <th>日時</th>
                <th>送受信</th>
                <th>連絡先</th>
                <th>担当者</th>
                <th>件名</th>
                <th>インシデント</th>
              </tr>
            </thead>
            <tbody>
              {logs.length === 0 ? (
                <tr>
                  <td colSpan={6}>
                    該当するメールがありません
                  </td>
                </tr>
              ) : (
                logs.map((log) => (
                  <tr key={log.id} className="align-top">
                    <td className="whitespace-nowrap">
                      {formatDateTime(log.sentAt)}
                    </td>
                    <td className="whitespace-nowrap">
                      {DIRECTION_LABELS[log.direction] ?? log.direction}
                    </td>
                    <td>
                      <a
                        href={`https://www.notion.so/${log.contactPageId.replace(/-/g, "")}`}
                        target="_blank"
                        rel="noreferrer"
                        className="link"
                      >
                        {log.contactEmail}
                      </a>
                    </td>
                    <td className="whitespace-nowrap">{log.repEmail}</td>
                    <td>
                      {log.subject ?? <span className="text-(--color-foreground)/40">(件名なし)</span>}
                      {log.snippet && (
                        <p className="mt-0.5 line-clamp-2 text-xs text-(--color-foreground)/50">{log.snippet}</p>
                      )}
                      <a
                        href={gmailMessageUrl(log.gmailMessageId)}
                        target="_blank"
                        rel="noreferrer"
                        className="link mt-0.5 block text-xs"
                      >
                        Gmailで開く
                      </a>
                    </td>
                    <td className="whitespace-nowrap">
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

      <div className="mt-3 flex items-center justify-between text-sm text-(--color-foreground)/60">
        <p>
          全{totalCount}件中 {totalCount === 0 ? 0 : (page - 1) * PAGE_SIZE + 1}
          -{Math.min(page * PAGE_SIZE, totalCount)}件を表示({page} / {totalPages}ページ)
        </p>
        <div className="flex gap-2">
          {page > 1 ? (
            <a href={pageHref(searchParams, page - 1)} className="btn-ghost btn-xs">
              前へ
            </a>
          ) : (
            <span className="btn-ghost btn-xs opacity-40">前へ</span>
          )}
          {page < totalPages ? (
            <a href={pageHref(searchParams, page + 1)} className="btn-ghost btn-xs">
              次へ
            </a>
          ) : (
            <span className="btn-ghost btn-xs opacity-40">次へ</span>
          )}
        </div>
      </div>
    </div>
  );
}
