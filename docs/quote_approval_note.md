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
   - 承認者（`approver_emails`、複数選択可）が空でないこと、全員が`DocumentApprover`
     （active）に登録されているかを検証（未登録が1件でもあれば送信しない）
   - 同一案件・同一カテゴリで`in_progress`の承認リクエストが既にないか検証（重複防止）
   - 依頼者本人の`RepDriveConnection`からアクセストークンを取得（未接続なら`DriveNotConnectedError`）
   - テンプレートをコピーしラベル駆動でセル差し込み（`_build_quote_copy()`、一時格納フォルダ
     `QUOTE_PENDING_APPROVAL_FOLDER_ID`＝Drive実名「営業部」直下へ直接作成）
   - **セル差し込み後、同じfile_idのままPDFへ内容変換する**（`export()`→`replace_content()`
     →`rename()`、2026-08-19追加。過去の承認履歴で見積書は常にPDFとして送られていた運用実態に
     合わせた。承認対象を編集可能なGoogle Sheetsのまま送っていた初期実装は、Drive Approvals
     APIの応答仕様の食い違い調査の過程で見直した）
   - `start_approval()`でDrive純正の承認リクエストを送信（`reviewerEmails`に承認者全員を指定。
     1件のリクエストに複数reviewerを持たせる形で、承認者ごとに別々のリクエストは作らない）
   - `DocumentApproval`行を作成（`notionProjectId`/`category`/`driveFileId`/`driveApprovalId`/
     `approverEmails`/`requestedByEmail`/`status="in_progress"`）
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

## 複数承認者対応（2026-08-27）

承認者を1人ではなく複数選択して同時に承認リクエストを送信できるようにした。

### Drive Approvals APIのセマンティクス

`reviewerEmails`に複数指定した場合の挙動（公式ガイド
https://developers.google.com/workspace/drive/api/guides/approvals で確認済み）:

- **全員が承認して初めて`APPROVED`になる**。1人でも却下すれば全体が`DECLINED`になる。
- ファイルが編集されると全員の承認状態がリセットされ、再承認が必要になる。

「1回の承認リクエスト＝Drive上の1つのapproval」という既存の構造は変えていない。複数承認者は
1つのapprovalに対する複数`reviewerEmails`として表現され、承認者ごとに別々のapprovalを
作るわけではない。したがって`approval_poll.py`側はDrive側の集約statusをそのまま見ればよく、
ポーリングのロジック自体に変更は不要だった（変わったのは送信時の`reviewerEmails`と、
DB・通知文面の複数承認者表現のみ）。

### Slack通知の可読性・却下者特定（obasan-qualityレビューWARN対応）

`approval_notify.notify_quote_approval_result()`の承認者一覧は、人数が増えても読みやすい
よう`、`区切りの1行ではなく箇条書き（改行＋`・`）にした。

さらに、複数承認者になったことで「5人中の誰が却下したのか」が依頼者に分からなくなる問題へ
対応した。Drive Approvals APIのApprovalリソースには`reviewerResponses[]`（各要素が
`reviewer`と`response`: `NO_RESPONSE`/`APPROVED`/`DECLINED`）が公式ドキュメント上存在し、
`get_approval()`は既に`fields=*`を指定しているため取得できる可能性が高い。却下時の通知には、
`reviewerResponses`から`DECLINED`を返した人を特定できればその人を明示する。

**この`reviewerResponses`が実レスポンスで返ってくることは2026-08-27時点で未検証**（公式
リファレンス上の存在確認のみ）。`_extract_declined_reviewers()`（`approval_notify.py`）は
フィールドが無い・要素の形が想定と違う場合も例外を投げず空リストを返し、呼び出し元（
`notify_quote_approval_result()`）は従来どおり承認者全員の列挙にフォールバックする
（`tests/document_generation/test_approval_notify.py`のフォールバック系テストで担保）。
実機で形が確認でき次第、このコメントとフォールバック要否を見直すこと。

### スキーマ変更（expand方式）

`DocumentApproval`テーブルに`approverEmails`（`String[]`、正）を追加し、旧`approverEmail`
（単一、`String`）は削除せずnullableへ変更した（`dashboard/prisma/migrations/
20260827000000_document_approval_multi_approver/`）。

expand方式（追加のみ）を採った理由: `dashboard/package.json`のbuildスクリプトは
`prisma generate && prisma migrate deploy && next build`で、マイグレーションはNext.jsの
ビルド時に自動適用される。このため新デプロイが公開される前に、本番DBのスキーマだけが先に
変わる瞬間がある。その数十秒の間は旧コード（Vercel上でまだ旧バージョンとして動いている
関数）が動いており、もし同じマイグレーションで旧`approverEmail`列をDROPしていたら、その窓で
承認リクエスト送信・承認状態ポーリングcronの両方が500エラーになる。このプロジェクトは
過去に同期基盤（ProjectMirror等）で無停止でない変更により実害のあるデータ消失事故を複数回
起こしているため、今回は無停止で進められる形を優先した。

Python側の`insert_document_approval()`は、正である`approverEmails`に加えて旧`approverEmail`
にも先頭1件をdual-writeする。これは「ロールバックして旧コードに戻った場合に、旧コードが
Slack通知文面で`approver_email`をNoneとして出してしまう」事態を避けるための経過措置であり、
読み取りは常に`approverEmails`のみを使う（旧`approverEmail`は書き込み専用の互換カラム）。

#### `approverEmails`にまだNOT NULL制約を付けていない理由（shirokuma-secレビューBLOCKER対応）

`migration.sql`は当初、バックフィル後に`ALTER TABLE ... ALTER COLUMN "approverEmails" SET
NOT NULL`を実行していたが、これはこのPRの設計意図そのもの（デプロイ窓の間、旧コードが
動き続ける）と矛盾していた。旧`insert_document_approval()`のINSERT文は列を明示指定しており
`approverEmails`には一切触れないため、デプロイ窓でNOT NULL制約を付けてしまうと、その窓で
送信された承認リクエストが`null value in column "approverEmails" violates not-null
constraint`で失敗し、承認リクエスト送信が500を返す（旧カラムをDROPして避けようとした事故を、
ADD側のNOT NULL化で作ってしまっていた）。

そのため`approverEmails`は現時点でnullableのまま残している。NOT NULL化は、新コードが本番で
安定稼働してから（目安: 事故なく数営業日稼働）、別マイグレーションで行う（「旧`approverEmail`
列を削除できる条件」と同じタイミング判断）。

なおPrismaのスカラーリスト（`String[]`）はスキーマ定義言語上optional（`String[]?`）にできない
仕様のため、`schema.prisma`側は`approverEmails String[]`のまま（nullableをスキーマ上表現
できない）。Prisma ClientはPostgresのNULL配列を読み取り時に空配列`[]`として返すため、DB側が
nullableでもJS側の型と実害は一致する。

#### デプロイ窓で作られた行への読み取りフォールバック（shirokuma-secレビューBLOCKER対応）

上記のデプロイ窓で旧コードがINSERTした行は、`approverEmails`がNULL（Python側は`psycopg`で
直接読むためNone）のまま旧`approverEmail`（単一）のみ埋まった状態になる。これを
`approverEmails`だけで読むと「承認者0人」と誤認し、Slack通知の承認者欄が空になってしまう。

`src/document_generation/approval_db.py`の`_row_to_approval()`は、`approverEmails`が
NULL/空でかつ`approverEmail`が非NULLの場合、`[approverEmail]`（1要素配列）として読む
フォールバックを持つ（`tests/document_generation/test_approval_db.py`で担保）。これは
`insert_document_approval()`のdual-writeと対になる経過措置であり、旧`approverEmail`列を
削除する別マイグレーションの際に、dual-writeと一緒に削除する。

### 旧`approverEmail`列を削除できる条件

以下がすべて満たされたら、別マイグレーションで`approverEmail`列自体をDROPしてよい:

- 今回のデプロイが本番に反映され、ロールバックの可能性が実質的になくなったこと
  （目安として、デプロイ後の承認フローが最低数営業日分、事故なく稼働していること）
- `insert_document_approval()`のdual-write処理・関連コメントも合わせて削除すること

### フロント・APIの互換性

- ダッシュボードUIは単一`<select>`から、承認者ごとのチェックボックス一覧に変更した
  （`DocumentsPageClient.tsx`。`<select multiple>`はCtrl/Cmd＋クリックが必要で業務ユーザーに
  分かりにくいため採用しなかった）。
- `POST /api/documents/quote/request-approval`（dashboard側プロキシルート）は、新形式の
  `approver_emails`（文字列配列）を正とするが、デプロイ直後に古いJSバンドルを掴んだままの
  ブラウザから送信された場合に備え、旧形式の`approver_email`（文字列単数）も1要素配列として
  受理する（`route.ts`）。バックエンド(FastAPI)側の`QuoteApprovalRequest.approver_emails`は
  配列必須で、この互換変換はdashboard側プロキシルートでのみ行う。

### 承認者選択UIの注意喚起・送信先確認（obasan-qualityレビューBLOCKER/WARN対応）

複数承認者を選べるようになったことで、「とりあえず選んで永久に承認が揃わない」業務事故が
起こりうる。`DocumentsPageClient.tsx`の承認者チェックボックス一覧の直前に、Drive Approvals
APIのセマンティクス（全員承認で完了・1人でも却下すれば全体却下・編集で全員の承認がリセット）
を業務ユーザーの言葉で示す注記を出している（BLOCKER対応）。

また、送信ボタンの直前には選択中の承認者の氏名・件数を「送信先: 平本さん、黒井さん（2名）」
のように表示する（`selectedApprovers`）。`window.confirm()`のようなモーダルは使わず、
このテキスト表示のみで完結させている（このアプリの既存UIにモーダルの前例が無く、操作を
止めるだけで情報量が増えないため）。0人選択時は同じ表示欄に「承認者を1人以上選択して
ください」を出し、送信ボタンの`disabled`（従来から存在）と合わせて防止する（WARN対応）。

### 未登録承認者エラーメッセージ（obasan-qualityレビューWARN対応）

`InvalidApproverEmailError`（`quote_generator.py`）が実際に起きるのは「画面を開いたまま
管理者が承認者を無効化した」ケースが主で、メールアドレスの列挙だけでは営業担当者が次に
何をすればいいか分からない。メッセージへ「ページを再読み込みして承認者を選び直してください」
を追記した。氏名への変換は行っていない——Python側は`DocumentApprover`の氏名を持たない設計
（承認リクエスト送信時にPython側へ渡されるのは選択済みの`approver_emails`のみ）のため、
氏名解決のためだけにDBアクセスを増やすことは今回のスコープでは見送った。

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

## 決着済みの論点

- **「書類を生成」（プレビューダウンロード）ボタンも、承認リクエストと同じ正式な当日発行
  連番を消費する**（shirokuma-secレビューWARN）。詳細情報欄を編集しながら内容確認のために
  何度も「書類を生成」を押すと、実際には送付されない番号（K01, K02, ...）が歯抜けで
  発生しうる。
  → **2026-08-28、金沢さんの判断で「欠番は問題なし」と決着した。** 「承認リクエスト送信時のみ
  正式番号を払い出す」方式への変更は**行わない**。生成のたびに番号を消費する現在の挙動のままとする
  （この論点を再び持ち出さないこと）。

## 既知の制約

- 担当者印影欄はテンプレート上フローティング画像として配置されているため、自動生成では
  差し替えできない（`_SEAL_NOTE`、テンプレートの雛形のまま出力される。送付前に手動確認・
  差し替えが必要）。
- `is_active_document_approver()`（`approver_db.py`）は承認者ごとに個別にDB接続を張って
  検証しており、N+1気味の実装になっている（2026-08-27、shirokuma-secレビューINFOに加え、
  外部モデルレビュー(Gemini)でも同様の指摘あり）。承認者は業務上数名〜十数名程度で実害が
  無いため今回は見送った。将来、承認者数が増える等で問題になった場合は
  `WHERE email = ANY(...)`で一括クエリにまとめる余地がある。

## 外部モデルレビュー(Gemini)で出たが今回は見送った指摘（2026-08-27）

### 重複送信チェックのTOCTOU（2026-08-28対応済み）

`find_in_progress_approval()`による重複チェックと、その後のテンプレートコピー・PDF変換・
`start_approval()`送信・`DocumentApproval` INSERTの一連の処理はアトミックではなかった。
ボタン連打や別ウィンドウからのほぼ同時送信で、チェックをすり抜けて同一案件・同一カテゴリの
承認リクエストが二重生成され得た。**複数承認者対応で新たに入った問題ではなく、単一承認者の
頃から存在した元々の挙動**（2026-08-27時点ではデプロイに含めず見送っていたが、2026-08-28に
対応した）。

**部分ユニークインデックスを選ばなかった理由**: `("notionProjectId", category) WHERE
status = 'in_progress'`の部分ユニークインデックス追加が筋の良い対処ではあるが、既存データに
重複した`in_progress`行が1組でもあると`CREATE UNIQUE INDEX`自体が失敗する。このリポジトリの
マイグレーションは`dashboard/package.json`のbuildスクリプト（`prisma generate && prisma
migrate deploy && next build`）でVercelのビルド時に走るため、インデックス作成が失敗すると
**ビルドごと落ちてデプロイが止まる**。本番DBの現状（重複行の有無）を事前確認する手段が無い
状態でこの方式を採用するのはリスクが高いと判断し、見送った。

**採った対処**: `src/project_mirror/db.py`の`try_acquire_refresh_lock()`/
`release_refresh_lock()`（同期処理の多重実行防止で実績のあるPostgresアドバイザリロック）と
同じ作法に揃え、`request_quote_approval()`の「重複チェック→送信→INSERT」区間を
`(notion_page_id, category)`をキーにしたロックで直列化した
（`approval_db.try_acquire_approval_lock()`/`release_approval_lock()`）。
`project_mirror`/`relation_sync`の既存ロックは固定キー1個の`pg_try_advisory_lock(bigint)`
だが、今回は案件・カテゴリごとに異なるキーで取り合う必要があるため、名前空間(int4定数)＋
`hashtext(f"{notion_project_id}:{category}")`（Python側で`:`区切りの文字列に組み立ててから
渡す）を使う`pg_try_advisory_lock(int, int)`版にした。

ロックが取得できなかった場合（＝同じ案件・カテゴリで既に別のリクエストが処理中）は、新しい
例外型を増やさず既存の`DuplicateApprovalRequestError`を送出する（`src/api/app.py`が422へ
変換する既存経路をそのまま使えるため）。非ブロッキングの`pg_try_advisory_lock`を使っており、
待たせて後で通すのではなく即座に失敗させる（Drive APIは一切呼ばない）。

**ロックをDrive API呼び出しを跨いで保持するトレードオフ**: このロックはテンプレートコピー・
セル差し込み・PDF変換・`start_approval()`・`insert_document_approval()`という一連の処理を
跨いで保持されるため、数秒〜十数秒にわたって保持される可能性がある。承認リクエスト送信は
営業担当者が画面のボタンを押す低頻度の操作であり、同じ案件へ同時に2人以上が送信を試みる
頻度は低いと判断し、このロック保持時間の長さは許容できるトレードオフとして受け入れている。

例外が飛んだ場合も`try/finally`で必ずロックを解放する（`tests/document_generation/
test_quote_generator.py`の`test_request_quote_approval_deletes_copy_when_start_approval_
fails`等で担保）。

### 前提条件: `DATABASE_URL`は非pooled（direct）接続であること（QA指摘、2026-08-28）

**この設計は`DATABASE_URL`が非pooled（direct）接続であることが前提。pooled接続（pgbouncerの
transaction pooling等）だとadvisory lockは静かに機能しなくなり、二重送信対策が無言で
無効化される。**

Postgresのアドバイザリロック（`pg_try_advisory_lock`/`pg_advisory_unlock`）はセッション単位の
状態であり、ロックを取得したセッション（コネクション）自身が明示的に解放するか、切断される
まで保持される。`approval_db.py`/`project_mirror/db.py`/`relation_sync/db.py`のロックは
いずれも「ロック取得に使った`Connection`をそのまま呼び出し元へ返し、処理完了後にその同じ
`Connection`で`pg_advisory_unlock`を呼ぶ」設計（`try_acquire_approval_lock()`/
`release_approval_lock()`等）のため、取得から解放までの間、**物理的に同じセッションが
維持されている**ことが前提になっている。

pgbouncerのtransaction pooling（Neonの`-pooler`エンドポイント等）は、クライアントから見た
1つの「接続」の裏で、トランザクションごとに異なる物理セッションを使い回す。ロック取得
（`SELECT pg_try_advisory_lock(...)`、1トランザクション）と解放（`SELECT
pg_advisory_unlock(...)`、別トランザクション）が同じ物理セッション上で実行される保証が
無くなるため、以下のいずれかが無言で起こりうる:

- ロック取得直後にコネクションプール側がセッションを使い回してしまい、実質的にロックが
  即座に失われる（＝ロックを取っていないのと同じ状態で後続処理が進む）
- `pg_advisory_unlock`が別セッションで実行され、「そのセッションはそもそもロックを
  持っていない」ため解放が失敗する（`false`が返るがエラーにはならない）

いずれのケースもPython側の`try_acquire_approval_lock()`は`row["locked"]`が`True`である限り
成功したように見え、例外も出ない。つまり**アプリケーションコードからは正常に動いている
ように見えたまま、TOCTOU対策（二重送信防止）だけが無言で無効化される**。

`_connect()`（`approval_db.py`/`project_mirror/db.py`/`relation_sync/db.py`のいずれも同じ
実装）は接続文字列を`os.environ["DATABASE_URL"]`からそのまま`psycopg.connect()`に渡すのみで、
pooled接続かどうかを検知・拒否する処理は無い（このコードを読んだだけでは分からず、Vercel側の
実際の環境変数を見る必要がある）。

**確認方法**: Vercelの環境変数設定で`DATABASE_URL`のホスト名に`-pooler`が含まれていないこと
（Neon×Vercel連携では、pooled接続は`ep-xxxxxxxx-pooler.<region>.aws.neon.tech`のような
`-pooler`サフィックス付きホスト名、非pooled（direct）接続は同じホスト名から`-pooler`を除いた
`ep-xxxxxxxx.<region>.aws.neon.tech`になる。Vercelには`DATABASE_URL`と対になる
`DATABASE_URL_UNPOOLED`が自動で用意されることが多く、advisory lockを使うならこちらを
`DATABASE_URL`として設定する必要がある）。

なお`dashboard/.env.local`（ローカル開発用、Gitには含まれない）を確認したところ、
`DATABASE_URL`は`-pooler`付きホスト名（pooled）を指しており、`DATABASE_URL_UNPOOLED`が
別途non-pooledホスト名で用意されていた。このモジュールのdocstringにあるとおりPython側は
「dashboard側と同じ`DATABASE_URL`環境変数を共有する」設計のため、本番（Vercel）の
`DATABASE_URL`も同様にpooled接続になっている可能性がある。

### 対処: advisory lock専用の接続だけ`DATABASE_URL_UNPOOLED`を使う（2026-08-28）

上記の懸念どおり、本番の`DATABASE_URL`（Vercel環境変数）が実際にNeonのpooled接続
（ホスト名に`-pooler`を含む）であることを確認した。APIプロジェクトのVercel本番環境変数に
`DATABASE_URL_UNPOOLED`（非pooled/direct接続）を追加し、以下のように設計を変更した:

- **advisory lockを取得・解放する接続だけ**`DATABASE_URL_UNPOOLED`を使う
  （`src/db_utils.py`の`connect_for_advisory_lock()`、`approval_db.py`の
  `try_acquire_approval_lock()`が呼ぶ）。通常のSELECT/INSERT等のクエリ用接続
  （`_connect()`）は引き続き`DATABASE_URL`（pooled）のまま——transaction poolingでも
  単発クエリは問題なく動作し、pooledの利点（コネクション数の節約）を捨てる必要が無いため。
- `project_mirror/db.py`・`relation_sync/db.py`の`try_acquire_refresh_lock()`も同じ
  `connect_for_advisory_lock()`を使うよう統一した（元は3ファイルにほぼ同じ`_connect()`
  実装がコピーされていたため、ロック専用接続の作成ロジックを`src/db_utils.py`に集約した）。

**この設計は環境変数が正しく設定されていることに依存している点に注意。**
`DATABASE_URL_UNPOOLED`が未設定の場合、`connect_for_advisory_lock()`は例外を出さず
`DATABASE_URL`へフォールバックして動き続ける（＝アプリは正常に見え、承認リクエストの
二重送信対策・夜間reconcileの多重実行防止だけが無言で無効化された今回と同じ状態に戻る）。
気づけるようwarningログは必ず出す（フォールバック先のホスト名に`-pooler`を含む場合は
さらに強い警告を出す）が、**ログを見ない限り気づけない**。デプロイ後、一度は本番ログで
`DATABASE_URL_UNPOOLED is not set`系の警告が出ていないことを確認すること。

**テストは全てモック（`psycopg.connect`の差し替え）であり、実Postgresで
advisory lockが実際に排他制御として機能することは未検証**（`tests/test_db_utils.py`の
`connect_for_advisory_lock`系テスト・各`test_db.py`の`prefers_database_url_unpooled`系
テストは、いずれも「正しいURLが`psycopg.connect()`に渡されること」の検証までで、
Postgres側の実際のロック挙動は検証していない）。確認するなら、非pooled接続
（`DATABASE_URL_UNPOOLED`と同じ接続文字列）で2つのセッションを開き、片方で
`SELECT pg_try_advisory_lock(917263542, hashtext('test'))`を実行してロックを取得した
まま、もう片方の同じクエリが`false`を返すことを見るのが確実（`psql`を2枚起動して手動で
確認できる）。

### Prisma（dashboard）側のデプロイ窓フォールバックが無い件は誤検知

Geminiから「`approverEmails`のNOT NULLフォールバックがPython側（`approval_db.py`）にしか
無く、dashboard/Prisma側に同等のフォールバックが無いのはおかしい」という指摘が出たが、
これは誤検知。**dashboardは`DocumentApproval`テーブルを一切読んでいない**（承認状況を
表示する画面が存在しない。送信は`POST /api/documents/quote/request-approval`経由でFastAPI
バックエンドへプロキシするだけで、Prisma側はこのテーブルへの読み取りアクセサ自体を持たない）。
読み取るのはPython側（`approval_db.py`、cronポーリング）のみなので、フォールバックが
Python側だけにあるのは意図した設計であり漏れではない。次に似た指摘が出たときに同じ調査を
繰り返さないよう、ここに記録しておく。

## デプロイ後の手動確認項目（複数承認者UI、コンポーネントテスト基盤が無いため）

このリポジトリには`.test.tsx`・testing-library・jsdom等のコンポーネントテスト基盤が無いため
（確認済み、2026-08-27）、`DocumentsPageClient.tsx`の承認者チェックボックスまわりは自動テストを
追加せず、デプロイ後に以下を手動確認する:

- 承認者を複数選択→一部を選択解除したとき、選択状態が正しく反映されること
- 承認者を0人にしたとき送信ボタンが`disabled`になり、1人以上選択すると再び押せるように
  なること
- 送信ボタン直前の「送信先: 〇〇さん、△△さん（N名）」表示が、選択中の人数・氏名と
  ずれずに切り替わること
