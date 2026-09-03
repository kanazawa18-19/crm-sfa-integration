import prisma from "@/lib/prisma";
import SubmitButton from "@/components/SubmitButton";
import { unsubscribeAction } from "./actions";
import {
  loadUnsubscribeSecret,
  normalizeContactPageId,
  verifyUnsubscribeToken,
} from "@/lib/bulkEmailUnsubscribe";

// 一斉配信の配信停止ページ(2026-09-03)。**お客様が開く唯一の画面で、ログイン不要。**
//
// proxy.tsで、ログイン必須の対象からも**IP制限の対象からも**外している。
// IP制限だけ外し忘れると、社外から開けない配信停止リンクを載せたメールを撒くことになり、
// 特定電子メール法の「配信停止の方法を明示し、実際に停止できること」を満たさなくなる。
//
// このページは「押すと止まります」を見せるだけで、停止そのものはPOST(Server Action)で行う。
// 理由はactions.tsのコメント参照(メールのリンクは本人が押す前に先読みされることがある)。
export const dynamic = "force-dynamic";

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-lg flex-col justify-center px-5 py-10">
      <div className="rounded-xl border border-(--border-subtle) bg-(--color-surface) p-6 sm:p-8">
        {children}
      </div>
    </main>
  );
}

export default async function UnsubscribePage({
  searchParams,
}: {
  searchParams: Promise<{ c?: string; t?: string }>;
}) {
  const params = await searchParams;
  const contactPageId = normalizeContactPageId(params.c ?? "");
  const token = params.t ?? "";
  const secret = loadUnsubscribeSecret();

  if (!contactPageId || !verifyUnsubscribeToken(secret, contactPageId, token)) {
    return (
      <Frame>
        <h1 className="text-lg font-semibold">リンクが正しくありません</h1>
        <p className="mt-3 text-sm text-(--color-foreground)/70">
          お手数ですが、お手元のメールに記載されたURLをもう一度ご確認ください。
          コピーの際に途中で切れてしまうことがあります。
        </p>
      </Frame>
    );
  }

  const existing = await prisma.contactMailPreference.findUnique({
    where: { contactPageId },
    select: { unsubscribed: true, unsubscribedAt: true },
  });

  if (existing?.unsubscribed) {
    return (
      <Frame>
        <h1 className="text-lg font-semibold">すでに配信を停止しています</h1>
        <p className="mt-3 text-sm text-(--color-foreground)/70">
          このアドレス宛のご案内メールは、
          {existing.unsubscribedAt.toLocaleDateString("ja-JP")}に停止済みです。
          あらためてのお手続きは不要です。
        </p>
      </Frame>
    );
  }

  return (
    <Frame>
      <h1 className="text-lg font-semibold">ご案内メールの配信を停止します</h1>
      <p className="mt-3 text-sm text-(--color-foreground)/70">
        下のボタンを押すと、以後このアドレス宛のご案内メールをお送りしません。
        取り消したい場合は、お手数ですが担当者までご連絡ください。
      </p>
      <form action={unsubscribeAction} className="mt-6">
        <input type="hidden" name="c" value={contactPageId} />
        <input type="hidden" name="t" value={token} />
        <SubmitButton pendingLabel="停止しています..." className="btn-primary w-full">
          配信を停止する
        </SubmitButton>
      </form>
    </Frame>
  );
}
