# 障害の日次通知の送達確定

中優先度の通知は、金沢さんのDMへ送り、Slackが HTTP 200 とJSONの `ok: true` を返した後にだけ
`EmailLog.digestedAt` を更新する。通知先未設定・通信失敗・受理拒否は例外を返し、
未処理のまま次回に残す。例外に認証情報・応答本文を含めない。リダイレクトは追わない。
2026-09-09のDM移行と現在の検証結果は [運用DM対応](operations_dm_delivery.md) を参照。
以下の2026-09-08の検証・レビューは、当時のIncoming Webhook実装の記録。

送信対象は作成日時・ID順の最大50件。送信中は同じDBトランザクションで行をロックし、
別実行は `FOR UPDATE SKIP LOCKED` でその行を飛ばす。送信後の更新も同じ接続で行う。
変更はテーブル構造に影響しない。残りは次回の定時実行で送る。
50件到達時はSlack本文に持ち越しの可能性を表示し、HTTP結果の
`batch_limit_reached`をtrueにする。これは上限到達の表示であり、残件の存在を断定しない。
件名・連絡先・担当営業は通知表示のみ短縮し、元データは保持する。

SlackとPostgresを一括確定することはできないため、Slackが受理した後の通信切断、
DB更新・commit失敗、プロセス停止では次回に重複送信し得る。
送信漏れ回避を優先する設計であり、厳密な一度限りの送達保証ではない。
日次50件を継続的に超える場合は未処理が積み残されるため、実行頻度の見直しが必要。

## 2026-09-08の検証

- 独立QAによる全pytest：2,758件成功、既存Starlette/httpx非推奨警告1件。
- 新しいローカルPostgres専用クラスタで、未設定時無更新、送信成功後commit、
  HTTP失敗/Timeout時rollbackと次回再送、同時2接続で同一行の送信1回を確認。
- 53件を初回50件・次回3件に分けて確定することを確認。
- 実Postgresの遅延制約トリガーでcommitを拒否し、未処理残存と再送を確認。
  この境界ではSlack受理済みの通知が再度送られることも確認した。
- 最終版APIのTestClientで成功200/失敗500、本文とtracebackへの偽秘密URL非露出を確認。
- SQLは製品実装を使用。接続だけ専用Unixソケットへ差替え、Slackは偽物。
  API試験は認証依存・DB取得・Slack送信を差し替え、routeとnotifyは製品実装。
- 独立セキュリティ/品質再レビュー：BLOCKER 0・WARN 0。
- 一時証跡：`/private/tmp/incident-qa.6BrLqi/` の`check.py`、`api_check.py`、
  `pytest-final.log`、`pytest-final2.log`。一時ファイルは永続保存の保証なし。QA専用Postgresは停止済み。
- 実Slack送達・本番cron・Neon接続プールは未検証。本番配備・試運転は未実施。
- 本人の具体的送信承認後、Gemini Pro / Claude Opus 5（高）で外部レビューを実施。
  同一本文の空白除外6,952文字・ハッシュ4040696799を両社の入力内容と照合。
  Claudeは添付テキストのプレビューで全文一致を確認し、先頭依頼文を入力欄に添えて送信。
- Geminiの通信失敗の識別改善を採用。例外本文や動的な型名を出さず、
  タイムアウト/接続失敗/その他の固定分類にした。追加修正後の独立SECはB0/W0、
  QA全2,758件・API200/500・秘密非露出を再確認。SQL不変のため実DB再試験なし。
  この追加修正版を他社へ再送はしていない。

## 外部レビューの採否

- [Gemini Pro](https://gemini.google.com/app/efd25a213ee8d241)：BLOCKERなし。
  通信失敗分類のWARNは上記対応。明示commitの削除は実害がなく処理境界が明確なため見送り。
- [Claude Opus 5](https://claude.ai/chat/c8244fd7-d4a0-4abe-8a74-a0b72eb1fc66)：
  次の2点をBLOCKERとしたが、要件・公式仕様・テストと照合して不採用と判断。
  1. 約15,000文字でSlackが切り捨てるとの指摘：
     [Slack公式](https://docs.slack.dev/changelog/2018-truncating-really-long-messages/)の
     Incoming Webhooksにも適用される切り捨て上限は40,000文字。
     項目ごとの文字数制限×最大50件と、2万文字未満のテストで上限内を確認済み。
     表示上の折り畳みと内容切り捨ては区別する。実Slack表示は未検証。
  2. HTTP200+本文okをやめて2xxだけで確定する案：
     [公式Incoming Webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks)
     の正常応答に合わせた判定を維持する。Workflow Builder等の別方式は今回対象外。
     別方式の受口を混用せず、配備前に対象受口がIncoming Webhooksであることを確認する。
- 両社のNULLメール指摘：Prisma定義でcontactEmail/repEmailは非NULL。
  件名NULLは既に「件名なし」で表示する。incidentScoreはNULL可のため不整合データでは
  None表示の余地があるが、送達欠落の問題とは分けて扱う。
- Claudeのロック中HTTP通信・DB切断/commit失敗による重複指摘は既知の制約。
  HTTPタイムアウトの実値は10秒。これは総実行時間の上限保証ではない。
  実測せずセッション設定を変更する案は見送り、Neonでの接続維持は配備前確認へ残す。
- 送信受理後のDB失敗の区別、詳細な構造化ログ、連続失敗検知は運用上の改善余地。
  今回はHTTP500と固定失敗分類、成功件数/上限表示まで。連続失敗アラートと
  実流入量/積み残しの確認を配備条件として残す。独立した監視実装・設定が必要。
- テストのRuntimeError捕捉改善・防御的WHERE追加等のINFOは、独立実DB/API試験と
  現行制約で今回の目的を確認できているため見送り。不要な改修の往復はしない。

## 反映時の注意

本番反映は別指示後。この修正を配備しても、旧実装で既に送信済み扱いにした行は
自動では復旧しない。`digestedAt`の一括リセットは、実際に届いた通知まで再送するため行わない。
過去の欠落回収はSlackと記録の照合を別イシューで行い、対象を確定してから判断する。
