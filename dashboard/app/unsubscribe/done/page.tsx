// 配信停止の完了画面(2026-09-03)。ログイン不要(proxy.tsのPUBLIC_PATHS参照)。
//
// `?status=invalid` は、フォームのPOST時点で署名の検証に落ちた場合に来る
// (表示時は通ったが、その後にリンクが書き換えられた等)。停止できていないのに
// 「完了しました」と見せると、お客様は止まったと思って以後の連絡をしてこない。
export const dynamic = "force-dynamic";

export default async function UnsubscribeDonePage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string }>;
}) {
  const { status } = await searchParams;
  const failed = status === "invalid";

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-lg flex-col justify-center px-5 py-10">
      <div className="rounded-xl border border-(--border-subtle) bg-(--color-surface) p-6 sm:p-8">
        <h1 className="text-lg font-semibold">
          {failed ? "手続きを完了できませんでした" : "配信を停止しました"}
        </h1>
        <p className="mt-3 text-sm text-(--color-foreground)/70">
          {failed
            ? "リンクが正しくないため、配信停止の登録ができませんでした。お手数ですが、メールに記載のURLをもう一度お試しいただくか、担当者までご連絡ください。"
            : "以後、このアドレス宛のご案内メールはお送りしません。行き違いで届いた場合はご容赦ください。"}
        </p>
      </div>
    </main>
  );
}
