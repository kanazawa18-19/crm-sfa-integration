# Google連携（Gmail+Drive統合フロー）

営業担当者向けのGmail連携（メール送受信の自動記録）とDrive連携（見積書の承認リクエスト送信、
[`docs/quote_approval_note.md`](./quote_approval_note.md)参照）は、もともと`/settings/gmail`・
`/settings/drive`から別々にOAuth同意を行う2本のフローだった。両方使う担当者にとっては2回同意
するのが手間なため、`gmail.readonly`・`drive`の両スコープを1回の同意画面でまとめて要求する
統合フロー（`/settings/google`、2026-08-27）を追加した。

## 構成

- `dashboard/lib/gmailOauth.ts` — Gmail単体フローの実装。`buildAuthUrlForScope()`（scope引数化
  版）と`exchangeCodeForToken()`（トークン交換、許可スコープも返す）を統合フロー向けに公開して
  いる。
- `dashboard/lib/googleOauth.ts` — 統合フロー用。`gmailOauth.ts`の上記2関数を再利用し、
  `gmail.readonly drive`の結合スコープでURLを組み立てるだけ。トークン交換ロジック自体は
  redirect_uriが同じなので完全に共通化できる。
- `dashboard/app/google/oauth/start/route.ts` — 統合フローのStep1。stateを
  `<nonce>.google_all`という形式で組み立てる（下記）。
- `dashboard/app/gmail/oauth/callback/route.ts` — Gmail単体フロー・統合フロー共通のStep2
  コールバック。stateのpurposeサフィックスで両フローを分岐する。なぜ新規callbackルートを
  作らずここを再利用しているかは[`app/google/oauth/README.md`](../dashboard/app/google/oauth/README.md)
  参照。
- `dashboard/app/(dashboard)/settings/google/page.tsx` — 統合ページ。実データは
  `RepGmailConnection`/`RepDriveConnection`をそれぞれ参照するだけで、専用テーブルは持たない。

## redirect_uriの再利用とGoogle Cloud Console

統合フローは新しいredirect_uriを登録せず、既存の`/gmail/oauth/callback`をそのまま使う
（Google Cloud Console側への追加登録漏れ・`redirect_uri_mismatch`のリスクを避けるため）。
そのため「どのフロー由来のコールバックか」をURLパスではなく`state`パラメータで判別する
必要がある。

## `state` の `nonce.purpose` 形式

- Gmail単体フロー（従来）: `state = <nonce>`（purposeなし）
- 統合フロー: `state = <nonce>.google_all`

cookie（`gmail_oauth_state`）にはnonceのみを保存し、purposeはstate文字列自体に載せて
コールバックまで運ぶ。コールバック側は`purpose`をallowlist（`ALLOWED_PURPOSES`、現状
`"google_all"`のみ）と突き合わせ、未知のpurposeは信頼できないため従来のGmailエラー
ページへフォールバックする。

`purpose`は現状1種類（`google_all`）しかないため、分岐はbooleanの`isGoogleAll`で行って
いる。**3つ目のpurposeを追加するタイミングでディスパッチテーブル化を検討すること**
（2種類のうちは過剰設計と判断し見送った、2026-08-27レビュー）。

## 許可スコープの検証（2026-08-27追加）

Googleの同意画面はスコープ単位でユーザーがチェックを外せる（granular permissions、
[公式ドキュメント](https://developers.google.com/identity/protocols/oauth2/web-server)）。
これを検証せずrefresh tokenをテーブルへ書き込むと、実際には許可されていない権限を
「連携済み」として扱ってしまう（例: 同意画面でDriveのチェックを外したのに
`RepDriveConnection`へトークンが入り、見積書承認リクエスト送信時に初めて権限不足で
失敗する）。

`exchangeCodeForToken()`はトークン交換レスポンスの`scope`（スペース区切りの許可済み
スコープ一覧）を`grantedScopes`として返す。コールバック側でこれを検証し:

- 統合フロー・両方許可 → 従来どおり`$transaction`で両テーブルへ同一トークンを書き込み
- 統合フロー・片方だけ許可 → 許可された側のテーブルだけに書き込み、`missing=gmail|drive`
  クエリパラメータで`/settings/google`へ「どちらが連携されなかったか」を伝える
- 統合フロー・どちらも許可なし → 何も書き込まず`error=scope_denied`
- Gmail単体フロー・`gmail.readonly`が許可されない → 同様に`error=scope_denied`
  （従来はここが未検証だった。単体フローでも同じ問題が起きうるため統合フローと
  同じ検証を入れた）

エラーメッセージ（`ERROR_MESSAGES_JA`）は`dashboard/lib/googleOauthErrors.ts`に集約し、
`settings/gmail`・`settings/drive`・`settings/google`の3画面から共通で参照する
（以前は3画面でほぼ同じ辞書がコピペされていた）。

## 最小権限からのトレードオフ（意図的に残している設計）

従来は`RepGmailConnection`のトークンは`gmail.readonly`のみ、`RepDriveConnection`の
トークンは`drive`のみと、テーブルごとに権限が最小化されていた。統合フローで両方
許可すると、**両テーブルに同じ「gmail.readonly + drive」の広いスコープを持つトークンが
入る**。トークンが漏洩した場合の被害範囲がGmail読み取りだけでなくDrive全体（ファイルの
読み書き）に広がる。

これは統合フローが1回の同意で両方をまとめて要求する以上、実装で消せない本質的な
トレードオフ。緩和策として:

- `/settings/google`の画面上で「片方しか使わないなら個別連携（`/settings/gmail`・
  `/settings/drive`）の方が権限を絞れる」ことを本文中の対等な選択肢として案内している
  （以前はグレーの補足テキストに格下げされ、実質全員をまとめて連携へ誘導していたが
  レビュー指摘で修正、2026-08-27）
- 既にどちらかだけ連携済みの状態で「まとめて連携する」を押すと、Gmailの同意も
  再度求められ（`prompt=consent`常時付与）、既存側のトークンも上書きされる旨を
  画面に明記している（差分追加ではない）

## 見送った設計（今回のスコープ外・将来課題）

- **`googleOauthCore.ts`のような中立な名前への共通モジュール切り出し**: `driveOauth.ts`
  （Drive単体フロー、`/settings/drive`・`/drive/oauth/*`）も巻き込む大きめのリファクタに
  なるため見送り。`driveOauth.ts`は`gmailOauth.ts`とほぼ同じ構造で重複しているが、
  Drive単体フローの改修は本note作成時点では対象外。将来3つ目のOAuthフローを増やす、
  または`driveOauth.ts`側にも同種の変更が必要になったタイミングで検討する。
- **master向け「全担当者の連携状況一覧」を統合ページにも作る件**: `/settings/gmail`に
  既にある機能（`allConnections`、master限定）の非移植。既存機能をなくしたわけではない
  （個別ページに残っている）ため今回は見送り。
- **別タブで2つの連携フローを同時開始した場合のcookie上書き**: `gmail_oauth_state`
  cookieは1つしかないため、別タブでGmail単体フローと統合フローを同時に開始すると
  片方のnonceが上書きされる。その場合は後から始めた方のcookieが残り、先に始めた方は
  コールバック時に`nonce`不一致で`invalid_state`となり安全に失敗する（誤ったテーブルに
  書き込まれることはない）。実害が「安全に失敗するだけ」のため対応は見送り、記録のみ。
- **`grantedScopes`の一致判定を部分一致へ緩める件（外部モデルレビュー(Gemini)、
  2026-08-27）**: Google側のスコープ表記に将来揺れがあった場合、`hasGmailScope`/
  `hasDriveScope`の完全一致判定が誤って`scope_denied`を返す脆さがあるという指摘。
  部分一致（`includes`ではなく`some(s => s.includes(...))`等）に緩めるとスコープ検証の
  意味自体が薄れる（「本当に許可されたスコープか」を確認する目的で入れた検証なので、
  ここを緩めるのは本末転倒）ため、完全一致のまま維持することにした。今回は対応しない。
