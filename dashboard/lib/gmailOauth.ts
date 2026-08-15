// crm-sfa-integration専用のGmail OAuth接続フロー(2026-08-16)。MA
// (web-engagement-tool)と同じGoogle Cloud OAuthクライアント(GOOGLE_OAUTH_
// CLIENT_ID/SECRET)を共用するが、リダイレクトURIは本アプリ専用の
// /gmail/oauth/callback（Google Cloud Console側でMA用URIとは別に追加登録
// 済み）。googleapis SDKは使わず、直接HTTPで叩く(Python側のgmail_client.py
// と同じ方針、新規の重い依存を増やさない)。
const GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly";

function redirectUri(): string {
  const base = process.env.APP_BASE_URL;
  if (!base) throw new Error("APP_BASE_URL is not set");
  return `${base}/gmail/oauth/callback`;
}

export function buildAuthUrl(state: string): string {
  const clientId = process.env.GOOGLE_OAUTH_CLIENT_ID;
  if (!clientId) throw new Error("GOOGLE_OAUTH_CLIENT_ID is not set");

  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri(),
    response_type: "code",
    access_type: "offline",
    prompt: "consent",
    scope: GMAIL_SCOPE,
    state,
  });
  return `https://accounts.google.com/o/oauth2/v2/auth?${params.toString()}`;
}

export async function exchangeCodeForRefreshToken(code: string): Promise<string> {
  const clientId = process.env.GOOGLE_OAUTH_CLIENT_ID;
  const clientSecret = process.env.GOOGLE_OAUTH_CLIENT_SECRET;
  if (!clientId || !clientSecret) throw new Error("Google OAuth client credentials are not set");

  const response = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      code,
      client_id: clientId,
      client_secret: clientSecret,
      redirect_uri: redirectUri(),
      grant_type: "authorization_code",
    }),
  });
  if (!response.ok) {
    throw new Error(`Google token exchange failed: ${response.status}`);
  }

  const json = (await response.json()) as { refresh_token?: string };
  if (!json.refresh_token) {
    // Google only returns a refresh_token on the first consent (or with
    // prompt=consent forcing re-consent, which buildAuthUrl always sets) —
    // if this still happens, prior access needs manual revocation first.
    throw new Error(
      "Google did not return a refresh_token — revoke prior access at https://myaccount.google.com/permissions and retry"
    );
  }
  return json.refresh_token;
}
