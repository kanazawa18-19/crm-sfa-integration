"use client";

import { useActionState } from "react";
import { useRouter } from "next/navigation";
import { confirmTotpEnrollment } from "@/app/actions";

export default function TotpSetupForm({
  secret,
  qrCodeDataUrl,
  email,
}: {
  secret: string;
  qrCodeDataUrl: string;
  email: string;
}) {
  const router = useRouter();
  const [state, formAction, pending] = useActionState(confirmTotpEnrollment, undefined);

  if (state?.backupCodes) {
    return (
      <div className="flex flex-col gap-3">
        <div className="alert-warning">
          このバックアップコードは今だけ表示されます。認証アプリが使えなくなった場合の復旧に使うので、必ず安全な場所に保存してください。
        </div>
        <div className="grid grid-cols-2 gap-2 rounded-[6px] bg-(--color-surface-muted) p-3 font-mono text-sm">
          {state.backupCodes.map((code) => (
            <span key={code}>{code}</span>
          ))}
        </div>
        <button type="button" className="btn-primary" onClick={() => router.push("/")}>
          保存しました。続ける
        </button>
      </div>
    );
  }

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <input type="hidden" name="secret" value={secret} />
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={qrCodeDataUrl} alt="TOTP QRコード" className="mx-auto h-40 w-40" />
      <p className="text-center text-xs text-(--color-foreground)/50">
        読み取れない場合は手動でキーを入力:{" "}
        <code className="rounded bg-(--color-surface-muted) px-1 py-0.5">{secret}</code>
        <br />
        アカウント名: {email}
      </p>
      <input
        type="text"
        name="code"
        inputMode="numeric"
        autoComplete="one-time-code"
        placeholder="123456"
        required
        className="input text-center text-lg tracking-widest"
      />
      <button type="submit" disabled={pending} className="btn-primary">
        {pending ? "確認中..." : "確認して設定を完了する"}
      </button>
      {state?.error && <p className="text-xs text-(--brand-danger)">{state.error}</p>}
    </form>
  );
}
