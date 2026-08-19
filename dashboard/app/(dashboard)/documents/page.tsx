import prisma from "@/lib/prisma";
import { requireRole } from "@/lib/auth";
import DocumentsPageClient from "./DocumentsPageClient";

export const dynamic = "force-dynamic";

// 見積書 承認フロー(2026-08-18)向けに、ログイン中ユーザーのDrive連携状況(RepDriveConnection)・
// 有効な承認者一覧(DocumentApprover)をサーバー側でPrismaから直接取得してからクライアント
// コンポーネントへ渡す(いずれもdashboard側が所有するテーブルのため、Pythonバックエンド
// 経由にせずここで完結させる——RepGmailConnection/User管理と同じ方針)。
//
// このクエリはtry/catchで囲む(obasan-qualityレビューWARN対応)。Neon接続断時にここで
// 例外を投げると、承認フローと無関係な既存の書類ダウンロード機能(申込書・契約書)まで
// 巻き添えでページごとエラーになってしまうため、失敗時はdriveConnected=false・
// approvers=[]にフォールバックする(承認リクエストUIのみ利用不可になる)。
export default async function DocumentsPage() {
  const user = await requireRole("viewer");

  let driveConnected = false;
  let approvers: { id: string; name: string; email: string; title: string | null }[] = [];
  try {
    const [driveConnection, approverRows] = await Promise.all([
      prisma.repDriveConnection.findUnique({ where: { repEmail: user.email } }),
      prisma.documentApprover.findMany({ where: { active: true }, orderBy: { name: "asc" } }),
    ]);
    driveConnected = driveConnection !== null;
    approvers = approverRows.map((a) => ({ id: a.id, name: a.name, email: a.email, title: a.title }));
  } catch (error) {
    console.error("failed to load drive connection / document approvers", error);
  }

  return (
    <DocumentsPageClient
      driveConnected={driveConnected}
      approvers={approvers}
      creatorNameDefault={buildCreatorNameDefault(user.email)}
    />
  );
}

// 見積書NOの採番（`CN{YYYYMMDD}{作成者頭文字1字}{連番}`）は先頭1文字をそのまま使うため、
// 日本語の表示名(User.name)を初期値にすると「金沢」→「金」のように意図しない文字になる
// (obasan-qualityレビューBLOCKER対応)。社内のメールアドレスはローマ字の姓が使われている
// 慣例（kanazawa@cnctor.jp等）のため、メールのローカル部を先頭大文字化した値を初期値にする。
function buildCreatorNameDefault(email: string): string {
  const localPart = email.split("@")[0] ?? "";
  if (!localPart) {
    return "";
  }
  return localPart.charAt(0).toUpperCase() + localPart.slice(1);
}
