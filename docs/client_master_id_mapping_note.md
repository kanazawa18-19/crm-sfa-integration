# 取引先マスター（client_master）kintone⇔Zoho IDマッピング欠落と再構築（2026-08-13）

## 背景

`src/migration/migration_pipeline.py`の`plan_migration()`docstringに明記されている通り、
取引先マスター（client_master）は6DBの中で唯一、移行時にZohoレコードを**既存のkintone由来
Notionページへマージ**する（新規ページを作らない）設計になっている。これは金沢さん確認済みの
方針であり、他5DB（案件・アクション・チェーン・連絡先・サービス・商品）はZoho由来データも
常に新規ページを作成する。

## 発覚した問題

[`docs/id_mapping_persistence_note.md`](id_mapping_persistence_note.md)のNotion裏付けIDマッピング
ストアを2026-08-13に調査したところ、client_masterについて、kintone由来Notionページ
（約61,943件）に対応するマッピング行が**1件も存在しない**ことが判明した。単に`zoho_id`列が
空欄なのではなく、行自体が存在しなかった（IDマッピングストアはZoho側からの一方向backfillでしか
埋まっておらず、移行時にマージされたkintone由来ページの情報が反映されていなかった）。

このままではZoho側でこれらの会社データが更新されても、Webhookが対応するNotionページを特定
できず、最悪の場合は重複ページが作成されるリスクがあった。

## 再構築の手順

1. `migration_output/migration_id_mapping.db`（旧SQLite移行データ、Notionクエリのページング
   信頼性上限（大規模DBで約1万件を超えると`has_more`が正しく返らない既知の問題）を回避する
   ため、Notion API検索ではなくこちらから直接候補を抽出）から、kintone由来client_masterの
   61,943件を特定。
2. 各Notionページを個別に`GET /v1/pages/{id}`で取得し、会社名・郵便番号を収集
   （`src/migration/notion_dedupe.py`の`fetch_client_master_snapshots`と同じ発想だが、
   `query_all_pages()`のページング上限を回避するため個別取得方式に変更）。
3. Zoho取引先CSVエクスポート（37,446件）と、既存の名寄せロジック
   （`src/migration/notion_dedupe.py`の`match_existing_client`、会社名＋郵便番号の
   ファジーマッチ、元の移行処理と同じロジックを再利用）で突合。
4. 確信度の高いマッチ（32,248件、要確認22件は自動では触れずスキップ）について、
   実運用の`NotionIdMappingStore.upsert()`（`src/sync_engine/notion_id_mapping.py`）を
   そのまま使って新規マッピング行を作成。`_assert_no_duplicate_external_id`による重複
   検知が実際に機能し、203件は「このZoho IDは既に別のNotionページに紐づいている」と
   検知されて自動では書き込まず除外された（元の移行で本来1つにまとまるべきだった会社が
   誤って2ページに分かれている可能性があり、要手動レビュー）。

## 最終結果

32,059件処理（189件は同一ページへの重複マッチで自動スキップ）のうち、新規作成14,696件・
既存確認17,160件・重複検知でスキップ203件・エラー0件。合計31,856件のNotionページに
kintone ID・Zoho IDの対応関係を記録した。

## 要確認として残っている203件

会社名は一致したが、そのZoho IDが既に別のNotionページ（client_master）に紐づいていた
ケース。書き込みはスキップ済みで実害は無いが、元の移行で本来1ページにまとまるべきだった
会社が誤って2ページに分かれている可能性がある。まとまった件数ではないため緊急対応は
不要だが、いずれ人の目でのレビュー・統合が必要になる可能性がある。

## Why this matters

他5DBと違い、client_masterだけがこの「マージ」設計を持つため、同種の問題は他DBでは
発生しない（他DBはZoho由来データが常に新規ページを持つため、Zoho ID→Notionページの
対応はページ作成時に自然に記録される）。将来、同様のマージ的な統合ロジックを他DBに
導入する場合は、IDマッピングストアへの記録漏れが無いか同じ観点で確認すること。
