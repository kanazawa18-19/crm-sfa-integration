# 残件の実稼働復旧（2026-09-29、業務情報待ち）

> 9/30の残件実行結果は [通常入口・関連保留の最終証跡](remaining-execution-20260929.md) を参照。確定ID476件の修復・読戻し完了、保留114件。以下は9/29時点の完了記録。

本人の一括許可 `docs/handoff_completion_20260929.md` に基づく。完了済み処理を再実行せず、本人認証と未確定の業務事実を最後に分ける。非公開の原本・比較・操作台帳は git除外の `migration_output/completion-20260929/` に保存する。

## 11:34時点の更新：Zoho認証とmain本番反映も完了

本人がAPI Consoleへログイン後、既存設定とのID照合で **Self Client** が対象と確定。NotionLinkGASは別クライアントだった。組織90001966327を選び、既存の `ZohoCRM.modules.ALL` / `ZohoCRM.settings.ALL` / `ZohoCRM.notifications.ALL` を維持し、`ZohoCRM.coql.READ` を追加して再認可した。更新用トークンはmacOSキーチェーン `crm-sfa-zoho-refresh-token` に保存し、本番APIの認証3項目へ設定。新しい秘密値はリポジトリ・Vaultへ置いていない。ローカル `config/.env` の旧トークンは変更していないため、COQLをローカル再検証する場合はキーチェーンの新値をプロセス内で渡す。

- 実製品コードから6モジュールすべてでCOQLのID比較・昇順・次ページ・更新日時条件が成功。取引先37,496件を19ページ＋更新差分1ページで照合し、6.81秒・1回でverified。しおりはメモリだけ、外部書込み0。更新差分は観測窓に対象0件だった。
- 独立QA：Python全体4,032成功/79skip、関連追加48成功（実PG44＋既存の大規模照合4）。重複を除く4,076成功/35未実施。環境由来の初回7失敗は該当試験だけ調整して全成功。本番接続なし、試験スキーマ残0・専用PG停止済み。
- mainの18コミットをpushし `d742105` をAPI・管理画面とも本番READYと確認。API `dpl_3Pc6NCT2a6sztcJsQJFfPWhe5LwN`、管理画面URL `https://crm-sfa-integration-dashboard-daiptvavu-cnctor1.vercel.app`。両方の配備メタデータが同一コミット。
- [CI 36512765225](https://github.com/kanazawa18-19/crm-sfa-integration/actions/runs/36512765225) は全ジョブ成功。Python4,028成功/83skip、GAS・Docker/health成功。API本番healthz 200。
- 本番管理画面からZoho診断を実行し **11:33:43 JST、1.5秒、参照確認済み**。本番API自身からZohoへの接続成功を確認した。手元の旧管理用トークンによる診断401は、既存Googleログインを使った正規の管理画面から確認して解消した。診断成功を全Webhookの配送確認とは扱わない。
- 非公開証跡：`coql-live-readback.json` / `coql-full-scan-readback.json` / `main-deployment-readback.json`。API Consoleの秘密値を含み得る全画面出力は自動審査で拒否されたため、値を出さない一致判定と非機密項目だけの確認へ縮小して実施した。

以下の配備・認証待ちは上記更新前の経緯。残りはFIRSTfit深井店・DEARの所在地と異名3施設の営業上の判断だけ。完了した実配送・日報・施設23組の処理は再実行していない。

## 実物まで確認できたこと

|対象|実測・読戻し|
|---|---|
|日報9/28分|案件27,792・アクション74,399、計102,191件を収集し、9/29 03:00:17 JSTに本人DMへ自然送達。DB台帳deliveredと実際のSlack本文691文字・SHA256が一致。管理画面でも配信記録を確認|
|確定施設23組|23ジョブ全てdone/readback_verified。旧23ページarchive、旧ID解決、旧側固有関連10件の保持、正本読戻しを確認|
|旧失敗5件中3件|所在地を社内原本と公開出典で裏付け、Notion/kintone/シートへ補完。Zohoの全件照合＋直前差分照合後に登録し、全送信値・正逆対応表・シートを読戻し。残り2件は所在地未確定|
|商品関連|専用の取引先・商品・案件3件でNotion双方とシートの関連ID一致、再通知で増殖なし、全試験データの片付けを確認|
|新規登録9経路|Zoho6種類とkintone3種類で実作成・正逆対応表・シートの読戻し、再通知同IDを確認。Zoho送信値は取引先6/チェーン6/連絡先4/案件16/商品7/アクション6項目が一致。試験片付けと独立読戻し完了|
|追加のアクション試験|競合修正後に新しい専用アクションを実作成。再通知同ID、同じ履歴メモのNotionページ1件、正逆対応表一致。片付け完了。受信ごとの監査記録は0件のため、それだけで個別Webhook到達を証明したとはしない|
|DB移行|20260928130000_hub_creation_scan適用、テーブル存在、未適用0を確認|

日報：[本人DMの実メッセージ](https://cnctor.slack.com/archives/D0BNE3Y0P9Q/p1790618417840539)。本文SHA256 `370cec0c01efe35ea7530749009ce11e00eb327019cb4de4eed56b657ceda6d0`。
施設の計画hash `89269e2eaffeaab663764d45ec52837ce26cf82c40d4e9a1120b01817dc881a4`。計画・全23件の読戻し結果は `migration_output/remaining-20260928/facility-current-{plan,result}-private.json`。

## 障害と修正

- 日報はDB容量上限のSQLSTATE 53100で保存不能。93,545行の全原本を圧縮バックアップし、集計に必要な項目を保持した保存へ変更。DBは513,417,216から312,549,376 bytesへ縮小した時点を実測した。負荷・容量試験ではない。
- 日報の一括全件読取も10秒の制限に達したため、同じトランザクション内で1,000行ずつ読む方式へ変更。送信予約前に本文長を検査し、本人DM宛だけを許可する。
- 施設archive後にNotion本文APIが404になる場合を、承認済み履歴と完了済み処理の確認付きで再開できるよう修正。
- 商品・取引先の関連IDがシートに届かなかったため、双方のシート配送を追加。
- kintoneの案件・アクション作成は、参照元取引先アプリの認証も必要。正式bulk APIの強制ロールバックによる最小再現で追加作成0を確認し、参照元トークンも重複排除して付けるよう修正。
- kintoneアクション作成直後の返信が対応表保存より先に到着し、Notion複製1件ができた。実物を照合してその複製・シート行だけを片付け、元の対応を復元した。外部作成から対応表保存までと、外部からの新規受信を同じ鍵で排他するよう修正した。
- 作成結果不明の予約が同じDB・送り先に残る間は、新規受信を再試行可能な状態で止める。既存更新は継続する。確定IDの履歴を先に照合し、タイトルの組み立て直しで二重作成しない。予約は根拠なく解除・再POSTしない。

## 配備と検証

main `ae8ec44` はorigin/mainより17コミット先。全て退避ブランチ `recovery/completion-20260929` にpush済み。COQL権限がない状態でCOQL依存コードを配備しないため、必要な修正だけを `fix/report-recovery-20260929` に抽出した。

本番API・管理画面は同じ `613f5f3` でREADY。API `/healthz` は `{"status":"ok"}`。

- API: `dpl_34qMyK35UhA2boXq4fXYhja3WVym`
- 管理画面: `dpl_CDf2vzJnKLZsttkDdyyTf44qmZSS`
- [確認用draft PR #1](https://github.com/kanazawa18-19/crm-sfa-integration/pull/1)。mainへ未統合。

変更周辺の独立QA783件、最終追加の実PostgreSQL8件、COQL未配備部分を除いた抽出版の関連108件が成功。SEC/品質レビューは最終B0/W0。Claude Opus 5.5・思考量中のレビューはB0で、対応表再読・確定IDの先行判定を反映した。Gemini Thinkingは今回のレビュー依頼に一般的拒否を返したため未実施とする。過去のCOQL実装に対する両社レビューの完了とは区別する。

最新CI [36473393797](https://github.com/kanazawa18-19/crm-sfa-integration/actions/runs/36473393797) はPython 3,980成功・39skip、GAS38成功・0skip、Docker build/health成功。DB依存のskipは、別途localhost実PostgreSQLの試験で確認した範囲と区別する。

案件登録の追加修正 `ae8ec44`（配備抽出版 `613f5f3`）：

- 実v2 Pipeline APIは400 `API_NOT_SUPPORTED`、v2 Fields/LayoutはPipeline自体を返さない。案件のFields/Layout/Pipeline読取とPipeline付き案件POSTだけv8へ揃えた。その他のCRUD・接続先・認証キャッシュは維持。
- 実設定は3レイアウト、パイプライン一覧は204/200/200、既定trueは全体で1件。既定を推測せず、一意性とStage所属を確認してPipeline・Layout IDを送信する。別レイアウトの必須項目を要求しない。
- 実リードソースの表示「テレアポ」「メール営業」は内部値「選択肢1」「選択肢2」。単一の受信表から送信を逆引し、未知混在を保留する。作成時は取得済みメタデータの表示名と内部値も再照合する。
- 独立QA285成功（実PG44含む）、最終metadata版変更後の入口36ケース全件成功、追加の設定変更検知を含む抽出版65成功。SEC/品質は残B0/W0。ClaudeはB0、表示名の将来変更に関するWARNへ作成前の実設定照合を追加。通常更新は既知対応表を使用するため、Zoho選択肢設定変更時には対応表も再検証する。
- [公式の案件登録仕様](https://www.zoho.com/crm/developer/docs/api/v8/insert-records.html)、[パイプライン取得仕様](https://www.zoho.com/crm/developer/docs/api/v8/get-pipelines.html)。実試験で案件16項目（Pipeline・Layout・関連IDを含む）の読戻しが一致した。

## 継続中・本人にしか解けないこと

- Zoho6経路の限定試験は正式v8 Get Recordsの全件走査と直前If-Modified-Since差分で重複候補を確認し、全6実配送・再通知・読戻しを完了。これは本番COQL走査の検証完了を意味しない。Notion原本を共通Dispatcherへ渡した実サービス試験で、各新規POSTが本番Webhook入口を通ったとの主張ではない。
- 正しいZoho組織90001966327、既存原本ID、有効ユーザー、本人の担当者対応は確認・設定済み。COQLは401 OAUTH_SCOPE_MISMATCH。API Consoleが本人のパスワード/OTP等の再認証を求めるため、権限追加と本番COQL実照会は未完了。
- 旧失敗の残りはFIRSTfit深井店・DEAR。社内契約原本に住所がなく、公開情報だけでは店舗所在地を確定できない。創作・他店住所の流用はしない。
- 異名3施設（斉木別館・千年亭・松涛園）は候補13件の現原本・対応表・関連を追加読取済み。法人・旧名称・施設を営業上同一にまとめるかは証拠だけで決められないため統合していない。
- Drive承認、容量/負荷試験、Slack認証交換、完了済み監視/Q058試験は再実行しない。

## 限定試験の照合根拠

走査開始時点の件数：Accounts37,496 / CustomModule3 232 / Contacts4,715 / Deals26,071 / Products326 / CustomModule2 27,410。200件ずつのID昇順・page_token・more_recordsを検査し、試験名の候補0を確認。連絡先がメール必須だったため、試験用example.invalidアドレスも含めてContacts4,715件を再照合し候補0。その後も各POST直前に更新差分を再読する。

アクションの「その他」は対応するZoho値がないため作成前に保留となることを実確認し、専用試験を対応済みの「テレアポ」へ変更して配送を確認した。未知の業務値を任意のZoho選択肢へ丸める変更はしていない。

最終DB読戻し（`final-db-readback.json`）：日報phase=done、送達delivered、今回の施設23ジョブdone、追加移行finished・rollbackなし。全体のHubCreationAttemptでstate=reservedは0件。試験済みの作成履歴は、返信の再登録防止と監査のため保持する。

試験上の通信障害：追加アクションの対応表作成がNotionのReadTimeoutとなったが、登録元の履歴を保持して安全に再開し、外部ID増殖なしを確認した。片付け時の二重アーカイブは404になるため、既にarchive済みか読み戻してから再開した。

## 片付けと最終状態

Zoho試験6件、kintone試験3件＋追加アクション1件を、実ID・名称・作成者・版番号（kintone）照合後に削除し、APIで不存在を確認。商品関連試験・途中にできた複製を含むNotion11ページはarchive済み。独立した最終読戻しで11/11の対応表なし・シート同期キーなしを確認した。試験に対応する未作成保留0、結果不明予約0、商品連携タスク0、シート配送待ち0。非公開 `final-test-readback.json` に各IDと結果を保存。

追加アクションから自然通知で発生した未作成Zoho保留1件は、原本archive・対応表なし・シートなしを確認して退避後に解消。作成済み監査履歴は保持した。完了済み業務データの再作成・日報再送・監視再設定は行っていない。

本人に必要な情報は、FIRSTfit深井店の確定住所、DEARの対象拠点・確定住所、斉木別館・千年亭・松涛園を営業上同じ相手に統合するか別の契約先として残すか。Zoho本人認証・COQL権限・main反映は上記11:34の更新で完了した。
