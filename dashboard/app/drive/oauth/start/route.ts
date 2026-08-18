import { randomBytes } from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { buildAuthUrl } from "@/lib/driveOauth";

const STATE_COOKIE = "drive_oauth_state";

// Step 1 of the per-user Drive connect flow (see app/drive/oauth/callback and
// app/(dashboard)/settings/drive). Mirrors app/gmail/oauth/start (2026-08-18,
// 見積書 承認フロー). proxy.ts already requires a logged-in session for this
// path, but getCurrentUser() is re-checked here since the repEmail written to
// RepDriveConnection on callback comes from the session, not anything
// client-supplied.
export async function GET(request: NextRequest) {
  const user = await getCurrentUser();
  if (!user) return NextResponse.redirect(new URL("/login", request.url));

  const state = randomBytes(16).toString("hex");
  const response = NextResponse.redirect(buildAuthUrl(state));
  response.cookies.set(STATE_COOKIE, state, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    maxAge: 600,
    path: "/drive/oauth",
  });
  return response;
}
