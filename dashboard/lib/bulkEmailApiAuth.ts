import { NextResponse } from "next/server";
import { getCurrentUser, type CurrentUser } from "@/lib/auth";

// 一斉配信まわりのAPIルートの権限チェック(2026-09-03)。
//
// 3つのルート(preview / consent-overview / consent)で同じ判定をコピーしていたため
// 1箇所に寄せた。文言を片方だけ直して画面ごとに表現がずれる、を防ぐ
// (obasan-qualityレビュー指摘)。
//
// 閲覧者(viewer)を入れないのは、いずれのルートも取引先の連絡先(氏名・メールアドレス)を
// まとめて返す、あるいは「送ってよい」という判断を記録するため。

export type BulkEmailApiAuth =
  | { user: CurrentUser; error?: undefined }
  | { user?: undefined; error: NextResponse };

export async function requireBulkEmailEditor(): Promise<BulkEmailApiAuth> {
  const user = await getCurrentUser();
  if (!user) {
    return { error: NextResponse.json({ detail: "ログインが必要です" }, { status: 401 }) };
  }
  if (user.role === "viewer") {
    return {
      error: NextResponse.json(
        { detail: "この操作には編集者以上の権限が必要です" },
        { status: 403 }
      ),
    };
  }
  return { user };
}
