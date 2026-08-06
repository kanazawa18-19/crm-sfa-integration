# ダッシュボード（管理画面）バックエンドAPI 補足ノート

営業管理者向け管理画面（ダッシュボード・日報・メンバー別パフォーマンス）のバックエンド
（`src/api/`配下）に関する既知の制約・注意事項をまとめる。フロントエンド
（`dashboard/`、Next.js）は別エージェントが並行実装しているため、ここでは触れない。

## action_typeはヒューリスティック推定である

アクション履歴DB（`src/db_schema/action.py`）のtitleプロパティ
「商談回数・電話回数・メール回数（何回目）」は表記ゆれの激しい自由記述であり
（【電話】N回目、【商談】N回目、テレアポ↓（担当者名）等）、正規化された
`action_type`（テレアポ／訪問商談／オンライン商談／メール／その他）を直接は持たない。

`src/api/action_classifier.py`の`classify_action_type`はキーワードマッチングによる
ヒューリスティックな推定であり、正確なアクション種別分類ではない。誤分類（例:
「訪問予定を電話で確認」のようなタイトルがテレアポと判定される等）が起こりうる。

kintone側（`src/migration/action_mapping.py`関連の移行データ）には既に正規化された
5種のアクション種別（テレアポ／訪問商談／オンライン商談／メール／自動メール）が存在する。
将来的にはこの正規化済みデータをNotion側アクション履歴DBへ統合し、`classify_action_type`
によるヒューリスティック推定を廃止する移行が必要（現状は保留タスク）。

## 「本日ステータス変更のあった案件」は常に空になる

案件管理DB（`src/db_schema/project.py`）にはステータス変更履歴を保持するプロパティが
存在せず、現状のスナップショットのみからは「本日ステータスが変更された案件」を算出
できない。そのため`build_daily_report`（`src/api/dashboard_service.py`）が返す
`status_changes`は常に空配列になる（`src/reports/daily_report.py`の
`previous_status`/`status_changed_date`を常にNoneのまま渡しているため）。

正確な変更検知には、ステータス変更の永続的な変更ログ（例: 同期エンジンの差分検知結果を
別テーブルへ記録する等）の実装が必要（将来課題）。

## ユーザー名解決に必要なNotion Integration権限

`src/api/user_directory.py`の`NotionUserDirectory`は`GET /v1/users`でワークスペースの
ユーザー一覧を取得し、案件管理DBの`担当メンバー`（user型）・アクション履歴DBの
`担当営業`（rollup、実データ次第でpeopleかtextか変わる）に含まれるユーザーIDを表示名へ
解決する。この呼び出しには`NOTION_API_KEY`のIntegrationに「ユーザー情報の読み取り」権限
（Notion Integration設定の "Read user information including email addresses" 相当）が
必要であり、この権限が無効なままだと`GET /v1/users`がエラーになる可能性がある。

## 必要な環境変数

| 環境変数 | 説明 |
|---|---|
| `DASHBOARD_API_TOKEN` | ダッシュボードAPIの簡易認証トークン。`Authorization: Bearer <token>`ヘッダーと比較する。未設定時はデフォルトで全リクエスト401（fail-closed）。 |
| `ALLOW_UNAUTHENTICATED_DASHBOARD_API` | `"true"`（大文字小文字無視）を明示的に設定した場合のみ、`DASHBOARD_API_TOKEN`未設定時でも認証をスキップして通す（ローカル開発用の明示的なオプトイン）。 |
| `DASHBOARD_FRONTEND_ORIGIN` | CORSで許可するオリジン（カンマ区切りで複数可）。未設定時はCORSを一切許可しない（fail-closed）。 |

上記に加え、既存の`NOTION_API_KEY`（Notion API呼び出し用）が必要。
