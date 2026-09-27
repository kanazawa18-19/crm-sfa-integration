# 残作業の独立レビュー記録

2026-09-27。対象は cd02121 以後の未コミット差分。実装を固定して3役を並行実施。

## 第1回 動物レビュー

|担当|指摘|対応状況|
|---|---|---|
|シロクマ|BLOCKER: 新規Notion案件の空FILESプロパティで解析例外|修正中|
|おばさん|BLOCKER: シート登録後の外部保留をシートから再判定できない|修正中|
|おばさん|BLOCKER: kintone必須ラジオボタンのAPI既定値を認めず常に保留|修正中|
|シロクマ|WARN: 外部発の新規Notionページへ初回メモが書かれない|修正中|
|シロクマ|WARN: 部分通知で未送信項目の保存済み業務値がメモから消える|修正中|
|おばさん|WARN: 対応アプリがない対象外まで永続保留になる|修正中|
|おばさん|WARN: 必須不足の具体的な項目・次の操作が不明|修正中|

くまQA（探索担当をQA役へ再割当）: Python 3,608成功・12スキップ・3件sandbox制約失敗。失敗3件はsandbox外で再実行して全成功、合計3,611成功。Node 32成功。

PostgreSQL17の使い捨て実DBに今回migrationを適用して確認した内容:

- 同じsourceで16同時予約、および別source・同じidentityで16同時予約: いずれも予約成功1件。
- reserved/createdは再予約不可、finish後の外部ID回収、blockedからの再開。
- unique競合のrollback、DB/target別スコープ、source_for_page、resolve_hold、pending集計。
- 試験DBは停止・削除済み。本番DBで試験していない。

修正版の再検証は別途追記する。上記は修正前版の結果。

### 再レビュー・実物確認

- 動物レビューの初回BLOCKER3件・WARNを修正し、シロクマ/おばさんともBLOCKER解消を確認。追加の登録状況更新、数値型検証も実装者と独立して確認済み。
- 21:46 JST時点のQA: Python3,636成功・12スキップ、Node35成功、差分形式チェック成功。その後Claude追加指摘を修正中のため、配備版の検証結果は後で区別して記録する。
- PostgreSQL追加5観点: 対象外の保留解除はblockedのみ削除し、reserved/created/別送り先の記録を保持。
- 本番DBには今回のmigrationだけを適用。適用前に未適用が1件だけであることを確認し、適用後の予約表は0行。
- 実API読み取りでZohoチェーンとkintone取引先の必須確認・重複検索・payload生成が成功。レコード作成POSTは行っていない。Zohoの他5DBはレイアウト必須項目等に対応不足があり、具体的理由で保留する。
- 承認済み隔離ページの対応表を一時アーカイブし、他ツールへ同期しない状態でNotion先頭メモを実API検証。Notion-Version 2022-06-28で空本文への先頭追加、既存本文の保持、カテゴリ順、同一内容で同一ブロックを維持、すべて成功。`memo-live-test.json`参照。対応表は新コード配備後に試験用として復元する。

## 他社モデルレビュー準備

専用の新規会話でGemini Thinking（画面表示3.6 Thinking）とClaude Opus 5.5・中を選択。
txt添付を両方で試みたが、ブラウザ連携のfileChooser.setFilesがエラーcode -32000「Not allowed」で失敗。自動承認審査の拒否理由は表示されていないため、承認拒否と断定しない。

代替として確認済みtxt全文を専用入力欄へ設定した。Geminiでは空白を除く全文の一致を確認して送信（表示上の改行は増加）。Claudeは貼り付けが自動的に「pasted, 2187行」の添付へ変換され、添付プレビューに元の全文が含まれることを完全一致で確認。標準依頼文27文字も照合して送信した。資料126,068 bytes、SHA256は external-review-manifest.json に記録。認証パターン・設定内の秘密値・非テスト用メールアドレスの検査で検出0件。資料はコード差分・新規ファイル・仕様のみで、取得済み顧客データを含めていない。

Gemini初回の「null配列がセル全消去」は公式ValueRange仕様（Null values will be skipped）と不一致で不採用。reservedのhold理由不更新は、結果未確認の状態と理由を保持する意図的な安全策。metadata保持は再判定・状況更新・応答不明回収に必要。根拠と動物レビュー修正差分を送り、Gemini再回答は追加BLOCKER/WARNなし。原文はgemini-initial.txt / gemini-followup.txt。

Claude初回回答はclaude-initial-snapshot.txt。B1は対象外分類修正済み。B2はNotion数値プロパティとシート列を配備前に用意して対応。B3は既存クライアント双方がidempotent=FalseかつZohoの業務応答SUCCESS判定ありと確認。B4のUUID/fieldは既存importあり。B5は上記Notion実機検証で解消。追加WARNの実害がある経路を修正中。

### 最終凍結版（21:54 JST）

- Claude追加指摘のうちW1/W3〜W7/W11〜W15を修正または手順に反映。W2は結果不明時の自動再POSTを避ける保守的な仕様を維持し、本人照合後の復旧手順を追加。
- W10はCOQLが現在のOAuth権限で使えないことを確認し、Searchではなく通常GETの完全な一覧を照合。2,000件までに完了しなければ保留。照合と作成の間の外部手動登録まで原子的に排除する保証はない。
- 動物再レビューはセキュリティ・品質とも追加BLOCKERなし。QAはPython3,651成功/12スキップ、Node4ファイル37成功、差分形式チェック成功。
- 実PostgreSQLのconflicts追加5観点も成功: reserved同名はID不問で競合、created同名同IDだけ競合、created別IDは非競合、DB/target/identity別の分離、POST前保留は非競合。使い捨てDBは停止・削除済み。
- 最終修正差分69,483 bytesを他社モデルへ再レビュー。SHA256はexternal-final-manifest.jsonに記録。Claudeはpasted 1,183行の添付プレビュー全文一致、依頼文27文字一致を確認し送信。

他社最終回答を取得。Geminiは新規BLOCKERなし。Claudeは以前のBLOCKER全解消、N1（メモ失敗が初回シート行作成を止める条件付き候補）とN2（通常同期後の再判定失敗が5xxになるWARN）を追加。初回シート行作成をメモより前に移し、再判定の例外を通常同期から分離する最小修正を実施。シロクマ独立確認で新規BLOCKERなし・回帰4件成功。22:01 JSTの最終QAはPython3,654成功/12スキップ、差分形式チェック成功。GAS37成功と実DB確認は変更なしで有効。外部回答原文はgemini-final.txt / claude-final.txt。

公式根拠: https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values
