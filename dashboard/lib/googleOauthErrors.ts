// Gmail/Drive/Google(統合)のOAuth連携3画面(settings/gmail, settings/drive,
// settings/google)で一字一句同じエラーメッセージ辞書が重複していたため共通化した
// (2026-08-27、obasan-qualityレビュー指摘。今回スコープ部分許可対応でscope_deniedを
// 追加したのを機に切り出し)。
export const ERROR_MESSAGES_JA: Record<string, string> = {
  invalid_state: "連携セッションの有効期限が切れました。もう一度お試しください。",
  exchange_failed: "Googleとの連携処理に失敗しました。もう一度お試しください。",
  // Googleの同意画面はスコープ単位でチェックを外せる(granular permissions)。
  // 必要な権限にチェックが入っていなかった場合にこのエラーになる
  // (dashboard/app/gmail/oauth/callback/route.ts参照)。
  scope_denied: "必要な権限が同意画面で許可されませんでした。同意画面で必要な項目にチェックを入れて、もう一度お試しください。",
};

export function googleOauthErrorMessage(error: string): string {
  return ERROR_MESSAGES_JA[error] ?? "連携に失敗しました。もう一度お試しください。";
}
