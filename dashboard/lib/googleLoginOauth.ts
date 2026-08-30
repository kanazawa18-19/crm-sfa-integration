// 管理画面への「Googleでログイン」フロー(2026-08-31)。
//
// **既存のredirect_uri `/gmail/oauth/callback` をそのまま再利用する。**
// Google Cloud Console に登録済みのURIはこれ1本だけで、新しいURIを足すと
// 登録漏れが `redirect_uri_mismatch` として本番でだけ現れる
// (app/google/oauth/README.md に同じ理由が書いてある)。
// そのため state に `<nonce>.admin_login` という印を付け、コールバック側で分岐する。
//
// Gmail/Drive連携フローとの決定的な違いは2つ。
// 1. **refresh_tokenを取らない。** ログインは「今このGoogleアカウントの本人である」
//    ことを1回確認できれば十分で、継続的にAPIを叩く必要がない。
//    `access_type=offline` と `prompt=consent` を付けないのはこのため
//    (付けると毎回同意画面が出た上に、使わないrefresh_tokenを受け取ってしまう)。
// 2. **セッションが無い状態で走る。** 連携フローはログイン済みが前提だが、
//    こちらはログインする手段そのもの。コールバック側でこの分岐だけは
//    `getCurrentUser()` の前に処理する必要がある。
import { redirectUri } from "@/lib/gmailOauth";

/** ログインに必要な最小のスコープ。カレンダーもGmailも要求しない。 */
export const LOGIN_SCOPES = ["openid", "email", "profile"].join(" ");

export function buildLoginAuthUrl(state: string): string {
  const clientId = process.env.GOOGLE_OAUTH_CLIENT_ID;
  if (!clientId) throw new Error("GOOGLE_OAUTH_CLIENT_ID is not set");

  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri(),
    response_type: "code",
    scope: LOGIN_SCOPES,
    state,
    // 別のGoogleアカウントでログインし直せるように、毎回アカウント選択を出す。
    prompt: "select_account",
  });
  return `https://accounts.google.com/o/oauth2/v2/auth?${params.toString()}`;
}

export interface GoogleIdentity {
  email: string;
  /** Google側でメールアドレスの所有が確認済みか。falseなら本人確認の材料にならない。 */
  verifiedEmail: boolean;
}

/**
 * 認可コードをアクセストークンに交換し、Googleアカウントのメールアドレスを読む。
 *
 * `id_token`(JWT)を自前で検証するのではなく、**Googleのトークンエンドポイントから
 * TLSで直接受け取ったアクセストークン**でuserinfoを引く。署名検証のための鍵取得と
 * 失効管理を持ち込まずに済み、経路自体がGoogleとの直接通信なので改ざんの余地がない。
 */
export async function exchangeCodeForGoogleIdentity(code: string): Promise<GoogleIdentity> {
  const clientId = process.env.GOOGLE_OAUTH_CLIENT_ID;
  const clientSecret = process.env.GOOGLE_OAUTH_CLIENT_SECRET;
  if (!clientId || !clientSecret) throw new Error("Google OAuth client credentials are not set");

  const tokenResponse = await fetch("https://oauth2.googleapis.com/token", {
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
  if (!tokenResponse.ok) {
    throw new Error(`Google token exchange failed: ${tokenResponse.status}`);
  }

  const token = (await tokenResponse.json()) as { access_token?: string };
  if (!token.access_token) {
    throw new Error("Google did not return an access_token");
  }

  const userInfoResponse = await fetch("https://www.googleapis.com/oauth2/v2/userinfo", {
    headers: { Authorization: `Bearer ${token.access_token}` },
  });
  if (!userInfoResponse.ok) {
    throw new Error(`Google userinfo request failed: ${userInfoResponse.status}`);
  }

  const info = (await userInfoResponse.json()) as { email?: string; verified_email?: boolean };
  const email = (info.email ?? "").trim().toLowerCase();
  if (!email) {
    throw new Error("Googleアカウントのメールアドレスを取得できませんでした");
  }
  return { email, verifiedEmail: info.verified_email === true };
}
