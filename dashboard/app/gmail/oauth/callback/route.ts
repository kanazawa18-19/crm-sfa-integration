import { NextRequest, NextResponse } from "next/server";
import prisma from "@/lib/prisma";
import { getCurrentUser } from "@/lib/auth";
import { encryptToken } from "@/lib/tokenCrypto";
import { exchangeCodeForRefreshToken } from "@/lib/gmailOauth";

const STATE_COOKIE = "gmail_oauth_state";

// Step 2 — exchanges the auth code for a refresh token and stores it
// (encrypted) against the current session's user, keyed by repEmail. Once
// connected, src/gmail_sync/ (Python backend) polls Gmail using this token
// and pushes matched send/receive events back to web-engagement-tool via
// POST /api/webhooks/crm-sfa-email.
export async function GET(request: NextRequest) {
  const user = await getCurrentUser();
  if (!user) return NextResponse.redirect(new URL("/login", request.url));

  const fail = (reason: string) =>
    NextResponse.redirect(new URL(`/settings/gmail?error=${encodeURIComponent(reason)}`, request.url));

  const code = request.nextUrl.searchParams.get("code");
  const state = request.nextUrl.searchParams.get("state");
  const expectedState = request.cookies.get(STATE_COOKIE)?.value;

  if (!code || !state || !expectedState || state !== expectedState) {
    return fail("invalid_state");
  }

  try {
    const refreshToken = await exchangeCodeForRefreshToken(code);
    await prisma.repGmailConnection.upsert({
      where: { repEmail: user.email },
      update: { refreshTokenEnc: encryptToken(refreshToken) },
      create: { repEmail: user.email, refreshTokenEnc: encryptToken(refreshToken) },
    });
  } catch (err) {
    console.error("Gmail OAuth callback failed", err);
    return fail("exchange_failed");
  }

  const response = NextResponse.redirect(new URL("/settings/gmail?connected=1", request.url));
  response.cookies.delete(STATE_COOKIE);
  return response;
}
