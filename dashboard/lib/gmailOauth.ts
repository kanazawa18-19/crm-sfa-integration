// crm-sfa-integration専用のGmail OAuth接続フロー(2026-08-16)。MA
// (web-engagement-tool)と同じGoogle Cloud OAuthクライアント(GOOGLE_OAUTH_
// CLIENT_ID/SECRET)を共用するが、リダイレクトURIは本アプリ専用の
// /gmail/oauth/callback（Google Cloud Console側でMA用URIとは別に追加登録
// 済み）。googleapis SDKは使わず、直接HTTPで叩く(Python側のgmail_client.py
// と同じ方針、新規の重い依存を増やさない)。
//
// 2026-08-27: lib/googleOauth.ts(Gmail+Drive統合連携フロー)から
// buildAuthUrlForScope/exchangeCodeForTokenを再利用している。統合フロー
// もredirect_uriはこのファイルの/gmail/oauth/callbackをそのまま使う(新規URIを
// Google Cloud Consoleへ追加登録しないため)ので、トークン交換ロジックは重複させず
// このファイルのものをそのままexportする。
export const GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly";

// Googleログイン(lib/googleLoginOauth.ts)からも参照するためexportしている。
// **このアプリのredirect_uriはこの1本だけ**で、Google Cloud Console側にも
// これしか登録していない。新しいフローを足すときもここを再利用し、
// stateのpurposeで分岐すること(app/google/oauth/README.md参照)。
export function redirectUri(): string {
  const base = process.env.APP_BASE_URL;
  if (!base) throw new Error("APP_BASE_URL is not set");
  return `${base}/gmail/oauth/callback`;
}

// scopeを引数化した版。lib/googleOauth.tsがgmail.readonly+driveの結合scopeで
// 呼び出すために公開している(redirect_uriは常にこのファイルの/gmail/oauth/callback)。
export function buildAuthUrlForScope(scope: string, state: string): string {
  const clientId = process.env.GOOGLE_OAUTH_CLIENT_ID;
  if (!clientId) throw new Error("GOOGLE_OAUTH_CLIENT_ID is not set");

  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri(),
    response_type: "code",
    access_type: "offline",
    prompt: "consent",
    scope,
    state,
  });
  return `https://accounts.google.com/o/oauth2/v2/auth?${params.toString()}`;
}

export function buildAuthUrl(state: string): string {
  return buildAuthUrlForScope(GMAIL_SCOPE, state);
}

export interface TokenExchangeResult {
  refreshToken: string;
  // Googleの同意画面はスコープ単位でユーザーがチェックを外せる(granular
  // permissions、https://developers.google.com/identity/protocols/oauth2/web-server
  // に明記)。トークン交換のレスポンスに含まれるscope(スペース区切りの
  // 許可済みスコープ一覧)を配列化して返す — 呼び出し元はリクエストした
  // スコープが実際に許可されたかをここで検証し、許可されなかった機能を
  // 「連携済み」として扱わないようにする(2026-08-27、レビュー指摘対応)。
  grantedScopes: string[];
}

export async function exchangeCodeForToken(code: string): Promise<TokenExchangeResult> {
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

  const json = (await response.json()) as { refresh_token?: string; scope?: string };
  if (!json.refresh_token) {
    // Google only returns a refresh_token on the first consent (or with
    // prompt=consent forcing re-consent, which buildAuthUrl always sets) —
    // if this still happens, prior access needs manual revocation first.
    throw new Error(
      "Google did not return a refresh_token — revoke prior access at https://myaccount.google.com/permissions and retry"
    );
  }
  const grantedScopes = (json.scope ?? "").split(" ").filter(Boolean);
  return { refreshToken: json.refresh_token, grantedScopes };
}

// 2026-08-27以前からの呼び出し元向けの互換ラッパー。スコープ検証が不要な
// 箇所向けに残しているが、新規の呼び出し元はexchangeCodeForToken()を使い、
// 許可スコープを確認すること。
export async function exchangeCodeForRefreshToken(code: string): Promise<string> {
  const { refreshToken } = await exchangeCodeForToken(code);
  return refreshToken;
}
