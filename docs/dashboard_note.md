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

## クオーター着地予測のMax/Min判定基準変更（2026-08-14）

`src/analytics/forecast.py`のMax（楽観）/Min（悲観）シナリオの判定基準を、別プロパティ
「確度」（A〜D、RequirementLevel.OPTIONAL）から、必須入力の「営業ステータス」の値
（Aヨミ・Bヨミ・口頭受注・トライアル等）ベースに変更した。仕様書v2.0（06節）は旧仕様
（S・Aランクベース、実データに存在しないSランクを含む）のまま。詳細な経緯は
`src/analytics/forecast.py`のモジュールdocstringを参照。

- 変更前: ダッシュボードのMin/Expected/Maxが常に同じ数字になる不具合があった。原因は
  「確度」プロパティの実データ入力率が極端に低く（進行中案件の大半で未入力）、
  Max/Minがほぼ機能していなかったため。
- 変更後: Max＝営業ステータスが「Aヨミ」または「Bヨミ」の未契約案件を全額計上。
  Min＝営業ステータスが「Aヨミ」「口頭受注」「トライアル」の未契約案件を確度を
  問わず全額計上。Expectedは変更なし（引き続き「確度」による加重平均）。
- 旧実装にあった「Max≧Expected≧Minを常に保証するキャップ処理」も撤廃した（金沢さん
  判断）。そのため**Minの方がMaxより大きく表示される等、直感に反する場合がある**
  （ダッシュボードAPIの`notes`にもその旨の注記を追加済み、`src/api/dashboard_service.py`）。

## マネージャー通知（2026-08-12追加、`/alerts`）

案件管理DBを「失注」「失注候補」「停滞案件」「契約成立」の4区分に分けて一覧するAPI・画面。

- **失注候補は代理指標**: 実データに「失注候補」を直接表す営業ステータス値が存在しないため、
  確度D かつ進行中ステータスの組み合わせを代理指標として抽出している。フロントエンドでは
  該当行に「代理指標」バッジを表示し、実際のステータス変更ではない旨を明示している
  （`dashboard/app/(dashboard)/alerts/page.tsx`）。
- **停滞判定**: 次回アクション予定日を基準にした独自の閾値判定（日数は設定ファイルで
  調整可能）。基準日数はレスポンスの`stalled_days_threshold`で画面にも表示される。
- 基準日（`as_of`）を指定してその時点のスナップショットを見られる点は`/reports`（日報）と
  同じ設計。

## 必要な環境変数

| 環境変数 | 説明 |
|---|---|
| `DASHBOARD_API_TOKEN` | ダッシュボードAPIの簡易認証トークン。`Authorization: Bearer <token>`ヘッダーと比較する。未設定時はデフォルトで全リクエスト401（fail-closed）。 |
| `ALLOW_UNAUTHENTICATED_DASHBOARD_API` | `"true"`（大文字小文字無視）を明示的に設定した場合のみ、`DASHBOARD_API_TOKEN`未設定時でも認証をスキップして通す（ローカル開発用の明示的なオプトイン）。 |
| `DASHBOARD_FRONTEND_ORIGIN` | CORSで許可するオリジン（カンマ区切りで複数可）。未設定時はCORSを一切許可しない（fail-closed）。 |

上記に加え、既存の`NOTION_API_KEY`（Notion API呼び出し用）が必要。
