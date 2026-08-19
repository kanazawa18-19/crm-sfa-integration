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

## 見積書NOの正式採番（2026-08-19）

`_generate_quote_number()`は正式な採番規則で採番する: `CN{YYYYMMDD}{作成者頭文字1字}
{当日発行連番2桁}`（例: `CN20260819K01`）。「作成者頭文字」は下記の手動入力欄「作成者」
（未入力ならNotion案件データの担当メンバー）の先頭1文字を大文字化したもの。「当日発行連番」
は`quote_number_db.next_sequence_for_date()`が日付ごとに`QuoteNumberSequence`テーブルへ
`INSERT ... ON CONFLICT DO UPDATE ... RETURNING`で原子的にインクリメントして払い出すため、
同時に複数の見積書が生成されても連番が重複しない。

- **ローマ字変換はしない**（obasan-qualityレビューBLOCKER対応の一部）: 「作成者頭文字」は
  入力文字列の先頭1文字をそのまま大文字化するだけで、日本語名を渡すと漢字1文字になる
  （例:「金沢」→「金」）。この初期値問題を避けるため、ダッシュボード側の「作成者」欄の
  デフォルト値はログイン中ユーザーの表示名(`User.name`)ではなく、社内のメールアドレスが
  ローマ字姓の慣例であることを利用してメールのローカル部を大文字化した値
  （例: `kanazawa@cnctor.jp` → `Kanazawa`）を使う（`documents/page.tsx`の
  `buildCreatorNameDefault()`）。ただし手動入力欄で日本語名に書き換えることは可能なため、
  完全な防止にはなっていない（UI側に半角英字推奨の案内文言を表示している）。
- Notion案件データにも手動入力欄にも作成者名が無い場合は先頭文字が`"X"`になる。この場合は
  `DocumentResult.notes`（送付前確認欄）に理由を明示する。kintone/Zoho移行案件は担当メンバー
  が軒並み未設定なことが多く、この"X"採番は珍しくない（`project_crm_sfa_unresolved_assignee`
  参照）。
- 「作成者」欄は見積書NOの頭文字だけでなく、見積書シート上の「担当」表示セルにもそのまま
  書き込まれる。代理作成者が自分の名前を入力すると、顧客向け見積書の「担当」欄にも代理作成者
  名が表示される点に注意。

## 手動入力欄（2026-08-19）

書類作成画面（`documents/page.tsx`）に、見積書生成時のみ表示される「詳細情報（任意）」
セクションを追加した。備考・初期費用・月額費用・クライアント名・商材名・作成者の6項目。
未入力の項目はNotion案件データの値をそのまま使う（`QuoteOverrides`/`_resolve()`、
`src/document_generation/quote_generator.py`参照）。商材名・初期費用・月額費用はNotion
案件データ側に対応項目が無いため、この手動入力欄からのみ差し込まれる。

- 空文字列・空白のみの入力は「未入力（Notion側の値にフォールバック）」として扱われる。
  Notion側の値を意図的に空欄にして送りたい場合の手段は現状無い（備考・クライアント名・
  作成者は上書き専用で、明示的な「クリア」はできない）。
- 手動入力欄の値はGoogle Sheets APIへ`valueInputOption=USER_ENTERED`（セルに人間が入力
  したのと同じ扱い）で書き込むため、`=`/`+`/`-`/`@`で始まる文字列は数式として評価されうる
  （formula injection）。`_sanitize_sheet_cell_value()`が該当する場合に先頭へ`'`を付けて
  テキストとして強制する（shirokuma-secレビューWARN対応）。

## 未解決の論点（要 金沢さん判断）

- **「書類を生成」（プレビューダウンロード）ボタンも、承認リクエストと同じ正式な当日発行
  連番を消費する**（shirokuma-secレビューWARN）。詳細情報欄を編集しながら内容確認のために
  何度も「書類を生成」を押すと、実際には送付されない番号（K01, K02, ...）が歯抜けで
  発生しうる。連番に欠番があってはいけない運用（会計・監査要件）であれば、「承認リクエスト
  送信時のみ正式番号を払い出す」方式への変更が必要。現時点では変更しておらず、生成のたびに
  番号を消費する挙動のまま。

## 既知の制約

- 担当者印影欄はテンプレート上フローティング画像として配置されているため、自動生成では
  差し替えできない（`_SEAL_NOTE`、テンプレートの雛形のまま出力される。送付前に手動確認・
  差し替えが必要）。
