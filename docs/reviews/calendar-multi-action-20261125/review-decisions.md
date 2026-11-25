# 複数アクションのレビュー採否（最終）

## 独立内部レビュー

- SEC BLOCKER: TODOロック解除後のCalendar更新で新旧逆転を実再現。タスク単位のロックをTODOから予定応答まで拡張し、結果不明送信を永続保留する修正済み。
- QA BLOCKER: 実PostgreSQLの並行予約で別unique index競合が漏れる。UniqueViolationを予約失敗として扱う修正済み。
- 品質WARN: プレビューと既定OFFの差、保留の英語コードを修正。モード・必要flagを表示し日本語理由と案件URLを追加。

## Claude

- B1の同期多数処理: 時間予算と未着手保留を追加。自己誘発について、Webhookは対象5項目の変更判定を持つ。逆リレーション変更だけでは通常対象外。変更項目情報なしの場合は再処理される。
- B1のobjectロック疑義: record_sync_lock.py162以降はSQLite以外なら直接DB接続とpg_try_advisory_lockを使う。渡したobjectの同一性に依存しないため誤検知。
- B2の既知ID消失: 採用。finish失敗時のIDヒントを保留行externalIdへ保存し、後続理由更新で消さない。
- B2の確定4xx/429: 保守的に自動再POSTしない。手動照合待ちになる運用制約を記載。検索0件だけを根拠に再作成する提案は採用しない。
- B3旧予定移行: 仕様どおり保留。切替前の旧経路停止・完了待ちと移行確認を文書化。本番有効化前の条件。
- B3旧ID形式疑義: 旧plan.pyもstr(UUID(...))であり新旧同じ正規形式。
- W3再保留疑義: dismiss_holdはblocked行をDELETEするため、新しい保留をINSERTできる。state変更方式ではなく誤検知。
- W4実DB整合性: 独立ローカルPostgreSQLで検証。実運用DB利用上限は本番化前の残件。
- W5/W6: 採用。明示箇条書きは1件でも本文日付、日付隣接曜日は識別から除外。
- W1/2/7/8: Notion正規化差・結果不明保留・Calendar人編集上書き・再通知が必要な範囲を文書化。自動削除/自動予約解除/定期再送は追加しない。

## Gemini

- B1担当者キャッシュ: 現仕様は全タスクが同じ案件担当者。将来条件の指摘のため現実装のBLOCKERではない。修正後サービスはタスクごとの解決になった。
- B2既存getキー/NOT NULL疑義: SQLは実列名を返す。独立実DB試験で確認する。
- B3日付空配列疑義: select_action_dateはlist対応。日付なし3タスク→3TODO/2予定の独立模擬試験で成立。
- W2複数候補日: 本人の最寄り日指示に合わせ今日以降最短/全過去最新を維持し文書化。
- W3人編集保護、W4旧予定移行、W5削除済固定ID: 意図した保留・非復元の仕様。
- INFOの日付変更で別タスクとの記載は要件誤読。日付変更では同じタスクIDを維持する。

## 最終確認

- SEC追加BLOCKER（取得済み古い案件原本で両方巻戻り）を修正。原本last_edited_timeと入力hashを案件ロック内・外部書込前に永続受理し、古い版skip/同時刻別内容hold/同時刻同内容再開。batch_advanceで受理版を保持。独立再現で新12/02→旧11/26でも新値維持、Calendar呼出1回。
- 最終独立QA 566成功・skip0（ローカル558＋PostgreSQL8）。別途metadata rollback/再開2ケース確認。MA46成功・tsc成功。実Notion/Google書込は未検証。
- Claude追補は再現する未解消BLOCKERなし。予算条件は既存CRM検証配備をCLI inspectしlambda.timeout=300、project fluid=trueを確認。今回hook HTTP予算120秒。前後の既存処理を含む総時間の実測は本番化前に必要。API toolのscope403後、CLIの既存認証を使い明示teamで読取。設定変更なし。公式仕様: https://vercel.com/docs/functions/configuring-functions/duration
- Gemini追補B1（[x]境界）は誤検知。optionalチェックボックス部が一致しなくてもbullet接頭辞が境界になり、実行結果は独立2項目、未完了1件のみ採用。モデル提案で[x]自体を消すと完了判定を失うため不採用。
- Gemini追補B2（結果不明を通過して次へ）は意図した仕様。結果不明は永続calendar_task_eventのreserved＋保留一覧で管理し、再通知で自動再送せず手動照合を待つ。他タスクの処理を妨げない。
- 最終の原本版修正は独立内部SEC/QAで再確認。外部追補添付はその直前のスナップショットであり、外部モデルが最終版全行を確認したとは扱わない。
