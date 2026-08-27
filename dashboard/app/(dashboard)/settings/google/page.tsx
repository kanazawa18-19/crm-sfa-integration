import Link from "next/link";
import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import { googleOauthErrorMessage } from "@/lib/googleOauthErrors";
import { formatJst } from "@/lib/date";

// Gmail連携・Drive連携をまとめて1回のOAuth同意で行うための統合ページ(2026-08-27)。
// 実際の接続先テーブル(RepGmailConnection/RepDriveConnection)はGmail連携・Drive
// 連携それぞれの既存ページ(app/(dashboard)/settings/gmail, .../drive)と同じもので、
// このページはそれらを横断して状態を見せる+一括連携の入口を提供するだけ。個別の
// 再連携・解除は既存ページに残しているため、このページ自体には解除アクションを持たない。
//
// 設計・トレードオフの詳細はdocs/google_oauth_note.md参照。特に、まとめて連携すると
// RepGmailConnection/RepDriveConnectionの両方に「gmail.readonly + drive」の広い
// スコープを持つ同一トークンが入る(個別連携ならテーブルごとに最小権限のトークンに
// なる)点は、このページの案内文でも伝えている。
export default async function GoogleSettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ connected?: string; missing?: string; error?: string }>;
}) {
  const user = await requireRole("viewer");
  const { connected, missing, error } = await searchParams;

  const [gmailConnection, driveConnection] = await Promise.all([
    prisma.repGmailConnection.findUnique({ where: { repEmail: user.email } }),
    prisma.repDriveConnection.findUnique({ where: { repEmail: user.email } }),
  ]);

  return (
    <div className="max-w-xl space-y-6">
      <h1 className="page-title">Google連携</h1>
      <p className="text-(--text-grey)">
        Gmail連携（メール送受信の自動記録）とDrive連携（見積書の承認リクエスト送信）を、
        1回のGoogleアカウント同意でまとめて設定できます。
      </p>

      {connected === "1" && !missing && (
        <div className="alert-success">Gmail・Driveをまとめて連携しました。</div>
      )}
      {connected === "1" && missing === "drive" && (
        <div className="alert-warning">
          Gmailは連携できましたが、Drive（見積書の承認リクエスト送信）の権限は同意画面で
          許可されなかったため連携されていません。もう一度「Googleとまとめて連携する」を押し、
          同意画面でGmail・Driveの両方にチェックを入れてください。
        </div>
      )}
      {connected === "1" && missing === "gmail" && (
        <div className="alert-warning">
          Driveは連携できましたが、Gmail（メール送受信の自動記録）の権限は同意画面で
          許可されなかったため連携されていません。もう一度「Googleとまとめて連携する」を押し、
          同意画面でGmail・Driveの両方にチェックを入れてください。
        </div>
      )}
      {error && <div className="alert-error">{googleOauthErrorMessage(error)}</div>}

      <div className="surface-card p-6 space-y-4">
        <ul className="space-y-2 text-sm">
          <li className="flex items-center justify-between">
            <span>Gmail連携</span>
            {gmailConnection ? (
              <span className="badge-blue">連携済み（{formatJst(gmailConnection.connectedAt)}）</span>
            ) : (
              <span className="badge-muted">未連携</span>
            )}
          </li>
          <li className="flex items-center justify-between">
            <span>Gmail 最終同期</span>
            {gmailConnection?.lastSyncedAt ? (
              <span>{formatJst(gmailConnection.lastSyncedAt)}</span>
            ) : (
              <span className="badge-muted">未同期</span>
            )}
          </li>
          <li className="flex items-center justify-between">
            <span>Drive連携</span>
            {driveConnection ? (
              <span className="badge-blue">連携済み（{formatJst(driveConnection.connectedAt)}）</span>
            ) : (
              <span className="badge-muted">未連携</span>
            )}
          </li>
          <li className="flex items-center justify-between">
            <span>Drive 最終同期</span>
            <span className="badge-muted">Driveは同期処理を行わないため対象外</span>
          </li>
        </ul>

        <Link href="/google/oauth/start" className="btn-primary inline-block">
          Googleとまとめて連携する
        </Link>
        <p className="text-xs text-(--color-foreground)/50">
          既にGmailだけ（またはDriveだけ）連携済みの状態でこのボタンを押すと、まだ連携していない
          方が追加されるだけでなく、Googleの同意画面が改めて表示され、既存側のトークンも
          最新の同意内容で上書きされます。「まとめて」＝差分追加ではない点にご注意ください。
        </p>
      </div>

      <div className="surface-card p-4 space-y-2 text-sm">
        <p className="font-medium">個別に連携する（権限を絞りたい場合）</p>
        <p className="text-(--text-grey)">
          Gmailしか使わない、Driveしか使わない場合は、まとめて連携ではなくそれぞれの設定ページから
          個別に連携することをおすすめします。まとめて連携するとRepGmailConnection・
          RepDriveConnectionの両方に「Gmail読み取り＋Drive」の広いスコープを持つ同じトークンが
          保存されますが、個別連携ならテーブルごとに必要最小限のスコープのトークンだけが
          保存されます（詳細は<code className="text-xs">docs/google_oauth_note.md</code>）。
        </p>
        <div className="mt-2 flex gap-3">
          <Link href="/settings/gmail" className="btn-ghost">
            Gmail連携の設定
          </Link>
          <Link href="/settings/drive" className="btn-ghost">
            Drive連携の設定
          </Link>
        </div>
      </div>
    </div>
  );
}
