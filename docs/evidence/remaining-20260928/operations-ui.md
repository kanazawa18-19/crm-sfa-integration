# 商品関連保留の隔離UI確認

架空manager/viewer、localhost Postgres、実Next画面と実FastAPIルートで確認。対応表だけSQLite、実外部APIとcronは起動していない。

- 日本語保留理由と案件/商品/取引先リンクを確認。
- 「再開内容を確認」→「確認して再開」の2段階を実クリック。
- 送信中の両ボタンdisabledを確認。
- 実DB読戻しでevaluationHeld=false、pair.state=pending、履歴1件を確認。実配送完了ではなく再開予約。
- 初回表示エラーは検証サーバーの生成済Prismaクライアントが古かったことが原因。prisma generate＋当該localhostサーバー再起動後に解消。本番buildのgenerate手順を省かない。
