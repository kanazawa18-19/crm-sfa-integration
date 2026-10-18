# 姓名補完のGAS定期実行

GASが5分ごとに専用APIを4系統で呼び、承認済み33,042件の計画を少しずつ進める。
進捗はPostgresへ保存し、Macを閉じても次回から再開する。Web公開GASは作らない。

```text
GAS 5分トリガー → 認証付きAPI 4系統 → Notion保存前後照合
                         ↓
                  DBへ1件ごとの結果保存
GAS 15分監視 → 停止/進捗停止を本人メールへ通知
```

## 安全条件

- 固定ジョブIDと事前計画のみ。APIは任意の対象・変更内容・認証キーを受け取らない。
- Google署名・有効期限・専用GASのaudience・本人subjectのすべてを照合する。
  `CONTACT_NAME_GAS_AUDIENCE` / `CONTACT_NAME_GAS_SUBJECT` は公開識別子。秘密はGASに貼らない。
- Notion接続2〜5を使用し、接続1は通常同期専用に残す。
- 系統単位のDBセッションロック＋既存と同じキーのレコード単位ロック。
  無通信180秒で接続をDB側から終了する。実PATCH直前と保存後の照合後に両方の接続を確認し、失効した古い実行からは書き込まない。
- 既存値が変わったものはheld。書込み後の名前/姓/名すべてを照合してverified。
- 書込み成功後に進捗保存だけ失敗しても、再読一致により書き直さずverifiedにする。
- 接続障害は当該1件をretryにし、その回を中断。5〜30分後に再試行、8回目でfailed。
  Notionの混雑応答（429）は失敗回数を消費せず、次回以降に再試行する。
- 姓名に不要な関連の追加取得はしない。統合済み・削除済み・親DBの確認は既存Gatewayを維持。
- 保存前の読み取りとNotionへの保存は一つの不可分な操作にはできない。
  その間に本人が直接編集した場合の競合は残るため、実行中の対象の姓名編集は避ける。

## 導入・切替

1. ローカルの単体/実DBテスト、独立レビューを完了する。
2. 新規migrationとAPIを配備。ジョブは初期状態で無効、GAS認証の許可リスト未設定なら401。
3. 専用GASへ `gas/contact-name-job/Code.js` とmanifestを保存。`nameJobIdentity` を実行し、公開識別子をAPIの許可リストへ登録する。GAS認可が必要。
4. Mac側4workerを停止し、親/子不在とparallel.lock解放を確認する。終了途中の保存は再読で回収できる。
5. `python -m scripts.manage_contact_name_job import --root <私有計画フォルダ>` で試算し、`--apply`で登録。DB件数と元計画のID集合/ハッシュを照合する。
6. 同CLIの `activate --root ... --apply`。この時もMac側ロックが空いていることを要求する。
7. GASの `previewNameJob` → `startNameJob`。5分/15分トリガーを一覧で確認する。
8. 初回処理だけでなく、定期トリガーの次の実行とDBのverified増加、標本のNotion実値一致を確認する。

GAS所有者はWorkspace利用を前提に5分間隔とした。1回25秒は次の対象を始める予算であり、
実行時間の厳密な上限ではない。HTTPは15秒・再試行0、関連の追加展開なし。
実配置先の上限300秒と実測時間を確認する。GASは1回6分・Workspaceトリガー合計6時間/日。
他GASとの共有枠に注意し、長時間化した場合は間隔を広げる。DB障害そのものをクラウド移行で解決するわけではない。

## 停止・復旧

- `stopNameJob` はGAS定期実行の停止。DBの有効状態は変えない。
- CLI `pause --apply` はクラウド処理の一時停止。実行中の1件は終了することがある。
- 再開は原因を確認しCLI `activate ... --apply` → GAS `startNameJob`。
- `held` は人が元計画と現在値を確認する。自動でpendingへ戻さない。
- `failed` の対象はDBの `ContactNameJobItem` を `jobId='contact-names-20261017' AND state='failed'` で抽出し、
  notionId / errorType / attempts / updatedAt を確認する。接続復旧とNotion実値を読取確認後、
  対象IDを限定してstate='retry', attempts=0, nextAttemptAt=now()へ戻す。計画は変更しない。
  要確認で停止済みならGASの再開も必要。復旧SQLは確認済みIDだけを指定し、全held解除はしない。
- 完了/要確認通知は日本語件数と次の行動を記載。通知成功後に2本のトリガーを解除する。
- API応答やログには件数・例外クラスだけを出し、個人名や認証値は出さない。

## 公式仕様

- https://developers.google.com/apps-script/reference/script/script-app#getIdentityToken()
- https://developers.google.com/apps-script/guides/services/quotas
- https://developers.google.com/apps-script/guides/triggers/installable

## 検証・稼働記録

2026-10-18: 単体/実DB20件、ルート登録3件、模擬GAS8件を検証。
独立3レビューとGemini/Claudeレビューを実施し、判断は `docs/reviews/contact-name-gas-20261018/README.md`。

GAS実体は [CRM姓名補完・GAS定期実行](https://script.google.com/home/projects/1rg8a6aDQivx6h2XGsE3ceQeesAYHgwk17erRcMrlU1ZYLGWMqIxsuNjt/edit)。
Code.jsとmanifestの保存内容を読戻し照合済み。
2026-10-18本人がGoogle権限を許可。Notion接続2〜5を既存Vercel productionの秘密設定へ登録し、
専用GASのaud/sub許可リストを反映。実配備は `dpl_3FctYNauNve5qFq4U57D6ywLgn9U`、Production Ready。
GAS `previewNameJob` は12:27に認証成功・未取込状態を取得した。

旧Mac親/子5プロセス停止・parallel.lock解放を確認して、33,042件をDBへ移した。
元計画の全内容/順序/ハッシュ一致、保存確認3,764・未処理29,278・無効状態を読取確認後に有効化。
12:28:27に `startNameJob` を開始、12:29:02に正常終了。
初回64件を追加して保存確認3,828・残29,214、retry/held/failedすべて0。
各系統1件ずつの計4件をNotionから独立に再読し、名前/姓/名が計画と一致した。
トリガーはnameJobTick=5分、nameJobWatchdog=15分の2本。編集画面の選択値を読取確認。
旧Mac処理は再起動しない。クラウドの処理状況はCLI statusで読める。
初回と実標本の証跡は私有 `name-completion-approved-20261017/cloud-cutover-20261018.json`。

### 継続稼働と頻度の判断（2026-10-18）

16:11時点でverified6,820・pending26,222・retry/held/failed0。
16:10:34の時間主導型nameJobTickは29.872秒で完了、16:14:41のnameJobWatchdogも正常終了。
継続後の最新標本4件もNotion実値が一致した。Macなしで定期処理が継続している。

本人から1分化の提案と「弊害を考えて、ないなら続けて」と追加指示。
現状約30秒/回のまま1分化すると約12時間/日となり、Workspaceのトリガー枠6時間/日を超える。
同一アカウントの他GASも共用する枠であるため、弊害なしとは判断せず5分を維持した。
1分化には短い処理単位への変更と、実行時間・他GAS枠・実速度の検証が必要。設定変更は未実施。
完了/異常メールの実配送は未発生で未検証（通知コードの模擬試験のみ）。
