import { randomBytes } from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { getCurrentUser } from "@/lib/auth";
import { buildAuthUrl } from "@/lib/googleOauth";

const STATE_COOKIE = "gmail_oauth_state";
const PURPOSE = "google_all";

// Step 1 of the combined Gmail+Drive connect flow (see
// app/gmail/oauth/callback, which branches on the state's purpose suffix to
// handle both this flow and the legacy Gmail-only flow, and
// app/(dashboard)/settings/google). Deliberately reuses the existing
// gmail_oauth_state cookie/path — the callback this flow returns to is
// /gmail/oauth/callback (see lib/googleOauth.ts for why no new redirect_uri
// was added), so the cookie needs to be visible under /gmail/oauth just like
// app/gmail/oauth/start's. proxy.ts already requires a logged-in session for
// this path, but getCurrentUser() is re-checked here since the repEmail
// written to RepGmailConnection/RepDriveConnection on callback comes from the
// session, not anything client-supplied.
export async function GET(request: NextRequest) {
  const user = await getCurrentUser();
  if (!user) return NextResponse.redirect(new URL("/login", request.url));

  const nonce = randomBytes(16).toString("hex");
  const state = `${nonce}.${PURPOSE}`;
  const response = NextResponse.redirect(buildAuthUrl(state));
  response.cookies.set(STATE_COOKIE, nonce, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    maxAge: 600,
    path: "/gmail/oauth",
  });
  return response;
}
