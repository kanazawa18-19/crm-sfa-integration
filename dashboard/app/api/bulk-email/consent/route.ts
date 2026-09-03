import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import prisma from "@/lib/prisma";
import { BackendApiError, bulkEmailConsentOverview, getErrorMessage } from "@/lib/backend";
import { requireBulkEmailEditor } from "@/lib/bulkEmailApiAuth";
import {
  isConsentBasis,
  isNormalizedContactPageId,
  normalizeContactPageIdForConsent,
  parseObtainedAt,
} from "@/lib/bulkEmailConsent";

// 「送ってよい根拠」の登録と取り消し(2026-09-03、一斉配信)。
//
// ■ 画面から来た値をそのまま保存しない
//
// 根拠に載せるメールアドレスは**Notionの連絡先から取り直す**。フォームの隠しフィールドを
// そのまま信じると、編集者が任意のアドレスに対して「送ってよい」を登録できてしまう。
// 根拠は「登録したときのアドレスと今の宛先が一致したときだけ有効」なので、ここで
// 間違ったアドレスを保存すると、そのまま「送ってはいけない相手に送る」に化ける。
//
// ■ 登録も取り消しも、同じ所属確認を通す
//
// 以前は DELETE だけ確認が無く、連絡先IDを指定するだけで任意の根拠を取り消せた
// (Gemini が BLOCKER として指摘、2026-09-03)。POST と同じく client_page_id を受け取り、
// 「その連絡先が本当にその取引先の下にいるか」を確かめてから書く。
//
// ■ 書いたものは必ず AuditLog に残す。しかも同じトランザクションで
//
// 送ってよいと誰がいつ判断したか、が後から追えないと根拠を持つ意味が半分無くなる。
// 片方だけ成功すると「変わったのに記録が無い」状態になるため $transaction でまとめる
// (ChatGPT 指摘)。AuditLog に載せるページIDは**解決した値**を使い、リクエストの
// 申告値は使わない(監査ログの整合性をHTTP側から壊せてしまうため)。

const ACTOR_SOURCE = "dashboard_bulk_email_consent";

// 証跡の最大長。名刺交換の場・案件ID・URLを書けば足りる長さで、
// 貼り付け事故でDBと画面が壊れない程度に抑える。
const MAX_EVIDENCE_LENGTH = 2000;

type ResolvedContact = { pageId: string; email: string; name: string; clientName: string };

/** Notion側の連絡先を取り直して、アドレスと所属を確かめる。 */
async function resolveContact(
  clientPageId: string,
  contactPageId: string
): Promise<ResolvedContact | null> {
  const overview = await bulkEmailConsentOverview({ client_page_ids: [clientPageId] });
  const found = overview.contacts.find(
    (contact) =>
      normalizeContactPageIdForConsent(contact.contact_page_id) ===
      normalizeContactPageIdForConsent(contactPageId)
  );
  if (!found) return null;
  return {
    pageId: found.contact_page_id,
    email: (found.email ?? "").trim().toLowerCase(),
    name: found.contact_name,
    clientName: found.client_name,
  };
}

/** 両メソッド共通の入口チェック(権限・連絡先の指定・所属)。 */
async function resolveTarget(payload: Record<string, unknown> | null) {
  const clientPageId = String(payload?.client_page_id ?? "").trim();
  const contactPageId = normalizeContactPageIdForConsent(String(payload?.contact_page_id ?? ""));
  if (!clientPageId || !isNormalizedContactPageId(contactPageId)) {
    return {
      error: NextResponse.json({ detail: "連絡先の指定が不正です" }, { status: 400 }),
    };
  }

  let contact: ResolvedContact | null;
  try {
    contact = await resolveContact(clientPageId, contactPageId);
  } catch (error) {
    const status = error instanceof BackendApiError && error.status ? error.status : 500;
    return { error: NextResponse.json({ detail: getErrorMessage(error) }, { status }) };
  }
  if (!contact) {
    return {
      error: NextResponse.json(
        { detail: "指定された連絡先が、その取引先の中に見つかりませんでした" },
        { status: 404 }
      ),
    };
  }
  return { contactPageId, contact };
}

export async function POST(request: NextRequest) {
  const auth = await requireBulkEmailEditor();
  if (auth.error) return auth.error;
  const user = auth.user;

  const payload = (await request.json().catch(() => null)) as Record<string, unknown> | null;
  if (!payload) {
    return NextResponse.json({ detail: "リクエストの形式が不正です" }, { status: 400 });
  }

  const basis = payload.basis;
  const evidence = String(payload.evidence ?? "").trim();
  const obtainedAt = parseObtainedAt(String(payload.obtained_at ?? ""));
  // 取り消し済みの根拠を復活させるのは、誤って取り消した場合の訂正であって
  // 通常の登録し直しとは別の判断。画面が明示的に申告したときだけ許す
  // (古い画面から届いたPOSTが黙って復活させるのを防ぐ。ChatGPT指摘)。
  const reactivate = payload.reactivate === true;

  if (!isConsentBasis(basis)) {
    return NextResponse.json({ detail: "根拠の種類を選んでください" }, { status: 400 });
  }
  if (!obtainedAt) {
    return NextResponse.json(
      { detail: "根拠を得た日を、実在する今日までの日付で入力してください" },
      { status: 400 }
    );
  }
  if (!evidence) {
    // 空の証跡は後から誰も裏を取れない。DB側にもCHECK制約がある。
    return NextResponse.json(
      { detail: "取得元・証跡を入力してください（名刺交換の場、フォームの送信日、案件IDなど）" },
      { status: 400 }
    );
  }
  if (evidence.length > MAX_EVIDENCE_LENGTH) {
    return NextResponse.json(
      { detail: `取得元・証跡は${MAX_EVIDENCE_LENGTH}文字以内で入力してください` },
      { status: 400 }
    );
  }

  const target = await resolveTarget(payload);
  if (target.error) return target.error;
  const { contactPageId, contact } = target;

  const before = await prisma.contactMailConsent.findUnique({ where: { contactPageId } });
  if (before?.revokedAt && !reactivate) {
    return NextResponse.json(
      {
        detail:
          "この連絡先の根拠は取り消されています。" +
          "取り消しを解除して登録し直す場合は、画面から「取り消しを解除して登録」を選んでください。",
        revoked_at: before.revokedAt.toISOString().slice(0, 10),
      },
      { status: 409 }
    );
  }

  // 日付は暦の日として持つ(DBの列はDATE)。UTCの時刻を持たせると、
  // 日本時間の午前中に当日を登録したものが「未来日」として送信不可になる。
  const obtainedDate = new Date(`${obtainedAt}T00:00:00.000Z`);

  await prisma.$transaction([
    prisma.contactMailConsent.upsert({
      where: { contactPageId },
      update: {
        contactEmail: contact.email,
        basis,
        obtainedAt: obtainedDate,
        evidence,
        recordedBy: user.email,
        revokedAt: null,
        revokedBy: null,
      },
      create: {
        contactPageId,
        contactEmail: contact.email,
        basis,
        obtainedAt: obtainedDate,
        evidence,
        recordedBy: user.email,
      },
    }),
    prisma.auditLog.create({
      data: {
        dbKey: "contact",
        // 解決した値を使う。リクエストの申告値を入れると監査ログをHTTP側から汚せる。
        notionPageId: contact.pageId,
        action: before ? "update" : "create",
        changedFields: {
          送信根拠: {
            before: before
              ? {
                  種類: before.basis,
                  取得日: before.obtainedAt,
                  証跡: before.evidence,
                  アドレス: before.contactEmail,
                  取り消し: before.revokedAt,
                }
              : null,
            after: {
              種類: basis,
              取得日: obtainedAt,
              証跡: evidence,
              アドレス: contact.email,
            },
          },
        },
        actorSource: ACTOR_SOURCE,
        actorLabel: user.email,
      },
    }),
  ]);

  return NextResponse.json({ ok: true });
}

export async function DELETE(request: NextRequest) {
  const auth = await requireBulkEmailEditor();
  if (auth.error) return auth.error;
  const user = auth.user;

  const payload = (await request.json().catch(() => null)) as Record<string, unknown> | null;
  const target = await resolveTarget(payload);
  if (target.error) return target.error;
  const { contactPageId, contact } = target;

  const before = await prisma.contactMailConsent.findUnique({ where: { contactPageId } });
  if (!before) {
    return NextResponse.json(
      { detail: "この連絡先には根拠が登録されていません" },
      { status: 404 }
    );
  }
  if (before.revokedAt) {
    // 2回押しても「最初に取り消した時刻」を書き換えない。いつ取り消したかは記録なので、
    // 押すたびに進むと元の申し出の日が分からなくなる。
    return NextResponse.json({ ok: true, already_revoked: true });
  }

  // **行は消さない。** 「あった根拠を取り消した」ことも記録だから。
  await prisma.$transaction([
    prisma.contactMailConsent.update({
      where: { contactPageId },
      data: { revokedAt: new Date(), revokedBy: user.email },
    }),
    prisma.auditLog.create({
      data: {
        dbKey: "contact",
        notionPageId: contact.pageId,
        action: "update",
        changedFields: {
          送信根拠: {
            before: {
              種類: before.basis,
              取得日: before.obtainedAt,
              証跡: before.evidence,
              アドレス: before.contactEmail,
            },
            after: null,
          },
        },
        actorSource: ACTOR_SOURCE,
        actorLabel: user.email,
      },
    }),
  ]);

  return NextResponse.json({ ok: true });
}
