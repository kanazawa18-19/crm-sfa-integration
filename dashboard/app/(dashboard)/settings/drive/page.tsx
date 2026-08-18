import Link from "next/link";
import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import SubmitButton from "@/components/SubmitButton";
import { disconnectDrive } from "./actions";

const ERROR_MESSAGES_JA: Record<string, string> = {
  invalid_state: "連携セッションの有効期限が切れました。もう一度お試しください。",
  exchange_failed: "Googleとの連携処理に失敗しました。もう一度お試しください。",
};

function formatJst(date: Date): string {
  return date.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" });
}

export default async function DriveSettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ connected?: string; disconnected?: string; error?: string }>;
}) {
  const user = await requireRole("viewer");
  const { connected, disconnected, error } = await searchParams;

  const connection = await prisma.repDriveConnection.findUnique({ where: { repEmail: user.email } });

  return (
    <div className="max-w-xl space-y-6">
      <h1 className="page-title">Drive連携</h1>
      <p className="text-(--text-grey)">
        見積書の承認リクエスト（Googleドライブ純正の「承認をリクエスト」機能）を送信するには、
        自分のGoogleアカウントを連携する必要があります。サービスアカウントでは承認リクエストの
        送信権限（canStartApproval）が無いため、この個人連携が必須です（2026-08-18、
        Gmail連携と同じ仕組み）。
      </p>

      {connected === "1" && <div className="alert-success">Driveを連携しました。</div>}
      {disconnected === "1" && <div className="alert-success">Drive連携を解除しました。</div>}
      {error && (
        <div className="alert-error">{ERROR_MESSAGES_JA[error] ?? "連携に失敗しました。もう一度お試しください。"}</div>
      )}

      <div className="surface-card p-6 space-y-4">
        {connection ? (
          <>
            <p>
              連携中: <span className="font-medium">{user.email}</span>
            </p>
            <p className="text-sm text-(--text-grey)">連携日時: {formatJst(connection.connectedAt)}</p>
            <div className="flex gap-3">
              <Link href="/drive/oauth/start" className="btn-ghost">
                再連携
              </Link>
              <form action={disconnectDrive}>
                <SubmitButton pendingLabel="解除中..." className="btn-danger">
                  連携を解除
                </SubmitButton>
              </form>
            </div>
          </>
        ) : (
          <>
            <p>まだDriveと連携していません。</p>
            <Link href="/drive/oauth/start" className="btn-primary">
              Drive連携を開始
            </Link>
          </>
        )}
      </div>
    </div>
  );
}
