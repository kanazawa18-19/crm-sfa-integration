import { NextRequest, NextResponse } from "next/server";
import prisma from "@/lib/prisma";
import { getCurrentUser } from "@/lib/auth";
import { encryptToken } from "@/lib/tokenCrypto";
import { exchangeCodeForToken, GMAIL_SCOPE } from "@/lib/gmailOauth";
import { DRIVE_SCOPE } from "@/lib/googleOauth";
import {
  LOGIN_PURPOSE,
  LOGIN_STATE_COOKIE,
  LOGIN_STATE_COOKIE_PATH,
  exchangeCodeForGoogleIdentity,
} from "@/lib/googleLoginOauth";
import { establishSessionForUser } from "@/lib/loginSession";

const STATE_COOKIE = "gmail_oauth_state";

// state is `<nonce>` for the legacy Gmail-only flow (app/gmail/oauth/start)
// or `<nonce>.google_all` for the combined Gmail+Drive flow
// (app/google/oauth/start, 2026-08-27). Only the nonce is stored in the
// cookie — the purpose travels in the state param itself, matched against an
// allowlist below so an unrecognized purpose can't route this callback
// anywhere unexpected.
const ALLOWED_PURPOSES = new Set(["google_all"]);

// ログインフロー(app/login/google/start)。連携フローと同じredirect_uriを
// 共用しているので、GET()の先頭で分岐する。**このpurposeだけはセッションが
// 無い状態で来る**ため、getCurrentUser() より前に処理する必要がある。
// 定数は lib/googleLoginOauth.ts に集約している。

function parseState(state: string): { nonce: string; purpose: string | null } {
  const dotIndex = state.indexOf(".");
  if (dotIndex === -1) return { nonce: state, purpose: null };
  return { nonce: state.slice(0, dotIndex), purpose: state.slice(dotIndex + 1) };
}

// Step 2 — exchanges the auth code for a refresh token and stores it
// (encrypted) against the current session's user, keyed by repEmail. Once
// connected, src/gmail_sync/ (Python backend) polls Gmail using this token
// and pushes matched send/receive events back to web-engagement-tool via
// POST /api/webhooks/crm-sfa-email. Also handles the combined Google
// (Gmail+Drive) flow — see the state parsing above — since that flow
// deliberately reuses this same /gmail/oauth/callback redirect_uri instead of
// registering a second one in Google Cloud Console (lib/googleOauth.ts).
//
// Google's consent screen lets users uncheck individual scopes (granular
// permissions — https://developers.google.com/identity/protocols/oauth2/web-server
// says apps "must verify which scopes were actually granted"). We can't
// assume the requested scope(s) were granted just because Google returned an
// auth code, so exchangeCodeForToken()'s grantedScopes is checked below
// before writing anything — a table must never end up holding a refresh
// token that doesn't actually cover its scope (2026-08-27, review fix).

// 「Googleでログイン」の2歩目(2026-08-31)。
//
// 守っていること:
// - **アカウントを自動作成しない。** 既存のUserに一致するメールアドレスでなければ拒否する
// - **Google側で所有確認済み(verified_email)のメールしか信用しない。** 未確認の
//   メールアドレスは本人確認の材料にならない
// - **2FAを迂回しない。** establishSessionForUser()を通すので、AppSettingsで2FAが
//   ONなら、Googleでログインしても2FAの画面へ送られる
// - nonceは使い捨て。成否にかかわらずcookieを消す(残すと、後の試行で古いcookieが
//   nonce検証を満たしてしまう)
/**
 * Googleアカウントに対応するUserを探す（2026-08-31）。
 *
 * **emailは識別子にしない。** Workspaceでは退職者のアドレスを削除して同じアドレスで
 * 別アカウントを作り直せるため、emailだけで照合すると別人が旧ユーザーとしてログインできる
 * （ChatGPTのレビュー指摘。Google自身もOIDCの識別子にはsubを使うよう案内している）。
 *
 * 手順は trust on first use。
 * 1. `googleSubject` が一致するUserがいればそれ（以降はこちらが正）
 * 2. いなければemailで探し、そのUserがまだ束縛されていなければ**このsubで束縛する**
 * 3. 既に別のsubで束縛済みなら `"mismatch"` を返して拒否する
 */
async function findUserForGoogleIdentity(identity: {
  subject: string;
  email: string;
}): Promise<{ id: string } | null | "mismatch"> {
  const bySubject = await prisma.user.findUnique({
    where: { googleSubject: identity.subject },
  });
  if (bySubject) return bySubject;

  const byEmail = await prisma.user.findUnique({ where: { email: identity.email } });
  if (!byEmail) return null;
  if (byEmail.googleSubject && byEmail.googleSubject !== identity.subject) return "mismatch";

  // 初回ログイン。以降このUserはこのGoogleアカウントに固定される。
  await prisma.user.update({
    where: { id: byEmail.id },
    data: { googleSubject: identity.subject },
  });
  return byEmail;
}

async function handleAdminLogin(request: NextRequest): Promise<NextResponse> {
  const failLogin = (reason: string) => {
    const url = new URL("/login", request.url);
    url.searchParams.set("error", reason);
    const response = NextResponse.redirect(url);
    // **pathを明示しないと消えない。** cookieは path="/gmail/oauth" で発行されており、
    // delete()の既定は path="/" のため、指定しないと古いnonceが残って使い回せてしまう
    // （2026-08-31、Geminiのレビュー指摘）。
    response.cookies.delete({ name: LOGIN_STATE_COOKIE, path: LOGIN_STATE_COOKIE_PATH });
    return response;
  };

  const code = request.nextUrl.searchParams.get("code");
  const rawState = request.nextUrl.searchParams.get("state");
  const expectedNonce = request.cookies.get(LOGIN_STATE_COOKIE)?.value;
  const nonce = rawState ? parseState(rawState).nonce : null;

  if (!code || !rawState || !expectedNonce || nonce !== expectedNonce) {
    return failLogin("ログインの検証に失敗しました。もう一度お試しください");
  }

  let identity;
  try {
    identity = await exchangeCodeForGoogleIdentity(code);
  } catch (err) {
    console.error(err);
    return failLogin("Googleログインに失敗しました");
  }

  if (!identity.verifiedEmail) {
    return failLogin("このGoogleアカウントはメールアドレスが確認済みではありません");
  }

  const user = await findUserForGoogleIdentity(identity);
  if (user === "mismatch") {
    // このアドレスのCRMユーザーは、別のGoogleアカウントに束縛済み。
    // Workspaceでアドレスを作り直した場合にここへ来る。管理者が意図的に付け替える
    // 手段（googleSubjectのクリア）を用意するまでは、黙って通さない。
    return failLogin(
      "このGoogleアカウントは、同じメールアドレスの管理者アカウントに紐づいていません。管理者に連絡してください"
    );
  }
  if (!user) {
    // どのアドレスが登録済みかを推測させないため、理由は共通の文言にする。
    return failLogin("このGoogleアカウントに対応する管理者アカウントが見つかりません");
  }

  const { redirectTo } = await establishSessionForUser(user.id);
  const response = NextResponse.redirect(new URL(redirectTo, request.url));
  response.cookies.delete({ name: LOGIN_STATE_COOKIE, path: LOGIN_STATE_COOKIE_PATH });
  return response;
}

export async function GET(request: NextRequest) {
  const rawStateForLogin = request.nextUrl.searchParams.get("state");
  if (rawStateForLogin && parseState(rawStateForLogin).purpose === LOGIN_PURPOSE) {
    return handleAdminLogin(request);
  }

  const user = await getCurrentUser();
  if (!user) return NextResponse.redirect(new URL("/login", request.url));

  const code = request.nextUrl.searchParams.get("code");
  const rawState = request.nextUrl.searchParams.get("state");
  const expectedNonce = request.cookies.get(STATE_COOKIE)?.value;

  const { nonce, purpose } = rawState ? parseState(rawState) : { nonce: null, purpose: null };
  const isGoogleAll = purpose !== null && ALLOWED_PURPOSES.has(purpose);
  // Unknown purposes intentionally fall back to the legacy Gmail error page —
  // we can't trust an unrecognized purpose enough to route the failure
  // anywhere else, and it isn't the combined flow either way.
  const failTarget = isGoogleAll ? "/settings/google" : "/settings/gmail";
  const fail = (reason: string) => {
    const response = NextResponse.redirect(
      new URL(`${failTarget}?error=${encodeURIComponent(reason)}`, request.url)
    );
    // The nonce is single-use — leaving it on failure would let a stale
    // cookie satisfy the nonce check on a later retry attempt.
    response.cookies.delete(STATE_COOKIE);
    return response;
  };

  if (!code || !rawState || !expectedNonce || nonce !== expectedNonce) {
    return fail("invalid_state");
  }
  if (purpose !== null && !isGoogleAll) {
    return fail("invalid_state");
  }

  // Set when the combined flow only got one of the two scopes granted — the
  // write for that scope still succeeds, but /settings/google needs to know
  // which side was skipped so it can tell the user and offer a way to retry.
  let missing: "gmail" | "drive" | null = null;

  try {
    const { refreshToken, grantedScopes } = await exchangeCodeForToken(code);
    const refreshTokenEnc = encryptToken(refreshToken);
    const hasGmailScope = grantedScopes.includes(GMAIL_SCOPE);
    const hasDriveScope = grantedScopes.includes(DRIVE_SCOPE);

    if (isGoogleAll) {
      if (hasGmailScope && hasDriveScope) {
        // Both writes succeed or neither does — a partial write would leave
        // Gmail connected but Drive not (or vice versa) despite the user
        // having granted both scopes in the single consent screen.
        await prisma.$transaction([
          prisma.repGmailConnection.upsert({
            where: { repEmail: user.email },
            update: { refreshTokenEnc },
            create: { repEmail: user.email, refreshTokenEnc },
          }),
          prisma.repDriveConnection.upsert({
            where: { repEmail: user.email },
            update: { refreshTokenEnc },
            create: { repEmail: user.email, refreshTokenEnc },
          }),
        ]);
      } else if (hasGmailScope) {
        // Drive was unchecked on the consent screen — only write the table
        // for the scope that was actually granted.
        await prisma.repGmailConnection.upsert({
          where: { repEmail: user.email },
          update: { refreshTokenEnc },
          create: { repEmail: user.email, refreshTokenEnc },
        });
        missing = "drive";
      } else if (hasDriveScope) {
        await prisma.repDriveConnection.upsert({
          where: { repEmail: user.email },
          update: { refreshTokenEnc },
          create: { repEmail: user.email, refreshTokenEnc },
        });
        missing = "gmail";
      } else {
        return fail("scope_denied");
      }
    } else {
      // Legacy Gmail-only flow has the same granular-permission risk with a
      // single scope: if gmail.readonly is unchecked, Google still returns a
      // code, and without this check we'd mark Gmail "connected" with a
      // token that can't actually read Gmail.
      if (!hasGmailScope) {
        return fail("scope_denied");
      }
      await prisma.repGmailConnection.upsert({
        where: { repEmail: user.email },
        update: { refreshTokenEnc },
        create: { repEmail: user.email, refreshTokenEnc },
      });
    }
  } catch (err) {
    console.error("Gmail/Google OAuth callback failed", err);
    return fail("exchange_failed");
  }

  const successUrl = new URL(isGoogleAll ? "/settings/google" : "/settings/gmail", request.url);
  successUrl.searchParams.set("connected", "1");
  if (missing) successUrl.searchParams.set("missing", missing);

  const response = NextResponse.redirect(successUrl);
  response.cookies.delete(STATE_COOKIE);
  return response;
}
