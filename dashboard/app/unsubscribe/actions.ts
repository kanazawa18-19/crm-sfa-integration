"use server";

import { redirect } from "next/navigation";
import prisma from "@/lib/prisma";
import {
  loadUnsubscribeSecret,
  normalizeContactPageId,
  toDashedContactPageId,
  verifyUnsubscribeToken,
} from "@/lib/bulkEmailUnsubscribe";

// 配信停止の実行(2026-09-03)。**GETでは絶対に止めない。**
//
// メールのリンクは、本人が押す前にGmail/Outlook等のリンクスキャナや、社内のセキュリティ
// 製品が先読みすることがある。GETで停止する作りにすると、届いた瞬間に全員が停止済みに
// なりうる(しかも本人は何も操作していないので誰も気づかない)。そのため
//
//    GET  /unsubscribe?c=..&t=..   → 内容を見せて「配信を停止する」ボタンを出すだけ
//    POST (このServer Action)      → ここで初めて記録する
//
// という2段にしている。

export async function unsubscribeAction(formData: FormData): Promise<void> {
  const contactPageId = normalizeContactPageId(String(formData.get("c") ?? ""));
  const token = String(formData.get("t") ?? "");

  // 署名はページ表示時にも見ているが、POSTでも必ずやり直す。表示時のチェックだけに
  // 頼ると、フォームを直接組み立てて任意の連絡先を停止できてしまう。
  if (!contactPageId || !verifyUnsubscribeToken(loadUnsubscribeSecret(), contactPageId, token)) {
    redirect("/unsubscribe/done?status=invalid");
  }

  // 正規化済みのページIDの形（32桁hex）でなければ書きに行かない。
  // DB側にも同じ形のCHECK制約があるので、ここで止めないとお客様の画面が
  // 「配信を停止しました」ではなくDBエラーになる。
  const dashed = toDashedContactPageId(contactPageId);
  if (!dashed) {
    redirect("/unsubscribe/done?status=invalid");
  }

  // 停止時点のメールアドレス。連絡先レコードが作り直されてpage_idが変わっても
  // アドレス側で除外を続けられるようにするため保存する(ContactMailPreferenceの
  // スキーマコメント参照)。EmailLogにやり取りが1件も無い相手では空になるが、
  // その場合もpage_idでの除外は効く。
  // EmailLogのcontactPageIdはNotionから来たハイフン付きの形で入っているため、
  // URL側の正規化済みの形と両方で探す(toDashedContactPageIdの説明を参照)。
  //
  // **やり取りの記録が無い相手では空文字になる。** そのときアドレス側での除外は
  // 効かず、ページIDでの除外だけが残る。連絡先をNotionで作り直してページIDが
  // 変わると、その人はもう一度送信対象に戻る(ChatGPTレビュー指摘、2026-09-03)。
  // ②で実送信を足すときは、送った宛先を記録してそこからアドレスを取ること。
  const latestLog = await prisma.emailLog.findFirst({
    where: { contactPageId: { in: [contactPageId, dashed] } },
    orderBy: { sentAt: "desc" },
    select: { contactEmail: true },
  });
  const contactEmail = (latestLog?.contactEmail ?? "").trim().toLowerCase();

  const existing = await prisma.contactMailPreference.findUnique({
    where: { contactPageId },
    select: { unsubscribed: true },
  });

  await prisma.contactMailPreference.upsert({
    where: { contactPageId },
    // **update側でも unsubscribed: true を明示する。**
    // 今は書き手がこのアクションだけなので、行があれば必ず停止済み — つまり
    // 「行の有無＝停止済みか」という暗黙の前提の上でなら省略できる。だが
    // schema.prismaが source: "manual"(社内で手入力) を予約しており、将来
    // unsubscribed: false の行ができた瞬間に、お客様がボタンを押しても止まらないまま
    // 「配信を停止しました」と表示する状態になる。**特定電子メール法で一番避けたい壊れ方**
    // なので、前提に頼らず書く(動物チーム3体が独立に同じ箇所を指摘、2026-09-03)。
    //
    // unsubscribedAt は、停止中の相手が2回押しただけなら動かさない
    // (押すたびに日時が進むと、最初の申し出がいつだったか分からなくなる)。
    // 逆に「一度解除された行を停止し直した」ときは今の日時に進める。
    // 1月に停止 → 2月に再購読 → 3月に再度停止、で1月のままだと記録として誤り
    // (ChatGPTレビュー指摘、2026-09-03)。
    update: {
      unsubscribed: true,
      contactEmail: contactEmail || undefined,
      ...(existing && !existing.unsubscribed ? { unsubscribedAt: new Date() } : {}),
    },
    create: { contactPageId, contactEmail, unsubscribed: true, source: "self" },
  });

  redirect("/unsubscribe/done");
}
