// EmailLog.gmailMessageId(Gmail API `users.messages.get`が返すメッセージID)から、
// Gmailの該当メールをWeb UIで直接開けるURLを組み立てる(2026-08-26新設、金沢さんからの
// 「Gmail本文を直接開けるようにしたい」要望対応)。本文全文はDBに保存していないため
// (snippetのみ)、今回はこのリンクで代替する(ユーザー確認済み、DBスキーマ変更・
// 本文全文取得はスコープ外)。
//
// `#all/<messageId>`形式は、そのメッセージが属するラベル(受信トレイ/送信済み/
// アーカイブ等)によらず開ける(「すべてのメール」ビュー内をメッセージIDで直接指す
// ため)。threadIdは不要で、gmailMessageId単体で成立する。
// `u/0`は「そのブラウザで最初にログインしているGoogleアカウント」を指す固定値。
// Gmail連携は営業担当者ごとの個人接続のため、閲覧者が複数のGoogleアカウントを
// 使い分けている場合は目的のメールが開けない(別アカウントに切り替える必要がある)
// ことがある。これはURL形式そのものの制約であり、閲覧者側のブラウザ設定に依存する。
export function gmailMessageUrl(gmailMessageId: string): string {
  return `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(gmailMessageId)}`;
}
