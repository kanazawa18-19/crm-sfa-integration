import { NextRequest, NextResponse } from "next/server";
import prisma from "@/lib/prisma";
import { getCurrentUser } from "@/lib/auth";
import { encryptToken } from "@/lib/tokenCrypto";
import { exchangeCodeForRefreshToken } from "@/lib/driveOauth";

const STATE_COOKIE = "drive_oauth_state";

// Step 2 — exchanges the auth code for a refresh token and stores it
// (encrypted) against the current session's user, keyed by repEmail. Mirrors
// app/gmail/oauth/callback (2026-08-18, 見積書 承認フロー). Once connected,
// src/document_generation/request_quote_approval() (Python backend) uses this
// token to send Drive's native approval requests from the rep's own account
// (canStartApproval is false for the service account — see plan doc).
export async function GET(request: NextRequest) {
  const user = await getCurrentUser();
  if (!user) return NextResponse.redirect(new URL("/login", request.url));

  const fail = (reason: string) =>
    NextResponse.redirect(new URL(`/settings/drive?error=${encodeURIComponent(reason)}`, request.url));

  const code = request.nextUrl.searchParams.get("code");
  const state = request.nextUrl.searchParams.get("state");
  const expectedState = request.cookies.get(STATE_COOKIE)?.value;

  if (!code || !state || !expectedState || state !== expectedState) {
    return fail("invalid_state");
  }

  try {
    const refreshToken = await exchangeCodeForRefreshToken(code);
    await prisma.repDriveConnection.upsert({
      where: { repEmail: user.email },
      update: { refreshTokenEnc: encryptToken(refreshToken) },
      create: { repEmail: user.email, refreshTokenEnc: encryptToken(refreshToken) },
    });
  } catch (err) {
    console.error("Drive OAuth callback failed", err);
    return fail("exchange_failed");
  }

  const response = NextResponse.redirect(new URL("/settings/drive?connected=1", request.url));
  response.cookies.delete(STATE_COOKIE);
  return response;
}
