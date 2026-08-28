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
            <table className="data-table">
              <thead>
                <tr className="bg-(--color-surface-muted)/60">
                  <th>案件名</th>
                  <th>営業ステータス</th>
                  <th>確度</th>
                  <th>初期費用</th>
                  <th>月額費用</th>
                  <th>担当メンバー</th>
                  <th>次回アクション日</th>
                </tr>
              </thead>
              <tbody>
                {projects.length === 0 ? (
                  <tr>
                    <td colSpan={7}>
                      案件がありません
                    </td>
                  </tr>
                ) : (
                  projects.map((p) => (
                    <tr key={p.notion_page_id}>
                      <td>
                        <a href={notionPageUrl(p.notion_page_id)} target="_blank" rel="noreferrer" className="link">
                          {p.案件名}
                        </a>
                      </td>
                      <td className="whitespace-nowrap">
                        {p.営業ステータス ?? "-"}
                      </td>
                      <td className="whitespace-nowrap">
                        {p.確度 ?? "-"}
                      </td>
                      <td className="whitespace-nowrap text-right">
                        {p.初期費用 != null ? formatYen(p.初期費用) : "-"}
                      </td>
                      <td className="whitespace-nowrap text-right">
                        {p.月額費用 != null ? formatYen(p.月額費用) : "-"}
                      </td>
                      <td className="whitespace-nowrap">
                        {p.担当メンバー.length > 0 ? p.担当メンバー.join("、") : "-"}
                      </td>
                      <td className="whitespace-nowrap">
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
            <table className="data-table">
              <thead>
                <tr className="bg-(--color-surface-muted)/60">
                  <th>名前</th>
                  <th>部署</th>
                  <th>役職</th>
                  <th>メールアドレス</th>
                  <th>携帯番号</th>
                  <th>直通TEL</th>
                </tr>
              </thead>
              <tbody>
                {contacts.length === 0 ? (
                  <tr>
                    <td colSpan={6}>
                      連絡先がありません
                    </td>
                  </tr>
                ) : (
                  contacts.map((c) => (
                    <tr key={c.notion_page_id}>
                      <td>
                        <a href={notionPageUrl(c.notion_page_id)} target="_blank" rel="noreferrer" className="link">
                          {c.名前}
                        </a>
                      </td>
                      <td className="whitespace-nowrap">{c.部署 ?? "-"}</td>
                      <td className="whitespace-nowrap">{c.役職 ?? "-"}</td>
                      <td className="whitespace-nowrap">
                        {c.メールアドレス ?? "-"}
                      </td>
                      <td className="whitespace-nowrap">
                        {c.携帯番号 ?? "-"}
                      </td>
                      <td className="whitespace-nowrap">
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
            <table className="data-table">
              <thead>
                <tr className="bg-(--color-surface-muted)/60">
                  <th>アクション日</th>
                  <th>アクション種別</th>
                  <th>内容</th>
                  <th>先方担当者</th>
                  <th>担当営業</th>
                </tr>
              </thead>
              <tbody>
                {actions.length === 0 ? (
                  <tr>
                    <td colSpan={5}>
                      アクション履歴がありません
                    </td>
                  </tr>
                ) : (
                  actions.map((a) => (
                    <tr key={a.notion_page_id} className="align-top">
                      <td className="whitespace-nowrap">
                        {formatOptionalDate(a.アクション日)}
                      </td>
                      <td className="whitespace-nowrap">
                        {a.アクション種別 ?? "-"}
                      </td>
                      <td>
                        <a href={notionPageUrl(a.notion_page_id)} target="_blank" rel="noreferrer" className="link">
                          {a["商談回数・電話回数・メール回数（何回目）"] ?? "-"}
                        </a>
                        {a.履歴メモ && (
                          <p className="mt-0.5 whitespace-pre-wrap text-xs text-(--color-foreground)/50">
                            {a.履歴メモ}
                          </p>
                        )}
                      </td>
                      <td className="whitespace-nowrap">
                        {a.先方担当者 ?? "-"}
                      </td>
                      <td className="whitespace-nowrap">
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
            <table className="data-table">
              <thead>
                <tr className="bg-(--color-surface-muted)/60">
                  <th>日時</th>
                  <th>送受信</th>
                  <th>連絡先</th>
                  <th>担当者</th>
                  <th>件名</th>
                </tr>
              </thead>
              <tbody>
                {emailLogs.length === 0 ? (
                  <tr>
                    <td colSpan={5}>
                      メール履歴がありません
                    </td>
                  </tr>
                ) : (
                  emailLogs.map((log) => (
                    <tr key={log.id} className="align-top">
                      <td className="whitespace-nowrap">
                        {formatDateTime(log.sentAt)}
                      </td>
                      <td className="whitespace-nowrap">
                        {log.direction === "inbound" ? "受信" : "送信"}
                      </td>
                      <td>{log.contactEmail}</td>
                      <td className="whitespace-nowrap">{log.repEmail}</td>
                      <td>
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
              <table className="data-table">
                <thead>
                  <tr className="bg-(--color-surface-muted)/60">
                    <th>日時</th>
                    <th>操作</th>
                    <th>対象ページ</th>
                    <th>変更内容</th>
                    <th>経路</th>
                  </tr>
                </thead>
                <tbody>
                  {auditLogs.length === 0 ? (
                    <tr>
                      <td colSpan={5}>
                        変更履歴がありません
                      </td>
                    </tr>
                  ) : (
                    auditLogs.map((log) => (
                      <tr key={log.id} className="align-top">
                        <td className="whitespace-nowrap">
                          {formatDateTime(log.createdAt)}
                        </td>
                        <td className="whitespace-nowrap">
                          {log.action === "create" ? "作成" : "更新"}
                        </td>
                        <td className="font-mono text-xs">
                          <a href={notionPageUrl(log.notionPageId)} target="_blank" rel="noreferrer" className="link">
                            {log.notionPageId}
                          </a>
                        </td>
                        <td className="whitespace-pre-wrap text-xs text-(--color-foreground)/70">
                          {formatChangedFields(log.changedFields)}
                        </td>
                        <td className="whitespace-nowrap">
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
