# 未連携点検・優先3項目・主機監視のレビュー

2026-09-27 主機CX。書き手は主機CXのみ。shirokuma-sec / obasan-quality / kuma-qa を並列に読み取りレビューへ回した。他社レビューはAPIでなく、本人アカウントのブラウザチャットへ機械的に秘密値パターンを除外したコード資料を送付。添付操作はブラウザ拡張が拒否したため許可された本文貼付を利用（Claudeは自動的に添付テキスト化）。

- Gemini（思考モード／Gemini 3.6 Thinking）：https://gemini.google.com/app/ccd3afa31e342c70?hl=ja
- Claude（Opus 5.5・思考量中）：https://claude.ai/chat/8e1c0746-0561-4272-8d8c-33c385f6ddbf
- 初回資料SHA256：93491457012fc581f94b43b58646ede5030eceba085b9a459c02077c47eb80a5
- 補助資料SHA256：0a22dfdb3ea6a67041f001ea279e0f11e135f55fc1d738490624496b0b0713dd
- 主機運用資料SHA256：f91925a8dab609aa7519bc424b3384b1cddfd224944312c4a96a1f1283a531eb
- 回答抜粋は同ディレクトリの6 txt。最終コードの細かい安全修正は以下に採否と検証を記す。

## 重要指摘への対応

|指摘|対応・根拠|
|---|---|
|Gemini：空の同期キーをmappedとする|mapped_row_without_sync_keyに分離、空欄境界テスト。先行取得4シートも実APIで空欄0を再確認|
|Gemini：同期キー列が無い場合の誤集計|キー列なし／重複時は中断|
|Gemini：IdMappingのnotion_page_id属性|誤指摘。実コードはnotion_key。実APIの全件取得でも属性例外なし|
|Gemini：1万件超のキーセット不足|id/created_timeで動く既存実装を利用。実データの取引先103,176件で1万件超の取得を確認|
|Claude：新同期項目の古い空欄が実値を消す|Zoho非空14件をNotion空欄へ補完・全14読戻し確認。Notion発3項目は対象DBの明示項目変更だけ。kintone全レコード通知のtoPerson空欄はSKIP_FIELD。Zoho差分通知の明示clearは維持|
|Claude：全DBの同名項目まで除外|対象DBと項目の組へ限定し修正|
|Claude：補完get_pageの型・自己通知|実クライアントは平たいプロパティ辞書を返す。14件全て実読戻し一致。自己botの通知を除外する既存経路あり。補完時点の本番には新3変換がまだ未反映|
|両者：大規模読取の中断|通信／5xx再試行、ID/日時だけのcheckpointと明示resume追加。途中中断から実際に再開して完走|
|動物：公開Actionsへの依存ログ|quiet時はlogging.CRITICALまで抑制。HTTP本文・URL・顧客値を出さない。Artifactも保存しない。Actions自体は無効|
|動物：状態読込がロック前|ロック内へ修正、二重実行の合成試験|
|動物・Claude：同じ実行失敗が毎時通知|別ロック・Slack受理後保存・24時間再通知に修正、初回／境界／失敗時未保存を合成試験|
|Gemini：--quiet未定義で必ず失敗|旧資料に基づく誤指摘。現コードのArgumentParserに定義済み。ヘルプとQAで照合|
|Claude：GASと主機二重稼働|GAS未認可・未設置。移管前に主機を停止する手順を運用文書へ記載|
|Claude：主機実行環境・.env権限|リポジトリはホーム直下。既存config/.envのgitignore・0600を確認。実launchdからキーチェーン→診断→Slack受理→last_successを確認|
|Claude：pendingのまま受信進行で復旧しない|初回イシュー1レビュー時点で修正済み。domain.jsと回帰テストに受信進行時の復旧／新たな未着を確認する分岐あり|
|Gemini：例外原文を出す|不採用。認証やURLの漏洩を避け、型名のみ出す|

## 検証

- kuma-qa：全Python回帰3,546成功・12スキップ・失敗0。
- その後のkintone空欄保護：shirokuma-secが独立レビュー（BLOCKER/WARNなし）、関連109テスト成功。
- GAS判定7テスト成功。主機ラッパーのpreview無送信、Slack失敗時未保存、排他、古い結果の再走査、設置preview、異なる既存設定拒否を合成検証。
- 実APIの全件照合・14件補完・本番専用診断の認証成功/401・Slack受理を別途実測。全Python成功を本番4方向の完全な編集試験の代用とはしない。
- 主機停止中の点検停止、全レコードの同時点性、API最終確認と書込の間の競合、残80項目は未解消の限界として記録。
