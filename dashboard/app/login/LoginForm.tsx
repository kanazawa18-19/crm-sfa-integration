import BrandLogo from "@/components/BrandLogo";
import { ALLOWED_LOGIN_DOMAIN } from "@/lib/loginPolicy";

/**
 * ログインは cnctor.jp の Google アカウントだけ（2026-09-16 本人指示）。
 * パスワード欄は撤去した。Server Action の login() も拒否するので、フォームを
 * 復元しても通らない。判定は app/gmail/oauth/callback の admin_login 分岐。
 *
 * `initialError` は、Googleログインの失敗がコールバックからのリダイレクト
 * (`/login?error=...`)で返ってくるため。サーバー側で読んで渡している(2026-08-31)。
 */
export default function LoginForm({ initialError }: { initialError?: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-(--color-background)">
      <div className="surface-card w-full max-w-sm p-8">
        <BrandLogo heightClass="h-8" widthClass="w-36" className="mb-4" />
        <h1 className="page-title mb-6 text-xl">管理画面ログイン</h1>
        {initialError && <p className="mb-4 text-sm text-(--brand-danger)">{initialError}</p>}
        <a href="/login/google/start" className="btn-primary block w-full text-center">
          Googleでログイン
        </a>
        <p className="mt-3 text-xs text-(--color-foreground)/60">
          {ALLOWED_LOGIN_DOMAIN} の Google アカウントでログインしてください。
          招待されていないアカウントではログインできません。
        </p>
      </div>
    </div>
  );
}
