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
- `is_active_document_approver()`（`approver_db.py`）は承認者ごとに個別にDB接続を張って
  検証しており、N+1気味の実装になっている（2026-08-27、shirokuma-secレビューINFOに加え、
  外部モデルレビュー(Gemini)でも同様の指摘あり）。承認者は業務上数名〜十数名程度で実害が
  無いため今回は見送った。将来、承認者数が増える等で問題になった場合は
  `WHERE email = ANY(...)`で一括クエリにまとめる余地がある。

## 外部モデルレビュー(Gemini)で出たが今回は見送った指摘（2026-08-27）

- **重複送信チェックのTOCTOU**: `find_in_progress_approval()`による重複チェックと、その後の
  テンプレートコピー・PDF変換・`start_approval()`送信・`DocumentApproval` INSERTの一連の
  処理はアトミックではない。ボタン連打や別ウィンドウからのほぼ同時送信で、チェックをすり
  抜けて同一案件・同一カテゴリの承認リクエストが二重生成され得る。**今回の複数承認者対応で
  新たに入った問題ではなく、単一承認者の頃から存在した元々の挙動**。
  修正するなら`("notionProjectId", category) WHERE status = 'in_progress'`の部分ユニーク
  インデックス追加が筋だが、**既存データに重複した`in_progress`行が無いことを先に確認しないと
  インデックス作成自体が失敗する**ため、調査コストが発生する。今回のデプロイには含めない。
  次にこのテーブルを触るときの最有力候補として記録しておく。

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
