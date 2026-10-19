# 外部親取込の独立検証

書き手 `gmail_checks` と、読み取り専用の安全レビュー `recovery_design`・運用レビュー `slack_checks`、rootの試験実行を分離した。

- 初回の共通指摘：運用側の予約だけでは通常Zoho通知の再作成を止められず、Notion通知はHubの外部新規登録を起こし得た。両方の入口で専用予約を認識する修正を追加。
- 安全レビュー：最終BLOCKER 0。同時に別の外部IDを取り込むための名称全体の鍵はないので、今回の固定1対象を直列実行する。未確定予約を完了扱いにしない。
- 運用レビュー：最終BLOCKER 0 / WARN 0。汎用英単語だけの候補検索と、apply時の別ID引数の無視を修正。ID未確定中の未登録Notion取引先の保留範囲は運用書へ記載。
- root独立QA：新規運用試験、hub_creation全体、関連承認、production_wiringの合計477成功・69skip・失敗0。追加でローカルPostgreSQLの使い捨てschemaによる専用23ケースを実行し、23成功・失敗0。
- 実物プレビュー：管理者・元Zoho親ID・元Notionの空関連・候補全ページ・対応表・原文忠実な8項目を確認。本番への予約/POSTはガードの配備後に行う。

認証情報、顧客の原本・IDは本資料に含めない。実物の詳細は私有証跡に保存する。

## 他社レビュー後の追加確認

Claudeの「作成済みページの再開を元アクション全体の変更で止めない」指摘を採用し、POST前と作成後の照合条件を分離した。安全/運用の両者が追加差分を確認しBLOCKER 0。rootでもローカルPostgreSQLを含む30ケースが成功。

- Claude Opus 5.5・中：初回B1修正後、BLOCKER 0。URLは `https://claude.ai/chat/126084f3-9a20-417d-9f3a-b219d2c90683`。回答原文は `parent-import-claude*.txt`。
- 未確定予約中は同DBの未登録Notion取引先を保留する。今回固定1件の監視下で実行し、結果不明の場合は確認番号調査を継続する。未確定のまま完了報告しない。
- `NotionClient.create_page_once` は実コード574行で `children` を受け取り、`idempotent=False` のPOST本文へ格納することを確認済み。
- 対応表の検索反映が遅れた場合も既知IDから再確認する。再POSTしない。適用前にkintoneの同一顧客を実読し、通常CRM作成を誘発させない。

- Gemini 思考モード：最終BLOCKER 0 / WARN 2。先行配備と未確定予約中の保留範囲は上記運用で対応する。回答中の「実機テスト」はローカルPostgreSQLを用いた試験を指し、本番動作検証とは区別する。URL: `https://gemini.google.com/app/40e0f68bbc6b668d?hl=ja`。最終原文は `parent-import-gemini-final.txt`。
