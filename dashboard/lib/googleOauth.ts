// 営業担当者向けGoogle連携の統合フロー(Gmail+Drive、2026-08-27)。従来は
// /gmail/oauth/*と/drive/oauth/*で別々にOAuth同意を求めていたが、両方使う
// 担当者にとっては2回同意するのが手間なため、gmail.readonly・driveの両
// スコープを1回の認可URLでまとめて要求するこのフローを追加した。
//
// redirect_uriは新設せず、既存のGmail用/gmail/oauth/callback(lib/gmailOauth.ts
// 参照)をそのまま再利用する。新しいコールバックURIを作るとGoogle Cloud
// Console側への追加登録が必要になり、登録漏れでredirect_uri_mismatchになる
// リスクがあるため。/gmail/oauth/callbackというルート名がGmail由来なのは
// 歴史的経緯であり、この統合フローの認可コードもここに戻ってくる。
//
// トークン交換(exchangeCodeForToken/exchangeCodeForRefreshToken)はredirect_uriが
// 常にgmailOauth.tsと同じであるため、scopeの広さに関わらず全く同じ実装になる。
// 重複を増やさないため、gmailOauth.tsのものをそのままexportして再利用する。
import { buildAuthUrlForScope, exchangeCodeForRefreshToken, exchangeCodeForToken, GMAIL_SCOPE } from "@/lib/gmailOauth";

export const DRIVE_SCOPE = "https://www.googleapis.com/auth/drive";
const COMBINED_SCOPE = `${GMAIL_SCOPE} ${DRIVE_SCOPE}`;

export function buildAuthUrl(state: string): string {
  return buildAuthUrlForScope(COMBINED_SCOPE, state);
}

export { GMAIL_SCOPE, exchangeCodeForRefreshToken, exchangeCodeForToken };
