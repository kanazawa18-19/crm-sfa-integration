import Link from "next/link";
import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import SubmitButton from "@/components/SubmitButton";
import { disconnectGmail } from "./actions";
import { googleOauthErrorMessage } from "@/lib/googleOauthErrors";
import { formatJst } from "@/lib/date";

export default async function GmailSettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ connected?: string; disconnected?: string; error?: string }>;
}) {
  const user = await requireRole("viewer");
  const { connected, disconnected, error } = await searchParams;

  const connection = await prisma.repGmailConnection.findUnique({ where: { repEmail: user.email } });
  const allConnections =
    user.role === "master" ? await prisma.repGmailConnection.findMany({ orderBy: { connectedAt: "desc" } }) : null;

  return (
    <div className="max-w-xl space-y-6">
      <h1 className="page-title">Gmail連携</h1>
      <p className="text-(--text-grey)">
        自分のGmailアカウントを連携すると、連絡先とのメール送受信が自動でCRMへ記録され、
        リードスコアにも反映されます（web-engagement-tool側のGmail連携から移管、2026-08-16）。
      </p>

      {connected === "1" && <div className="alert-success">Gmailを連携しました。</div>}
      {disconnected === "1" && <div className="alert-success">Gmail連携を解除しました。</div>}
      {error && <div className="alert-error">{googleOauthErrorMessage(error)}</div>}

      <div className="surface-card p-6 space-y-4">
        {connection ? (
          <>
            <p>
              連携中: <span className="font-medium">{user.email}</span>
            </p>
            <p className="text-sm text-(--text-grey)">
              最終同期: {connection.lastSyncedAt ? formatJst(connection.lastSyncedAt) : "未同期"}
            </p>
            <div className="flex gap-3">
              <Link href="/gmail/oauth/start" className="btn-ghost">
                再連携
              </Link>
              <form action={disconnectGmail}>
                <SubmitButton pendingLabel="解除中..." className="btn-danger">
                  連携を解除
                </SubmitButton>
              </form>
            </div>
          </>
        ) : (
          <>
            <p>まだGmailと連携していません。</p>
            <Link href="/gmail/oauth/start" className="btn-primary">
              Gmail連携を開始
            </Link>
          </>
        )}
      </div>

      {allConnections && (
        <div>
          <h2 className="text-sm font-semibold text-(--color-foreground)/70">連携状況（全体）</h2>
          <p className="mt-1 text-xs text-(--color-foreground)/50">
            現在Gmailと連携しているアドレス一覧です（管理者のみ表示）。
          </p>
          <div className="surface-card mt-3 overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-(--border-subtle) bg-(--color-surface-muted)/60 text-left text-(--color-foreground)/50">
                    <th className="px-4 py-2 font-medium">メールアドレス</th>
                    <th className="font-medium">連携日時</th>
                    <th className="font-medium">最終同期</th>
                  </tr>
                </thead>
                <tbody>
                  {allConnections.length === 0 ? (
                    <tr>
                      <td className="px-4 py-3 text-(--text-grey)" colSpan={3}>
                        まだ誰も連携していません。
                      </td>
                    </tr>
                  ) : (
                    allConnections.map((c) => (
                      <tr key={c.id} className="border-b border-(--border-subtle) last:border-0">
                        <td className="px-4 py-2">
                          {c.repEmail}
                          {c.repEmail === user.email && <span className="badge-blue ml-2">あなた</span>}
                        </td>
                        <td>{formatJst(c.connectedAt)}</td>
                        <td>
                          {c.lastSyncedAt ? (
                            formatJst(c.lastSyncedAt)
                          ) : (
                            <span className="badge-muted">未同期</span>
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
      )}
    </div>
  );
}
