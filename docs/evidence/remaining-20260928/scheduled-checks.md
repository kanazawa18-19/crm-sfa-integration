# 定期実行の実観測

- 2026-09-28 11:30 JST：商品関連cron修正後のHTTP 200を確認。
- 2026-09-28 12:30:18 JST：`/api/cron/project-product-links` GET HTTP 200。Vercel本番request log、deployment `dpl_8d7UA5qw8FwbXjBmpvQS9H87tBt4`、request `hjms9-1790566218256-24fb0d756a7e`。対象処理は手動起動していない。
- これは実行応答の確認。実Notion関連書込・商品シート配送の成功を示すものではない。
- 9/28週次6DBは06:17:05〜07:00:38 JST、2613.4秒で完走。Slack読み取り連携で本人DMの保存済み通知本文を取得し、6DBの照合結果と作成保留0件を確認した（画面の目視ではなく実メッセージ取得）。[週次通知](https://cnctor.slack.com/archives/D0BNE3Y0P9Q/p1790546438516599)
- 9/28 00:48:08 JSTの日次監視も、本人DMの保存済み通知本文を取得できた。受信状態と診断結果を報告していることを確認。通知の受信は個々の同期成功を意味しない。[日次監視通知](https://cnctor.slack.com/archives/D0BNE3Y0P9Q/p1790524088897199)
- 日次掃除の次回19:00は観測前。手動再実行しない。
- 未返信通知の修正後定期実行は9/28 09:21 JSTのGitHub Actions [36361937929](https://github.com/kanazawa18-19/crm-sfa-integration/actions/runs/36361937929)成功。実ログにHTTP応答のfailed=0検査があり、対応する本人DM・DB記録1件の時刻/相手/件名も一致した（notification-delivery.md）。
- 暗号鍵診断は9/28 10:00:03.410 JST、`/api/cron/token-encryption-healthcheck` GET HTTP 200をVercel本番ログで確認。deployment `dpl_Er4sWPJrf4GwD8Wbh2dbDuLDyhdB`、request `skkgn-1790557203410-c8148c83bf00`。取得ログに応答本文はなく、本文中の個別診断結果までは未確認。Cloud Run側の継続実行はgcloud認証待ち。
