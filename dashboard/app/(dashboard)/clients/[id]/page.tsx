import { notFound } from "next/navigation";
import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import { BackendApiError, Client360, getClient360 } from "@/lib/backend";
import { formatYen } from "@/lib/format";
import { formatDateTime, formatChangedFields } from "@/lib/auditLogFormat";
import type { EmailLog, AuditLog } from "@/generated/prisma/client";

export const dynamic = "force-dynamic";

const MAX_ROWS = 200;
// Notion側の関連レコード取得(query_page)の上限。src/api/client_360_service.pyの
// _RELATED_PAGE_SIZEと一致させる(この値を超えると打ち切られる)。
const RELATED_PAGE_SIZE = 100;

// バックエンドから渡される日付文字列（"YYYY-MM-DD"や未設定nullを含む）を表示用に整形する。
function formatOptionalDate(value: string | null | undefined): string {
  if (!value) return "-";
  return value.slice(0, 10);
}

function notionPageUrl(pageId: string): string {
  return `https://www.notion.so/${pageId.replace(/-/g, "")}`;
}

// 各セクション共通の「上限到達」「読み込み失敗」注記(obasan-qualityレビューWARN対応、
// 2026-08-18)。audit-log/page.tsxの上限到達メッセージと同じ文言に揃える。読み込み失敗を
// 「0件」と区別できるようにする(取得失敗時にemailLogs=[]をそのまま返していたため、
// 実際に0件なのか読み込みに失敗したのかユーザーが判別できなかった)。
function SectionNotice({ capReached, loadFailed }: { capReached?: boolean; loadFailed?: boolean }) {
  if (loadFailed) {
    return <p className="alert-error mb-2">読み込みに失敗しました。時間をおいて再度お試しください。</p>;
  }
  if (capReached) {
    return (
      <p className="alert-warning mb-2">
        表示件数の上限に達しています。実際にはこれ以上のデータがある可能性があります。
      </p>
    );
  }
  return null;
}

export default async function Client360Page({ params }: { params: Promise<{ id: string }> }) {
  const user = await requireRole("viewer");
  const { id } = await params;

  let client360: Client360;
  try {
    client360 = await getClient360(id);
  } catch (error) {
    if (error instanceof BackendApiError && error.status === 404) {
      notFound();
    }
    throw error;
  }

  const { client, projects, contacts, actions } = client360;

  const contactIds = contacts.map((c) => c.notion_page_id);
  const allRelatedIds = [
    client.notion_page_id,
    ...projects.map((p) => p.notion_page_id),
    ...contactIds,
    ...actions.map((a) => a.notion_page_id),
  ];

  // メール履歴・変更履歴はこのdashboard側が所有するPostgres(Neon)テーブルのため、
  // バックエンド(Python)経由にせずここで直接取得する(email-log/audit-log/page.tsxと
  // 同じ方針)。取得に失敗しても360ビューの他セクション(Notion由来のデータ)は
  // 表示できるよう、documents/page.tsxと同様try/catchでフォールバックする。
  //
  // 変更履歴(AuditLog.changedFields)はメール・電話番号等のPIIを含むフィールド単位の
  // 差分であり、/audit-log(監査ログ画面)が既にmaster限定になっている理由と同じ機微性を
  // 持つ。360ビューは1社スコープとはいえ、取引先検索自体は全viewerに開放しているため
  // 「1社に絞られているから安全」という理由付けは実効性が薄く、/audit-logの既存方針との
  // 整合を優先してmaster限定にする(shirokuma-secレビューBLOCKER対応、2026-08-18)。
  const canViewAuditLog = user.role === "master";

  let emailLogs: EmailLog[] = [];
  let emailLogError = false;
  let auditLogs: AuditLog[] = [];
  let auditLogError = false;
  try {
    emailLogs = await prisma.emailLog.findMany({
      where: { contactPageId: { in: contactIds } },
      orderBy: { sentAt: "desc" },
      take: MAX_ROWS,
    });
  } catch (error) {
    console.error("failed to load email log for client 360 view", error);
    emailLogError = true;
  }
  if (canViewAuditLog) {
    try {
      auditLogs = await prisma.auditLog.findMany({
        where: { notionPageId: { in: allRelatedIds } },
        orderBy: { createdAt: "desc" },
        take: MAX_ROWS,
      });
    } catch (error) {
      console.error("failed to load audit log for client 360 view", error);
      auditLogError = true;
    }
  }

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="page-title">{client.取引先名}</h1>
        <p className="mt-1 text-sm text-(--color-foreground)/60">
          <a href={notionPageUrl(client.notion_page_id)} target="_blank" rel="noreferrer" className="link">
            Notionで開く
          </a>
        </p>
      </div>

      <section className="surface-card p-4">
        <h2 className="mb-3 text-lg font-semibold text-(--color-foreground)">取引先概要</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-(--color-foreground)/60">顧客種別</dt>
          <dd className="text-(--color-foreground)">{client.顧客種別 ?? "-"}</dd>
          <dt className="text-(--color-foreground)/60">都道府県</dt>
          <dd className="text-(--color-foreground)">{client.都道府県 ?? "-"}</dd>
          <dt className="text-(--color-foreground)/60">住所</dt>
          <dd className="text-(--color-foreground)">{client.住所 ?? "-"}</dd>
          <dt className="text-(--color-foreground)/60">TEL</dt>
          <dd className="text-(--color-foreground)">{client.TEL ?? "-"}</dd>
          <dt className="text-(--color-foreground)/60">FAX</dt>
          <dd className="text-(--color-foreground)">{client.FAX ?? "-"}</dd>
          <dt className="text-(--color-foreground)/60">備考</dt>
          <dd className="whitespace-pre-wrap text-(--color-foreground)">{client.備考 ?? "-"}</dd>
        </dl>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-(--color-foreground)">
          案件 ({projects.length}件)
        </h2>
        <SectionNotice capReached={projects.length === RELATED_PAGE_SIZE} />
        <div className="surface-card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-(--border-subtle) bg-(--color-surface-muted)/60 text-left text-(--color-foreground)/50">
                  <th className="px-4 py-2 font-medium">案件名</th>
                  <th className="px-4 py-2 font-medium">営業ステータス</th>
                  <th className="px-4 py-2 font-medium">確度</th>
                  <th className="px-4 py-2 font-medium">初期費用</th>
                  <th className="px-4 py-2 font-medium">月額費用</th>
                  <th className="px-4 py-2 font-medium">担当メンバー</th>
                  <th className="px-4 py-2 font-medium">次回アクション日</th>
                </tr>
              </thead>
              <tbody>
                {projects.length === 0 ? (
                  <tr>
                    <td className="px-4 py-3 text-(--color-foreground)/50" colSpan={7}>
                      案件がありません
                    </td>
                  </tr>
                ) : (
                  projects.map((p) => (
                    <tr key={p.notion_page_id} className="border-b border-(--border-subtle) last:border-0">
                      <td className="px-4 py-2">
                        <a href={notionPageUrl(p.notion_page_id)} target="_blank" rel="noreferrer" className="link">
                          {p.案件名}
                        </a>
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {p.営業ステータス ?? "-"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {p.確度 ?? "-"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-right text-(--color-foreground)/80">
                        {p.初期費用 != null ? formatYen(p.初期費用) : "-"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-right text-(--color-foreground)/80">
                        {p.月額費用 != null ? formatYen(p.月額費用) : "-"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {p.担当メンバー.length > 0 ? p.担当メンバー.join("、") : "-"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {formatOptionalDate(p.次回アクション日)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-(--color-foreground)">
          連絡先 ({contacts.length}件)
        </h2>
        <SectionNotice capReached={contacts.length === RELATED_PAGE_SIZE} />
        <div className="surface-card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-(--border-subtle) bg-(--color-surface-muted)/60 text-left text-(--color-foreground)/50">
                  <th className="px-4 py-2 font-medium">名前</th>
                  <th className="px-4 py-2 font-medium">部署</th>
                  <th className="px-4 py-2 font-medium">役職</th>
                  <th className="px-4 py-2 font-medium">メールアドレス</th>
                  <th className="px-4 py-2 font-medium">携帯番号</th>
                  <th className="px-4 py-2 font-medium">直通TEL</th>
                </tr>
              </thead>
              <tbody>
                {contacts.length === 0 ? (
                  <tr>
                    <td className="px-4 py-3 text-(--color-foreground)/50" colSpan={6}>
                      連絡先がありません
                    </td>
                  </tr>
                ) : (
                  contacts.map((c) => (
                    <tr key={c.notion_page_id} className="border-b border-(--border-subtle) last:border-0">
                      <td className="px-4 py-2">
                        <a href={notionPageUrl(c.notion_page_id)} target="_blank" rel="noreferrer" className="link">
                          {c.名前}
                        </a>
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">{c.部署 ?? "-"}</td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">{c.役職 ?? "-"}</td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {c.メールアドレス ?? "-"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {c.携帯番号 ?? "-"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {c.直通TEL ?? "-"}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-(--color-foreground)">
          アクション履歴 ({actions.length}件)
        </h2>
        <SectionNotice capReached={actions.length === RELATED_PAGE_SIZE} />
        <div className="surface-card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-(--border-subtle) bg-(--color-surface-muted)/60 text-left text-(--color-foreground)/50">
                  <th className="px-4 py-2 font-medium">アクション日</th>
                  <th className="px-4 py-2 font-medium">アクション種別</th>
                  <th className="px-4 py-2 font-medium">内容</th>
                  <th className="px-4 py-2 font-medium">先方担当者</th>
                  <th className="px-4 py-2 font-medium">担当営業</th>
                </tr>
              </thead>
              <tbody>
                {actions.length === 0 ? (
                  <tr>
                    <td className="px-4 py-3 text-(--color-foreground)/50" colSpan={5}>
                      アクション履歴がありません
                    </td>
                  </tr>
                ) : (
                  actions.map((a) => (
                    <tr key={a.notion_page_id} className="border-b border-(--border-subtle) align-top last:border-0">
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {formatOptionalDate(a.アクション日)}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {a.アクション種別 ?? "-"}
                      </td>
                      <td className="px-4 py-2 text-(--color-foreground)/80">
                        <a href={notionPageUrl(a.notion_page_id)} target="_blank" rel="noreferrer" className="link">
                          {a["商談回数・電話回数・メール回数（何回目）"] ?? "-"}
                        </a>
                        {a.履歴メモ && (
                          <p className="mt-0.5 whitespace-pre-wrap text-xs text-(--color-foreground)/50">
                            {a.履歴メモ}
                          </p>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {a.先方担当者 ?? "-"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {a.担当営業 ?? "-"}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold text-(--color-foreground)">
          メール履歴 ({emailLogs.length}件)
        </h2>
        <SectionNotice loadFailed={emailLogError} capReached={emailLogs.length === MAX_ROWS} />
        <div className="surface-card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-(--border-subtle) bg-(--color-surface-muted)/60 text-left text-(--color-foreground)/50">
                  <th className="px-4 py-2 font-medium">日時</th>
                  <th className="px-4 py-2 font-medium">送受信</th>
                  <th className="px-4 py-2 font-medium">連絡先</th>
                  <th className="px-4 py-2 font-medium">担当者</th>
                  <th className="px-4 py-2 font-medium">件名</th>
                </tr>
              </thead>
              <tbody>
                {emailLogs.length === 0 ? (
                  <tr>
                    <td className="px-4 py-3 text-(--color-foreground)/50" colSpan={5}>
                      メール履歴がありません
                    </td>
                  </tr>
                ) : (
                  emailLogs.map((log) => (
                    <tr key={log.id} className="border-b border-(--border-subtle) align-top last:border-0">
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {formatDateTime(log.sentAt)}
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                        {log.direction === "inbound" ? "受信" : "送信"}
                      </td>
                      <td className="px-4 py-2 text-(--color-foreground)/80">{log.contactEmail}</td>
                      <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">{log.repEmail}</td>
                      <td className="px-4 py-2 text-(--color-foreground)/80">
                        {log.subject ?? <span className="text-(--color-foreground)/40">(件名なし)</span>}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {canViewAuditLog && (
        <section>
          <h2 className="mb-3 text-lg font-semibold text-(--color-foreground)">
            変更履歴 ({auditLogs.length}件)
          </h2>
          <SectionNotice loadFailed={auditLogError} capReached={auditLogs.length === MAX_ROWS} />
          <div className="surface-card overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-(--border-subtle) bg-(--color-surface-muted)/60 text-left text-(--color-foreground)/50">
                    <th className="px-4 py-2 font-medium">日時</th>
                    <th className="px-4 py-2 font-medium">操作</th>
                    <th className="px-4 py-2 font-medium">対象ページ</th>
                    <th className="px-4 py-2 font-medium">変更内容</th>
                    <th className="px-4 py-2 font-medium">経路</th>
                  </tr>
                </thead>
                <tbody>
                  {auditLogs.length === 0 ? (
                    <tr>
                      <td className="px-4 py-3 text-(--color-foreground)/50" colSpan={5}>
                        変更履歴がありません
                      </td>
                    </tr>
                  ) : (
                    auditLogs.map((log) => (
                      <tr key={log.id} className="border-b border-(--border-subtle) align-top last:border-0">
                        <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                          {formatDateTime(log.createdAt)}
                        </td>
                        <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                          {log.action === "create" ? "作成" : "更新"}
                        </td>
                        <td className="px-4 py-2 font-mono text-xs">
                          <a href={notionPageUrl(log.notionPageId)} target="_blank" rel="noreferrer" className="link">
                            {log.notionPageId}
                          </a>
                        </td>
                        <td className="whitespace-pre-wrap px-4 py-2 text-xs text-(--color-foreground)/70">
                          {formatChangedFields(log.changedFields)}
                        </td>
                        <td className="whitespace-nowrap px-4 py-2 text-(--color-foreground)/80">
                          {log.actorSource}
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
        </section>
      )}
    </div>
  );
}
