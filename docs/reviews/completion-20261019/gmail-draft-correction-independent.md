# 下書き誤登録の限定修復・独立確認（進行中）

書き手とは別にrootが検証。固定4下書き＋送信1通だけを対象とし、顧客値・対象IDは私有計画に保持する。

- 途中版の共通統合API/applicationと専用script試験：通常27成功/DB1skip、専用ローカルPostgreSQLを指定して28成功/skip0。
- 存在しない試験ファイル名を指定した初回は試験0件で終了。実在する試験集合へ訂正して上記を実行した。
- 通信断後の取消再実行、予約前の消失、計画改変、管理外関連の非空、固定5件の履歴連鎖、専用jobを通常画面から再開しない境界を確認。
- 本文ブロック/参照保持と実DB受付移管・集計修正の追加検証は進行中。実適用は未実施で、この途中結果を修復完了とは扱わない。

- 専用QAの追加実測はローカルPostgreSQL込み76成功。SQL移管/削除の実DB検証と外部模擬検証を区別。
- QA BLOCKER：4連続統合の固有な本文累積・同一親逆関連計算を通す結合試験不足。書き手へ追加を依頼し、本番適用を止めた。現実物planは4統合/本文5/親1/hash一致。
- qualityレビューはBLOCKER0、WARN1（対象外受付に残す旧配送状態の説明）。SECの子関連ロック指摘は全子鍵の保持追加で解消、最終確認中。
- 外部2レビューは検査済み105,821 bytesの資料を添付して送信済み、回答待ち。顧客値・対象ID・原文・秘密値は含まない。

- SEC最終はBLOCKER0、専用実DB込み27成功/skip0。残WARNはZoho取消とSheets更新の外部UI同時編集窓。quality最終もBLOCKER0。QA結合試験不足のBLOCKERを解消するまでは未適用。

- 追加した結合試験2ケースの独立QAを含め78成功。実LiveIO/build/run・共通4統合・SQL履歴/aliasを通し、2/3回目本文追加直後の通信断→再開→再実行で本文5/追加4回/親関連正本のみ/旧alias4/受付移管/EmailLog保持を確認。QAの試験不足BLOCKERは解除、外部transportと鍵は合成。
- 外部初回：Gemini BLOCKER0/WARN3、Claude BLOCKER1。ClaudeはSENT証拠付きの未対応活動が通常操作・Notion通知から新規作成され得る境界を指摘。固定対象の訂正中マーカーと通常経路の拒否を追加するため未適用を維持。Geminiの「旧Webhookは正本へ誘導」「競合完全排除」等の要約は実装を正確に表さないので採用しない。

## 最終UI変更後の再実行（13:08、実装担当によるQA再実行）

- Python全体：**4,877成功 / 32 skip / 失敗0**（30.05秒）。`SYNC_REVIEW_TEST_DATABASE_URL=postgresql://127.0.0.1:55439/crm_field_review_test` を指定。別ポート55443専用の `tests/hub_creation/test_entry_persistence.py` は依頼どおり除外した。skipは未実施として残し、本番動作確認とは扱わない。
- 訂正script・EmailReview・RecordMerge・送信証拠の直接関連：**293成功 / 13 skip / 失敗0**（4.62秒）。上記全体実行とは別に実施。
- 管理画面メールCard描画：**7成功**。予約中SENT/unknownの操作非表示、取消済み理由優先、解除後通常操作復帰を確認。
- `npx tsc --noEmit`：成功。
- Python警告1件は既存Starlette/httpx非推奨警告。製品コード変更・本番書込は行っていない。
- ローカルログ：`/tmp/crm-draft-final-full-qa.log`、`/tmp/crm-draft-final-related-qa.log`。

### 最終外部再判定・配備前

Claude Opus 5.5 中はBLOCKER 0、前回B1とW1〜W3解消。予約取消機能がないWARNは保持：今回は固定対象の訂正を最後まで実施し、途中で内容が変わった場合は印を外さず同じ計画からの再開可否を実読する。原本/送信証拠が欠ける状態へ戻す自動解除は追加しない。外部UIとの競合窓と実クライアントは適用時の再読で確認する。SEC B0、quality最終UI B0/W0、全体4,877成功32skip、関連293成功13skip、描画7成功、型check成功。DB索引マイグレーションは単独pending確認後deploy成功。

Gemini最終再レビューもB0/W3（索引先行、順序、外部UI競合窓）。「SENTのblocked_action_ids=[]」という説明は不正確で、実装は旧SENTページを配列に残して永久停止し、旧正本draft側だけ空配列にして正本への通常経路を許可する。レビューの言い切りは実装と照合し採用しない。

本番実画面：開きっぱなしの旧bundleでは予約reason更新後もフォームが表示された。Meta+rでは実ページの読み直しを確認できず、公開URLへgotoで再遷移後は5件すべて専用訂正中案内・通常ボタンなしを実読。API投影欠落という疑いは撤回、追加コード変更なし。private held-ui/fresh-uiを別名保存。
