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
        <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800">
          このバックアップコードは今だけ表示されます。認証アプリが使えなくなった場合の復旧に使うので、必ず安全な場所に保存してください。
        </div>
        <div className="grid grid-cols-2 gap-2 rounded-lg bg-gray-50 p-3 font-mono text-sm">
          {state.backupCodes.map((code) => (
            <span key={code}>{code}</span>
          ))}
        </div>
        <button
          type="button"
          className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
          onClick={() => router.push("/")}
        >
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
      <p className="text-center text-xs text-gray-500">
        読み取れない場合は手動でキーを入力: <code className="rounded bg-gray-100 px-1 py-0.5">{secret}</code>
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
        className="rounded border border-gray-300 px-3 py-2 text-center text-lg tracking-widest text-gray-900 focus:border-blue-500 focus:outline-none"
      />
      <button
        type="submit"
        disabled={pending}
        className="rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {pending ? "確認中..." : "確認して設定を完了する"}
      </button>
      {state?.error && <p className="text-xs text-red-600">{state.error}</p>}
    </form>
  );
}
