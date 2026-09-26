# Zoho 通知停止中の「更新漏れ」を Notion へ届け直す

2026-09-27。スクリプトは `scripts/replay_zoho_missed_updates.py`（既定 dry-run）。
「作成漏れ」を扱う `scripts/backfill_zoho_missed_records.py` の続きで、対応表に**ある**レコードの
止まっていた間の更新を扱う。

## 何が問題だったか

Zoho の通知購読（watch）が 2026-09-15 16:01 JST に失効し、9/26 17:44 に復旧するまで Zoho 発の同期が
止まっていた。作成漏れは 9/26〜27 に回収済み。残っていたのが、対応表にある既存レコードの
**Zoho 側の更新**（案件 74・連絡先 131・アクション 14・取引先 7 が対象候補）。

通常の Zoho 通知は「変わった項目だけ」（`affected_values`）を運び、Dispatcher は項目ごとに Notion の
現在値・最終更新時刻と比べて書く。止まっていた間の変更にはこの差分が無い。レコード全体を流すと、
Zoho と Notion で違う全項目が「競合」として最新更新優先で解決され、

- Notion 側で後から直した値を Zoho の古い値で潰す
- もともと同期対象外だった違い（移行時からのズレ）まで巻き込む
- Notion の方が新しいと判定された項目は Notion の値を **Zoho へ書き戻す**（NOTION_OVERRIDE）

という 3 つの事故になりうる。

## どう解いたか

```
   Zoho v6 一覧（If-Modified-Since）─▶ --since 以降に変更された ID
        │
        ▼ 対応表にあるものだけ（無いものは backfill の担当）
   Zoho Timeline API（v6 __timeline）─▶ どの項目が・いつ変わったか（項目名と時刻だけ使う）
        │                               値の形は表示用（"￥ 20,000"）で通知と違うので使わない
        ▼
   Zoho の現在値（v2 GET）─▶ 変わった項目の値を、本番と同じ変換に通す
        │                     （zoho_payload_to_sync_events。取引先の上書き防止ガードも同じ）
        ▼
   Notion の現在値・ページ最終更新時刻 ─▶ 項目ごとに分類
        │
        ├ already_synced   Notion が既に同じ値
        ├ safe             Notion ページが Zoho の変更より前にしか触られていない → 流す
        ├ ambiguous        Notion ページが Zoho の変更より後に編集されている → 流さない。人が見る
        ├ unmapped         同期対象外（変換表に無い）
        ├ relation_pending 読み取りだけでは値が決まらない（名寄せが要るリレーション）。Notion が
        │                  後から触られていなければ --apply で本番と同じ解決を試みる
        ├ not_converted    名寄せが要るが Notion が後から触られている → 流さない
        ├ missing_in_record Zoho の現在値に項目が無い（空欄化と誤解しない）
        └ changed_after_until --until より後にも変わった → 流さない
        ▼
   --apply: safe と relation_pending だけを「Zoho の通知」の形に組み立て直して Dispatcher へ
            書く直前に Notion ページ（時刻と値）と Zoho レコード（値と Timeline）を読み直し、
            分類時から動いていたら流さない
```

| 守りたいこと | どう守っているか |
|---|---|
| Notion だけで直した項目を潰さない | Zoho で実際に変わった項目しか流さない |
| Notion で後から直した項目を潰さない | ページの `last_edited_time` が Zoho の変更より後なら ambiguous。Notion の時刻は分単位に丸められるので同じ分も ambiguous |
| Zoho へ書き戻さない | safe は「Zoho の値が最新」と確かめてあるので、Dispatcher の判定は必ず Zoho 採用（PROPAGATE_VALUE）になる |
| 分類から書き込みまでの間の Notion 編集を潰さない | 書く直前にページを読み直し、最終更新時刻が進んでいるか、流す項目の値が分類時と違えば `notion_edited_after_plan` として流さない（shirokuma-sec レビュー BLOCKER。値も見るのは、Notion の時刻が分単位で同じ分の編集が見えないため。ChatGPT レビュー BLOCKER） |
| 分類から書き込みまでの間の Zoho 変更を巻き戻さない | 書く直前に Zoho のレコードと Timeline を読み直し、流す項目の値が違うか、分類後にその項目が動いていれば `zoho_edited_after_plan` として流さない（ChatGPT レビュー BLOCKER。分類時の値を「今の最新」として流すと、通常運用の新しい変更を古い値で上書きし、その本物の通知まで stale_event になる） |
| --until 指定時に期間後の値を流さない | Timeline で until より後にも変わった項目は `changed_after_until` として流さない（現在値は期間内の値ではない。ChatGPT レビュー BLOCKER） |
| 書かないレコードに副作用を残さない | 分類の間は名寄せ（`RELATION_SYNC_ENABLED`）を切る。確認待ちキューへの書き込みは --apply で実際に流すレコードだけ（obasan-quality レビュー WARN） |
| 競合で捨てた値を見えるようにする | Slack 通知は切ってあるので、Dispatcher が捨てた値を標準出力と結果 JSON（`rejected`）に出す |
| 新規ページを作らない | `AUTO_CREATE_NEW_RECORDS_ENABLED` を常に false にする |
| 空欄化の誤伝播 | Zoho の現在値に項目が無ければ流さない。項目があって値が None のときだけ空欄化（本番と同じ） |

## Timeline API について（実測）

- `GET /crm/v3/{module}/{id}/__timeline` は `API_NOT_SUPPORTED`（supported_version: 5）。**v6 で使う**。
  既存の CRUD は v2、一覧は v3 なので、このスクリプトだけ v6 の URL を組み立てる
- エントリは新しい順。`action` は `updated` / `added` / `slack_notification`（workflow）など。
  項目の履歴 `field_history` を持つのは `updated` だけで、`{api_name, _value: {old, new}}` の形。値は表示用文字列
- 204 は履歴なし
- v2 の `GET /{module}/{id}` の応答に `Modified_Time` は含まれていなかった（`Last_Activity_Time` はある）

## イベントの時刻を「今」にしている理由

Dispatcher は対応表の最終同期時刻より古いイベントを `stale_event` として捨てる（古い通知の巻き戻り防止。
`docs/record_sync_freshness.md`）。通知復旧後に別の項目が同期されたレコードでは、止まっていた間の
変更時刻はこの下限より古く、そのまま流すと捨てられる。止まっていた間の変更は「捨ててよい古い通知」では
なく「まだ届いていない通知」なので、イベント時刻は再送時点にする。safe な項目は Notion 側にそれより
新しい編集が無いことを確かめてあるので、時刻を今にしても採用される値は変わらない。
副作用として、そのレコードの最終同期時刻が今に進む。再送時点より前に起きた本番の通知が遅れて届いた
場合は捨てられるが、本番の通知は数秒で届くので実害は無い。

## dry-run の実測（2026-09-27、案件 Deals 74 件）

| 分類 | 件数 |
|---|---|
| レコード ready（safe あり） | 13 |
| レコード nothing_to_do | 61（うち 3 は 9/27 に backfill が作った新規。58 は Timeline に項目の変更が無い。`Last_Activity_Time` が全て 9/25 19:18:07 で揃っており、項目単位に記録されない一括操作と推測。未検証） |
| 項目 safe | 30（営業ステータス・月額費用・失注理由・失注日・契約日 / 予想契約日 など） |
| 項目 ambiguous | 0 |
| 項目 not_converted | 2（連絡先・サービス・商品のリレーション。レビュー反映後の分類では Notion が後から触られていないので `relation_pending` になり、--apply で解決を試みる） |
| 項目 unmapped | 21（Probability 等、同期対象外） |

全モジュールの結果は `~/notes/Dev/crm-sfa-integration.md` の作業ログを参照。

## やらないこと・残ること

- 止まっていた間に Zoho 側で**削除**されたレコードは扱わない（backfill と同じ）
- Vercel 停止（9/16〜21）中の **Notion 発**の変更が Zoho / kintone に届いていない可能性は、この
  スクリプトの対象外（未調査）
- ambiguous は自動では流さない。人が見て Zoho の値でよいと決めたレコードは `--apply --trust-zoho-id <ID>`
- 書く直前の読み直しから Dispatcher の書き込みまでの数秒に Notion 側が編集されると、Zoho の値が勝つ。
  本番の通知でも同じ窓はある（Dispatcher はレコード単位のロックの内側で現在値を取り直す）
- 案件 74 件中 58 件は Timeline に項目の変更が無いのに Zoho の一覧では「変更あり」に出た。`Last_Activity_Time`
  が全件 9/25 19:18:07 で揃っており、項目単位に記録されない一括操作と推測（未検証。流すものは無い）

## レビュー（2026-09-27）

- shirokuma-sec: BLOCKER 1（分類→書き込みの間の Notion 編集）→ 書く直前の再チェックを追加。WARN 1（却下値の不可視）→ 結果に出す
- obasan-quality: WARN 3（分類中の確認待ちキュー書き込み／同上の再チェック／終了コード 3 の説明）→ すべて反映。INFO は凡例・ID 表示・引数名 `--trust-zoho-id`・Timeline の並び非依存を反映
- kuma-qa: BLOCKER 0。WARN 3（境界一致・ズレ印・収集のテスト不足）→ テスト追加。scripts 272・webhook_handlers 325 件通過
- ChatGPT（Sol・中程度、[会話](https://chatgpt.com/c/6ab80d03-8364-83e8-8a18-7c6cf3be05ce)）: BLOCKER 3・WARN 3・INFO 1。
  BLOCKER はすべて採用（①分類後の Zoho 再更新 → 書く直前に値と Timeline を再確認 ②Notion の同じ分の編集 → 値も比較
  ③--until 後の変更 → `changed_after_until`）。WARN も採用（Timeline は最後まで読む／dry-run でも error は終了コード 1／
  同じ秒の複数変更は old/new を推測しない）。INFO の項目単位の `--trust-zoho-field` は見送り（レコード単位で足りる。必要になったら足す）
- Gemini（3.1 Pro、[会話](https://gemini.google.com/app/cd9d79a5b14a5785)）: BLOCKER 1・WARN 3。BLOCKER（テストが Notion の時刻キー
  `_notion_last_edited_time` を文字列で直書き → 定数が変わるとテストが日時比較を通らずに偶然通る）は採用し定数参照に。
  WARN「run() 内の `os.environ` 変更が変換側に効かない懸念」は不採用（`src/relation_sync/resolve_zoho.py` は呼び出しのたびに
  環境変数を読む。実測で確認）。WARN「再送直前の正規通知が stale_event になる数秒の窓」は設計どおりで本文に明記済み。
  WARN「--until 指定時に現在値が流れる」は ChatGPT の BLOCKER ③と同じで反映済み。
  **3.6 Thinking はファイル添付でも本文貼り付けでも「対応できる機能がない」と 2 回断った**ので 3.1 Pro で実施
