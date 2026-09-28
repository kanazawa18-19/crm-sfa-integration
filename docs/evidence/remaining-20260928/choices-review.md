# 選択同期のレビューと検証

2026-09-28。対象はファーストタッチ、サイトコントローラー、Q024案件名、提案サービスの同期範囲。担当者や専用欄の残対応、二段階承認は別区分。

- 独立sec/quality/QAを実施。保存枠なしの複数選択減少、メモ本文と保存枠の同時編集、Q024逆通知での取引先誤関連、空欄案件名の無承認削除を修正。
- 最終対象251テスト成功。前段の全Pythonは3,748成功/12skipだが最終変更後の全件再実行ではない。
- [Claude Opus 5.5・中](https://claude.ai/chat/5e555575-1942-4512-97d7-6ddd458f52d6)、[Gemini 3.6 Thinking](https://gemini.google.com/app/ff18d3b2d2253e84?hl=ja)の最終コードレビューでBLOCKER0。
- Zoho型に関する推測指摘は実メタデータと照合して不採用。field20は全8送信候補が存在、field45は専用text(255)、field70はtextarea(2000)。changed_values=Noneは呼出元のMapping検証により到達しない。
- Q024は送受とも案件名に統一。現行Notion対応表でprojectかつkintone_id非空は0件/has_more=false。今朝の週次26,067案件も全件kintone対応なし。古いSQLiteのzcrm_移行IDは現行kintone番号として扱わない。
- kintone通知は全項目を含むため、将来案件を関連付ける際は案件名の現値比較を行う。現行の通常同期は項目差分の履歴を持たず、レコード更新時刻による比較である。
- 本番の選択項目試験書込は未実施。Q058を商品/案件の新試験へ拡大しない。

商品cronの先行修正ea8c27fは本番Ready、CI36369055224成功。11:30JSTの定期GET /api/cron/project-product-linksはHTTP200。業務データの配送試験完了とは区別する。
