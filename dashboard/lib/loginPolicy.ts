// 管理画面にログインできるのは cnctor.jp の Google アカウントだけ（2026-09-16 本人指示）。
//
// 守るべき性質は2つ。
// 1. Google ログインは、メールのドメインと Google Workspace の所属ドメイン（hd）の
//    両方が cnctor.jp のときだけ通す。メールだけを見ると、個人の Google アカウントに
//    「@cnctor.jp」のアドレスを登録した場合を区別できないため、hd も要求する。
// 2. パスワードログインは廃止。画面から消すだけでは Server Action が公開エンドポイント
//    として残るので、login() 自体が必ず拒否する（判定ではなく定数の文言をここに置く）。
//
// この関数は純粋関数。DB も Google も呼ばないので、単体テストで判定表を固定できる。
// web-engagement-tool の src/lib/loginPolicy.ts と同じ内容（両システム共通の方針）。

export const ALLOWED_LOGIN_DOMAIN = "cnctor.jp";

/** Google ログインの nonce を入れる cookie。連携フロー（カレンダー等）とは別名にする。 */
export const LOGIN_STATE_COOKIE = "admin_login_oauth_state";
/** cookie の path。削除するときも同じ path を渡さないと消えない。 */
export const LOGIN_STATE_COOKIE_PATH = "/gmail/oauth";

export const GOOGLE_ONLY_LOGIN_MESSAGE =
  `パスワードでのログインは廃止しました。${ALLOWED_LOGIN_DOMAIN} の Google アカウントで「Googleでログイン」を押してください`;

export const DOMAIN_REJECTED_MESSAGE =
  `${ALLOWED_LOGIN_DOMAIN} の Google アカウントでのみログインできます`;

export const HOSTED_DOMAIN_REJECTED_MESSAGE =
  `${ALLOWED_LOGIN_DOMAIN} の Google Workspace アカウントとして確認できませんでした（個人の Google アカウントではログインできません）`;

export const INVITE_DOMAIN_REJECTED_MESSAGE =
  `招待できるのは ${ALLOWED_LOGIN_DOMAIN} のメールアドレスだけです`;

export const EMAIL_CHANGE_DISABLED_MESSAGE =
  `メールアドレスの変更は受け付けていません。ログインは ${ALLOWED_LOGIN_DOMAIN} の Google アカウントで行うため、変えたいときは管理者に新しいアドレスで招待し直してもらってください`;

export const GOOGLE_ACCOUNT_MISMATCH_MESSAGE =
  "このGoogleアカウントは、同じメールアドレスの管理者アカウントに紐づいていません。管理者にユーザー管理から招待し直してもらってください";

/** メールアドレスの @ より後ろ（小文字）。形が崩れていれば null。 */
export function emailDomainOf(email: string): string | null {
  const normalized = email.trim().toLowerCase();
  const at = normalized.lastIndexOf("@");
  if (at <= 0 || at === normalized.length - 1) return null;
  return normalized.slice(at + 1);
}

/**
 * 招待できるアドレスか（ログインできない人を招待しないための入口の判定）。
 * 形は「空白なしのローカル部 @ ドメイン」に限る。`a@evil@cnctor.jp` のように @ が 2 つある
 * 文字列は最後の @ だけ見ると通ってしまう（ChatGPT レビュー INFO）ので、ここで弾く。
 */
export function isAllowedLoginEmail(email: string): boolean {
  const normalized = email.trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+$/.test(normalized)) return false;
  return emailDomainOf(normalized) === ALLOWED_LOGIN_DOMAIN;
}

export type GoogleAccountForLogin = {
  email: string;
  /** Google が返す hd（Workspace の所属ドメイン）。個人アカウントでは付かない。 */
  hostedDomain?: string | null;
};

export type GoogleAccountCheck = { ok: true } | { ok: false; reason: string };

/**
 * Google アカウントがログインを許されるドメインか。
 * メールの所有確認（verified_email）は Google 側の別の性質なので、呼び出し元で先に見る。
 */
export function checkGoogleAccountDomain(account: GoogleAccountForLogin): GoogleAccountCheck {
  if (emailDomainOf(account.email) !== ALLOWED_LOGIN_DOMAIN) {
    return { ok: false, reason: DOMAIN_REJECTED_MESSAGE };
  }
  const hd = (account.hostedDomain ?? "").trim().toLowerCase();
  if (hd !== ALLOWED_LOGIN_DOMAIN) {
    return { ok: false, reason: HOSTED_DOMAIN_REJECTED_MESSAGE };
  }
  return { ok: true };
}

/**
 * 招待を承諾して「有効になった」ユーザーか。
 * 以前は passwordHash の有無で見ていたが、Google だけのログインでは passwordHash が
 * 永遠に null のままなので、ログイン成立日時（lastLoginAt）も有効の印にする。
 * 「招待中」表示と、有効な管理者を削除させない保護がこれを使う（2026-09-16）。
 */
export function isActivatedUser(user: {
  passwordHash: string | null;
  lastLoginAt: Date | null;
  googleSubject?: string | null;
}): boolean {
  return user.passwordHash !== null || user.lastLoginAt !== null || !!user.googleSubject;
}
