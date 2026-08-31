import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { COOKIE_NAME, isValidSessionToken } from "@/lib/adminSession";
import { isIpAllowed, extractClientIp } from "@/lib/ipAllowlist";
import prisma from "@/lib/prisma";

// Next.js 16 では middleware.ts は非推奨となり proxy.ts にリネームされている。
// 動作・実行タイミングは旧 middleware と同等（Node.js ランタイムがデフォルト）。
// web-engagement-toolのsrc/proxy.tsと同じ構成に移植(2026-08-15、ユーザー管理・IP制限・
// 2FA導入に伴う置き換え)。

const PUBLIC_PATHS = [
  "/login",
  // Googleログイン(2026-08-31)。ログイン前に叩かれるので認証を要求できない。
  "/login/google/start",
  "/forgot-password",
  "/set-password",
  "/login/2fa",
  "/login/2fa-setup",
  "/login/2fa-email",
  "/confirm-email-change",
];

const GOOGLE_LOGIN_CALLBACK_PATH = "/gmail/oauth/callback";
const ADMIN_LOGIN_PURPOSE = "admin_login";

/** stateが `<nonce>.admin_login` 形式かどうか。nonce自体の検証はルート本体が行う。 */
function isAdminLoginCallback(request: NextRequest): boolean {
  const state = request.nextUrl.searchParams.get("state");
  if (!state) return false;
  const dotIndex = state.indexOf(".");
  return dotIndex !== -1 && state.slice(dotIndex + 1) === ADMIN_LOGIN_PURPOSE;
}

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  const settings = await prisma.appSettings.findUnique({ where: { id: 1 } });

  // Network-level restriction — checked before anything else, including the
  // login page itself, so a disallowed network never even sees the login
  // form. Off by default; see /settings/security.
  if (settings?.ipAllowlistEnabled) {
    const clientIp = extractClientIp(request.headers);
    if (!isIpAllowed(clientIp, settings.ipAllowlist)) {
      return new NextResponse("このネットワークからのアクセスは許可されていません。", {
        status: 403,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
  }

  if (PUBLIC_PATHS.includes(pathname)) return NextResponse.next();

  // Googleログインのコールバックだけは、Gmail/Drive連携フローと同じ
  // /gmail/oauth/callback を共用している(Google Cloud Consoleに登録済みの
  // redirect_uriが1本しかないため)。ログイン前に来るので認証は要求できない。
  //
  // **パス全体を公開にはしない。** stateのpurposeが admin_login のときだけ通す。
  // 全体を公開にすると、連携フローの保護がルート本体の getCurrentUser() だけになり
  // 多層防御が消える(2026-08-31、Geminiのレビュー指摘)。
  if (pathname === GOOGLE_LOGIN_CALLBACK_PATH && isAdminLoginCallback(request)) {
    return NextResponse.next();
  }

  const token = request.cookies.get(COOKIE_NAME)?.value;
  if (!isValidSessionToken(token)) {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  return NextResponse.next();
}

export const config = {
  // 画像等の静的アセット(public/配下、例: public/brand/*.png)を除外し忘れると、
  // ログイン前でも表示すべきロゴ画像までミドルウェアに横取りされ/loginへ
  // リダイレクトされてしまう(2026-08-15、実際にログイン画面のロゴが表示されない
  // 不具合として発覚。拡張子ベースで除外することで、今後public/配下に何を
  // 追加しても同じ事故が起きないようにする)。
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:png|jpg|jpeg|gif|webp|svg|ico)$).*)",
  ],
};
