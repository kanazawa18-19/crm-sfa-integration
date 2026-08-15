import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { COOKIE_NAME, isValidSessionToken } from "@/lib/adminSession";
import { isIpAllowed, extractClientIp } from "@/lib/ipAllowlist";
import prisma from "@/lib/prisma";

// Next.js 16 では middleware.ts は非推奨となり proxy.ts にリネームされている。
// 動作・実行タイミングは旧 middleware と同等（Node.js ランタイムがデフォルト）。
// web-engagement-toolのsrc/proxy.tsと同じ構成に移植(2026-08-15、ユーザー管理・IP制限・
// 2FA導入に伴う置き換え)。

const PUBLIC_PATHS = ["/login", "/forgot-password", "/set-password", "/login/2fa", "/login/2fa-setup", "/login/2fa-email"];

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

  const token = request.cookies.get(COOKIE_NAME)?.value;
  if (!isValidSessionToken(token)) {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
