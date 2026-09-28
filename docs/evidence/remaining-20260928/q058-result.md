# Q058 限定登録試験（2026-09-28）

4経路の実API登録、再通知時の重複防止、Notion/外部/シートの実読戻し、今回発行IDだけの片付けを確認した。
HTTP Webhook入口からの通し試験ではなく、配備コードの登録サービス・通常同期を実API接続で実行した。GASメニューの実クリックは未検証。

|経路|外部|作成・再通知|シート|片付け|
|---|---|---|---|---|
|n-chain|Zoho|実読戻し一致・重複なし|実読戻し一致|外部削除・Notion/対応表archive・行値clear確認|
|s-chain|Zoho|実読戻し一致・重複なし|実読戻し一致|外部削除・Notion/対応表archive・行値clear確認|
|n-client|kintone|実読戻し一致・重複なし|実読戻し一致|外部削除・Notion/対応表archive・行値clear確認|
|s-client|kintone|実読戻し一致・重複なし|実読戻し一致|外部削除・Notion/対応表archive・行値clear確認|

- 使用枠：Zoho 2/2、kintone 2/2、Notion 4/4、シート 4/4。追加登録しない。
- シート起点の行識別metadataも今回のIDだけ削除。登録予約の成功証跡は保持。
- kintone通知はログイン済み管理画面で追加/編集通知なし、レコード条件/リマインダーなしを確認。ZohoチェーンはCustomModule3のworkflow一覧0を確認。
- シート施設数0が非書式値では数値になる不具合を62ac007で修正。既存行の登録番号で再開し、追加の試験行は作っていない。71テスト成功、独立sec B0/W0/I0、CI36372269326成功、本番Ready。
- 試験対象はチェーン/取引先だけ。商品/案件の試験書込への承認拡張なし。
- 個別IDと読戻し証跡はgit対象外の migration_output/remaining-20260928/q058-*-issued.json に保存。
