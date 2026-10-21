# 連絡先の所属を2原本で照合する定期処理

未所属の連絡先について、同じメールアドレスに結び付く独立した所属根拠が2系統以上、同じ既存取引先1件を指す場合だけ所属を補完する。既存所属、氏名、メール、営業記録は変更しない。姓名補完GASは変更しない。

## 根拠

|入力|採用条件|同一原本の扱い|
|---|---|---|
|EmailReviewの送信本文|SENT、DRAFTなし、Toが当人1件、CCなし。冒頭宛名の会社名が完全一致|当社側宛名を全送信者まとめて1系統|
|EmailReviewの受信本文|Fromが当人1件、明確な区切り内の署名に本人氏名とメールトークン完全一致を明示。紹介・説明・否定文は不採用|相手の署名を1系統|
|Zoho Contacts|Emailが当人と一致、field21のEight会社名・field25の所属会社名|Modified_Timeでは所属の鮮度を保証できないため加点せず、矛盾・lookup IDの確認専用|
|追加原本|本人メール・明示社名・証拠日時・原本ID・原本URL・由来originを指定|コピーは原本と同じoriginを指定|

根拠日時は365日以内を既定とする。これは新旧所属の混同を減らす保守的な運用値で、古い根拠しかない連絡先は不足で保留する。Zohoは原本の更新日時を記録し、名刺の交換日と同一視しない。人事異動フラグ、別会社の根拠、同名の取引先候補、同じメールの複数連絡先は保留。メールドメイン・電話だけでは決めない。商談の相手施設を所属とみなさない。新規取引先は作らない。

EmailReview以外のメール本文を新たにGmailから大量収集しない。既存の保存本文がないメールは票にならない。署名の会社行は既存名との完全一致、または法人・ホテル・旅館等の明示行を使う。自由な署名表現すべてを理解する仕組みではない。未知の会社表記を完全検出できる保証はないため、試算で採用根拠を確認する。

問い合わせの既存InquiryHandoffはpayloadが上書きされ、原本ID・元日時・コピー元がないため自動的に独立票にしない。追加は `PYTHONPATH=. python scripts/manage_contact_affiliation_job.py import-evidence --file <ローカルJSON>` で原本を保存する。sourceは `inquiry_form` / `application_form` / `explicit_affiliation_record` / `official_staff_profile`。各行は source/source_id/source_url/origin/email/company_name/observed_at/explicit:true を必須とする。同じsource_idの内容変更は拒否する。営業記録に単なる訪問先があるだけではインポートしない。

## 永続状態と排他

DBは初期 `enabled=false / dryRun=true`。ContactAffiliationJobは実行状態・巡回位置、Itemは各連絡先の状態・根拠・次回評価日時、Historyは判定履歴、Evidenceは追加原本を保存する。原文・顧客識別子をGASログや通知に含めず件数だけを返す。

Notion全件を作成日時昇順100件ずつ巡回する。9000件、または上限シグナルがあり次ページを取り切った時に、最後の作成日時から重ね読みし、IDのupsertで重複を除く。同時刻境界から進めなければ成功扱いにせず監視通知する。最終ページ後は先頭へ戻る。根拠不足の保留は週次で再評価する。新着メールがあれば優先評価し、新規連絡先も次周で取り込む。APIは1回40秒、GASは毎時最大4回・合計約180秒を目安に短バッチを連続実行し、全件を1時間で処理する保証はしない。

DB直接接続のジョブロックで二重実行を防止する。姓名補完と同じrecord_lockとメール単位ロックを使用。別接続の専用Notionキー必須で、通常同期botの自己更新除外に依存せず、共通dispatcherで明示配送する。保存前後に統合・削除・メール・所属を実読し、保存直前にも更新時刻を確認する。Notion APIには条件付き更新がないため、画面からの同時編集を原子的に防げる保証はない。

`writing → delivery_pending → verified`を永続化し、書込み結果不明時は保存値を読み直してから再開する。保存済みの同じ所属を二重に書かない。配送先Zohoの所属を読み戻し、未作成の対応IDや親、別会社の所属、照合失敗は配送待ちを維持する。連絡先の所属の同期対象は既存定義のZohoで、kintone・Sheetsへの未対応項目を成功と偽らない。

## 設置・運用

1. SQL migrationを先に適用し、API配備後も無効状態を確認。
2. 専用GASにCode.js/appsscript.jsonを保存。姓名プロジェクトのコード・トリガーを変更しない。
3. `affiliationJobIdentity` の公開aud/subを `CONTACT_AFFILIATION_GAS_AUDIENCE` / `CONTACT_AFFILIATION_GAS_SUBJECT` に設定。`CONTACT_AFFILIATION_NOTION_API_KEY` は通常同期とは別接続の秘密情報。
4. CLI `dry-run` → `/run`試算と根拠確認 → CLI `enable`。状態は`status`、停止は`pause`。
5. `startAffiliationJob`で毎時tickと15分監視を各1つ登録。GASロックも使用。`stopAffiliationJob`は専用トリガー2種類だけを停止する。

監視は最終成功時刻・トリガー数・配送実読の未確認件数・所要時間を確認する。本人宛ての障害通知を4時間抑制、結果通知を24時間抑制する。保留・試算候補は保存成功と別表示。OAuth権限は外部通信・トリガー管理・本人へのメール通知・openid。Slackへ質問や共有投稿を自動送信しない。

新着EmailReviewの更新日時カーソルを200件ずつ追跡し、GmailContactEmailと巡回済みemail索引から該当連絡先を優先する。保存済みverified・既存所属existingは通常の再評価で根拠不足へ降格させない。実物で所属解除が確認された場合だけ解除保留として記録する。初回全件走査はAPI `/discover` またはCLI `discover` を連続実行できる（原本変更なし）。

巡回時に既存所属existingまたは自動補完済みverifiedが空へ変わった実値を読んだ場合はreleased（解除保留）へ移す。人が意図的に解除した可能性を尊重し、新しいメールや根拠が来ても自動で再紐付けしない。履歴を調べたうえで管理者が再評価する。途中のwriting/delivery_pendingで試算へ切り替えた場合は1時間延期し、処理状態を保持する。

ローカル検証と実データ確認の結果は [検証記録](reviews/contact-affiliation-20261019/verification.md) を参照。実サービスでの新規紐付けはローカル検証に含まない。


## 初回の連続試算と採用根拠の確認

`dry-run`で有効にしてから `PYTHONPATH=. python scripts/manage_contact_affiliation_job.py discover --batches 1000` を実行すると、原本を書き換えず初回巡回を最大1000短バッチまで続ける。完了ページで止まり、途中停止しても保存カーソルから再開する。続いて毎時runの試算を進める。statusの `unevaluated`（未評価件数）、`total_discovered`、metrics内の`last_discovery_completed_at`・`last_evaluated`・`evaluated_per_minute`・`last_duration_ms`で到達と速度を見る。全件巡回の完了と全対象の評価完了は別である。

`candidates --output ~/.local/state/crm-sfa-integration/affiliation-candidates.json` と `history --contact-id <UUID> --output <私有JSON>` で、根拠・保留理由・日時を最大1000行出力する。新規ファイルのみ、権限0600、リポジトリ/Vaultへの出力は拒否する。採用を確認するまでenableしない。

Zohoのfield25は表示名だけで判断せずlookup IDを原本に保存する。IDを既存client_masterへ逆引きできない場合や候補IDと異なる場合は、Notionを書き込む前に保留する。メール重複等の既知の判断不能は理由付き保留として他の候補を続け、通信等の障害は再試行として監視する。


## 根拠の鮮度と判定の限界

Zoho/EightのModified_Timeは姓名等の変更でも新しくなる。所属の確認日とは扱わず、これらの値は独立2票に加算しない。信頼できる原本の所属確認日が得られるまでは矛盾とlookup ID照合専用である。したがって当社宛名だけの繰り返しでは確定せず、本人署名または日時・原本由来を確認した追加根拠が必要になる。当社宛名も過去の署名・名刺からコピーされた可能性があり、作成過程の独立性まで完全に保証するものではない。

名称一致では、取引先マスターに未登録の同名の他社が存在する可能性を排除できない。メール本人・複数原本・既知の矛盾・既存IDを照合する範囲での判定であり、現実のすべての同名企業を識別できる保証はない。試算の根拠を確認し、この限界を踏まえて有効化する。

試算でも同メールの連絡先一意性と取引先名の現物再読を行う。保存済みの不一致はwriting/delivery_pendingを保持し、既知の不整合として理由を記録する。配送待ちが残る日も、要確認通知に加えて日次結果通知で保存済み・未完の件数を報告する。巡回だけの実行では評価速度の前回値を0で上書きしない。

配送待ちdelivery_pendingはNotion保存実読済みなので、その後の所属解除を再PATCHしない。再開時とロック取得後の両方で空を確認した場合はreleasedへ移し、配送も止める。書込み結果が不明なwritingでも所属が空なら、未保存と人の解除を区別できないためreleasedへ移して再保存・配送を止める。同じ所属が保存済みの場合だけ実読回復と配送を再開する。releasedは巡回・新着メール・期限経過で自動再評価しない。

Notionのrequest_status=incompleteは初回ページから返る場合がある。有効なhas_more/next_cursorが残る間はページングを優先し、上限注記だけで同じ作成日時へ戻さない。注記は永続カーソルへ引き継ぎ、終端で重ね読みする。

## 2026-10-19の本番状態

本人の明示承認後、`1dedd28`を配備し専用GASの認証・毎時照合・15分監視を有効化した。初回の実行と監視を本番で確認。848件は既存所属833・情報不足等の保留15で、新規紐付け0。全件巡回・時間トリガーの自然実行・新規保存/配送の確認は継続事項。詳細と実測時刻は検証記録を参照。
