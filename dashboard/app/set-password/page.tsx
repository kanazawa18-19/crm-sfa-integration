import { GOOGLE_ONLY_LOGIN_MESSAGE } from "@/lib/loginPolicy";

// パスワード設定は廃止（2026-09-16）。招待・再設定メールの古いリンクから来た人への案内だけ残す。
// Server Action の setPassword() も拒否するので、フォームを復元しても更新は起きない。
export default function SetPasswordPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-(--color-background)">
      <div className="surface-card w-full max-w-sm p-8">
        <h1 className="page-title mb-2 text-xl">パスワードの設定は不要になりました</h1>
        <p className="mb-4 text-sm text-(--color-foreground)/60">{GOOGLE_ONLY_LOGIN_MESSAGE}</p>
        <a href="/login" className="btn-primary block w-full text-center">
          ログイン画面へ
        </a>
      </div>
    </div>
  );
}
