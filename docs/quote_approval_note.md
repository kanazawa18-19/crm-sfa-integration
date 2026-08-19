# 見積書 承認フロー（Google Drive Approvals連携）

見積書について、Googleドライブ純正の「承認をリクエスト」機能（Drive Approvals API）を使った
社内承認フローを実装している（2026-08-18〜19）。対象は見積書のみ（申込書・契約書は非対応、
`src/document_generation/quote_generator.py`の`_CATEGORY = "見積書"`固定）。

## 認証方式: 営業担当者個人のOAuth接続

Drive Approvals APIはサービスアカウントでは`canStartApproval`が`false`（実機検証済み）のため、
承認リクエストを送る営業担当者本人が`/settings/drive`から個人のGoogleアカウントを連携する
必要がある（`RepGmailConnection`と同じ個人OAuth同意方式、`RepDriveConnection`テーブル）。
アクセストークンはリクエストのたびに`RepDriveConnection.refreshTokenEnc`（AES-256-GCM暗号化、
`TOKEN_ENCRYPTION_KEY`環境変数）を復号して取得する。

## 処理の流れ

1. `POST /api/documents/quote/request-approval`（dashboard `documents/page.tsx`から呼ばれる）
2. `request_quote_approval()`（`src/document_generation/quote_generator.py`）:
   - 承認者が`DocumentApprover`（active）に登録されているか検証
   - 同一案件・同一カテゴリで`in_progress`の承認リクエストが既にないか検証（重複防止）
   - 依頼者本人の`RepDriveConnection`からアクセストークンを取得（未接続なら`DriveNotConnectedError`）
   - テンプレートをコピーしラベル駆動でセル差し込み（`_build_quote_copy()`、一時格納フォルダ
     `QUOTE_PENDING_APPROVAL_FOLDER_ID`＝Drive実名「営業部」直下へ直接作成）
   - **セル差し込み後、同じfile_idのままPDFへ内容変換する**（`export()`→`replace_content()`
     →`rename()`、2026-08-19追加。過去の承認履歴で見積書は常にPDFとして送られていた運用実態に
     合わせた。承認対象を編集可能なGoogle Sheetsのまま送っていた初期実装は、Drive Approvals
     APIの応答仕様の食い違い調査の過程で見直した）
   - `start_approval()`でDrive純正の承認リクエストを送信（`reviewerEmails`に承認者を指定）
   - `DocumentApproval`行を作成（`notionProjectId`/`category`/`driveFileId`/`driveApprovalId`/
     `approverEmail`/`requestedByEmail`/`status="in_progress"`）
3. `GET /api/cron/document-approval-poll`（GitHub Actions、1時間おき、`DOCUMENT_APPROVAL_CRON_SECRET`
   で認証。Vercel Hobbyプランのcron制約(1日1回まで)のため`vercel.json`のcronsには登録していない）:
   - `in_progress`の`DocumentApproval`を全件`get_approval()`でポーリング
   - `APPROVED`: `move()`で送付済みフォルダ（`QUOTE_SENT_FOLDER_ID`＝Drive実名「送付済」）へ
     移動、Notion案件ページの「見積書」（FILESプロパティ）へファイルリンクを追記
   - `DECLINED`/`CANCELLED`: 一時格納フォルダに残したまま依頼者へSlack通知のみ
   - いずれの場合も`DocumentApproval.status`を更新し`notify_quote_approval_result()`で通知

## Drive Approvals APIの実機確認済みの落とし穴

公式REST referenceの記載と実際のレスポンス形状が食い違っていた点（2026-08-18〜19の実機
テストで発見、すべて`src/document_generation/google_drive_client.py`で対応済み）。

1. **`supportsAllDrives`パラメータ**: `files`系エンドポイント（copy/get/update等）専用で、
   `approvals:start`等のApprovals系エンドポイントには存在しない。付与すると
   `HTTP 400: Unknown name "supportsAllDrives"`になる。Approvals系メソッドは
   `include_shared_drive_support=False`で呼ぶ。
2. **`fields`パラメータ未指定時の部分レスポンス**: `fields`を指定しないと、Google APIが
   デフォルトで`{"kind": "drive#approval"}`のような最小限のフィールドしか返さない
   （`approvalId`や`status`が欠落する）。`start_approval`/`get_approval`/`list_approvals`
   いずれも`fields=*`を明示的に付与する。
3. **`approvals:start`のレスポンスに`approvalId`が無いことがある**（`fields=*`指定後も、
   結果整合性(eventual consistency)により直後は空のことがある）: `list_approvals()`
   （`GET /files/{fileId}/approvals`）で`IN_PROGRESS`状態の承認を探すフォールバックを行う
   （最大4回・1.5秒間隔でリトライ）。

## Drive上の固定フォルダ

- 一時格納（Drive実名「営業部」）: `1-JEnDVJQPY677vqIObtTLeiFt437jPGa`
- 送付済み（Drive実名「送付済」）: `1HZDKCBD1JLq1g9MEg9alU0azAjPKdSzl`

## 既知の制約

- 担当者印影欄はテンプレート上フローティング画像として配置されているため、自動生成では
  差し替えできない（`_SEAL_NOTE`、テンプレートの雛形のまま出力される。送付前に手動確認・
  差し替えが必要）。
- 見積書NO（`_generate_quote_number()`）は現時点で`CN{YYYYMMDD}{案件IDの先頭4文字}`という
  簡略化した独自ルール。正式な採番規則（`CN{YYYYMMDD}{作成者の頭文字1文字}{その日の発行
  連番2桁}`、例: `CN20260819K01`）への対応は未実装（2026-08-19、金沢さんから正式ルールの
  共有あり、着手待ち）。
