# Gmail送信証拠の独立検証

実装担当gmail_checks、SEC recovery_design、quality slack_checks、独立QA rootを分離した。

- 実物：固定4受付はGmail履歴でDRAFT追加→削除、最終1受付だけSENT追加と照合した。実データのID/本文は私有証跡に分離。
- SEC：BLOCKERなし。状態の純粋判定、共通入口、通常Notion通知、旧耐久キューの原文保持、送信証拠補完を確認。
- quality：初回WARN（状態不明でも手動操作を案内）をSlack/管理画面の再取得案内と操作非表示で解消。最終BLOCKER 0 / WARN 0。
- root通常回帰：766成功/140skip。追加後、ローカルPostgreSQLを含むgmail_sync/email_review/hub_creation（別専用portを要求するentry_persistenceを除く）は764成功/13skip。初回の旧late_registration試験データ5件のlabel欠損は実応答相当のfixtureへ訂正後解消。
- root GAS：14成功。書き手の管理画面静的描画3成功・型検査成功は独立のブラウザ実物確認とは区別する。
- 初回の実DB一括指定はentry_persistenceの専用55443要件に55439を指定したため38件がsetup拒否。製品の障害ではなく、隔離DBの安全制約による拒否。今回変更に関連する実SQLは55439の使い捨てschemaで確認した。

## 他社レビュー

Claude Opus 5.5・中の初回B1（実取得のラベル空と旧保存の欠損の混同）を採用。実API応答省略は空配列、旧保存/旧GASの欠損は不明を維持。SENTのラベル追加イベントも取り込み、既存action_id索引による照会へ変更。固定8件の送信証拠は配備前に補完し、完了状態を保持して確認する。

- Claude: https://claude.ai/chat/126084f3-9a20-417d-9f3a-b219d2c90683
- Gemini: https://gemini.google.com/app/40e0f68bbc6b668d?hl=ja
- Gemini初回は送信直後の会話表示を確認したが、その後の再読では依頼と回答が会話から見つからず、回答取得済みとは扱わない。修正後の別資料で最終依頼中。

過去の誤活動/EmailLogは別の限定訂正工程であり、このコード配備だけで修復完了にはしない。

## 最終回答と採否

Claude・Geminiとも最終BLOCKER 0。Claudeの通し試験WARNは追加検証中。rootの管理画面描画/APIルート試験は7成功。Geminiの配備順の記述は前段と矛盾するため採用せず、正常8件の証拠補完→API配備→GAS更新を正とする。旧GASの応答はHTTP自体の成功/失敗と、各メッセージのeffect_failedを区別して確認する。配備窓の観測結果も記録する。

- 通し試験：実API形式の取得→record_message→capture→ローカル実DBの受付/EmailLogまでを追加。root独立でdeferred全14ケース成功。metadata取得でもlabelを保持する条件を確認。
- 固定8件の送信証拠補完は私有計画・全行ロック・監査・別接続の全JSON一致で完了。rootも証跡8件を独立照合し、label/revision以外の全項目不変を確認。
- 全体試験初回は4,699成功/195skip/15失敗。うち3件はsandboxの子プロセス制約で、隔離ローカルの適切な権限で再実行し3成功。残12件は旧fixtureのlabel不足/メール受付照会mock不足を修正中。全体最終結果は後続記録で確定する。

- 最終全体回帰：root独立で **4,714成功・196skip・失敗0**。全件照会では活動あり12件中、送信証拠あり8・証拠なしは今回のDRAFT4だけ。保留漏れなし。
