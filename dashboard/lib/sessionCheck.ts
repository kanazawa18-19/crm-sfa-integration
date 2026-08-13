// セッション切れ検知（`redirect: "manual"`指定のfetchが未認証アクセスを/loginへ302
// リダイレクトする際、opaqueredirectとして観測される）の共通ロジック。
//
// documents/page.tsx（元々のBLOCKER修正: デフォルトのfetchはリダイレクトを追従するため、
// セッション切れ時に/loginのHTMLがそのまま「見積書.pdf」等としてダウンロードされてしまって
// いた）に2箇所、settings/page.tsxに1箇所と同じロジックが手動で複製されていたため、
// 共通ヘルパーへ切り出した（obasan-qualityレビュー指摘: WARN）。

export const SESSION_EXPIRED_MESSAGE =
  "セッションの有効期限が切れている可能性があります。再度ログインしてください。";

/**
 * `fetch(..., { redirect: "manual" })`のレスポンスが、認証切れによる/loginへのリダイレクト
 * （opaqueredirect）かどうかを判定する。
 */
export function isSessionExpiredResponse(response: Response): boolean {
  return response.type === "opaqueredirect" || response.status === 0;
}
