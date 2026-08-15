import Link from "next/link";
import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import { disconnectGmail } from "./actions";

const ERROR_MESSAGES_JA: Record<string, string> = {
  invalid_state: "連携セッションの有効期限が切れました。もう一度お試しください。",
  exchange_failed: "Googleとの連携処理に失敗しました。もう一度お試しください。",
};

export default async function GmailSettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ connected?: string; error?: string }>;
}) {
  const user = await requireRole("viewer");
  const { connected, error } = await searchParams;

  const connection = await prisma.repGmailConnection.findUnique({ where: { repEmail: user.email } });

  return (
    <div className="max-w-xl space-y-6">
      <h1 className="page-title">Gmail連携</h1>
      <p className="text-(--text-grey)">
        自分のGmailアカウントを連携すると、連絡先とのメール送受信が自動でCRMへ記録され、
        リードスコアにも反映されます（web-engagement-tool側のGmail連携から移管、2026-08-16）。
      </p>

      {connected === "1" && <div className="alert-success">Gmailを連携しました。</div>}
      {error && (
        <div className="alert-error">{ERROR_MESSAGES_JA[error] ?? "連携に失敗しました。もう一度お試しください。"}</div>
      )}

      <div className="surface-card p-6 space-y-4">
        {connection ? (
          <>
            <p>
              連携中: <span className="font-medium">{user.email}</span>
            </p>
            <p className="text-sm text-(--text-grey)">
              最終同期: {connection.lastSyncedAt ? connection.lastSyncedAt.toLocaleString("ja-JP") : "未同期"}
            </p>
            <div className="flex gap-3">
              <Link href="/gmail/oauth/start" className="btn-ghost">
                再連携
              </Link>
              <form action={disconnectGmail}>
                <button type="submit" className="btn-danger">
                  連携を解除
                </button>
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
    </div>
  );
}
