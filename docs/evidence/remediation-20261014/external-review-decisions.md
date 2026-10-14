# 外部レビュー初回の採否

2026-10-14、Claude・Geminiの初回回答を実装と照合した。モデルの指摘だけで不具合を確定せず、以下の範囲で修正・検証する。個別データや認証情報は掲載しない。

| 指摘 | 採否と根拠 |
|---|---|
| Claude B1：完了後も旧保存先へ書く | 採用。保存前の共有ページ排他内、およびdone返却前にアーカイブ・ゴミ箱・統合旧IDを確認し、永続保留へ戻す。正本を推測して自動作成・移し替えはしない。既存の結果不明予約も再作成しない |
| Claude W1：復旧キーが入口と異なる | 採用。pageIdに対応する既存のsourceKeyを使って再開する。既存行がない固定対象だけ復旧キーを使う |
| Claude W2：曖昧な問い合わせの入力を失う | 採用。company/name/email/phoneをinputPayloadへ保存してから同定する。候補が曖昧でも原本照合でき、確認後に同じ入口へ再投入できる |
| Claude W3 / Gemini B3：姓のガード | 採用。スキーマでは姓は任意だが、Zoho作成は明示欄のみをLast_Nameへ渡し、外部必須メタデータで判定していた。問い合わせの会社一意・明示姓の条件をdomain.inquiry_creation_holdへ共通化し、通常通知にも適用した |
| Claude W4：作成前原本変更の戻り値 | 変換欠落はなし。SkipTrackingDispatcher.dispatchはhub_creation_complete以外をskippedにする。実HubCreationService→実ラッパーの試験を追加し、source_changed_after_handoffの伝播と作成0件を確認する |
| Claude W5：比較する値表現が異なる | 異なる変換の前提は不成立。HttpNotionClient.get_pageとHubCreationService._creation_propertiesは同じparse_notion_property_valueを使用し、NotionSyncTarget.get_recordはget_pageを返す。実クライアントに固定API応答を与え、4項目の文字列・空値・関係IDの一致を試験する。推測的な正規化は追加しない |
| Claude W6：作成中予約の誤警告 | 新たな時間閾値は追加しない。GAS domain.planはunknownを初回pendingとし、同じ状態を48分以上後に再観測するまで警告しない。短期のreserve→finishは次回okへ戻る。journal.dismiss_holdはblocked行をDELETEする実装で、dismissed行の残留はない |
| Claude W7：監視の先行反映 | 本番監視はGASではなく、現在のcheckoutのdomain.jsを使うローカルlaunchd。新しい任意キーへの対応はAPI前に配置済み。未稼働の監視GASを新たに稼働させない |
| Claude W8：長期停止・重複IDで走査停止 | 採用。31日以下に分割し、成功位置を飛ばさず2時間重複で続行。重複IDは延べ件数・一意件数・出現回数を終端へ記録して完了できる。任意の4xxで記録を捨てる案は不採用。内容競合・件数欠落は依然として拒否し、未完了を隠さない |
| Claude W9 / Gemini W1：404の証跡・再出現 | 採用。missingフェーズのDB保存成功後にだけ旧走査のカーソルを破棄する。DB保存失敗なら同じIDで再送。元走査は完了不可。DB保存済みの消失IDは短期キャッシュで再取得ループを防ぎ、比較器では未観測と区別する |
| Claude W10：旧ログを自然観測へ算入 | 採用。明示kindとscanIdを持つ行だけを採用し、旧行の除外数を出す |
| Claude W11：同名候補の会社が不明でも作成 | 採用。同名候補があり会社で一意に同定できなければ保留し、新規作成しない |
| Claude W12：外部処理中のDBトランザクション | 本番読取実測はidle_in_transaction_session_timeout=5分、statement_timeout=0、lock_timeout=0。本番関数上限300秒・GAS1バッチ10通のため、長いI/Oでは記録の確定が失敗しうる。この場合は成功応答を返さずカーソルも進めない。EmailLogと効果処理の冪等性を使い再送する。開始行だけが残る状態は未完了であり合格証拠にしない。今回は予約と結果の2段化による新たな回復手順を追加しない |
| Claude I1 / Gemini B2：遅延開始の省略 | 採用。自然観測CLIはlatency-start必須。関数単体でも開始未指定では遅延を集計しない。初回遡及を自然遅延へ混ぜない |
| Claude I2：Prisma時計の差分 | 採用。migrationの既定をNOW()へ揃える。実記録は受信時刻を明示して渡す |
| Gemini B1：sent_at欠落 | 不採用。比較器はinternal_date_msから日時を生成する。sent_atは参照していない。独立QAのDB→export→compare照合も成功済み |
| Gemini B4：create_page_once未定義 | 不採用。HttpNotionClient.create_page_onceは既存実装。外部POSTの結果不明を自動再送しない専用経路として使用している |
| Gemini W2：deliver全体をページ排他内へ入れる | 不採用。通常Dispatcher・作成処理が同じ非再入の排他を取得する。保存時の排他と、配送・作成時の排他内原本照合を組み合わせる |

未経過の14日観測・移管後72時間並行確認は、これらの試験やレビューによって完了にはならない。本番実物の確認は別の結果欄へ記録する。

## 最終回答と後続対応

回答6ファイルとモデル・会話URL・SHA-256を[manifest](reviews/manifest.json)へ保存した。最終回答はGemini BLOCKER 0 / WARN 0、Claude BLOCKER 0 / WARN 4。以下の4件を採用した。修正後の外部モデルへの再々送は行わず、独立SEC・QAの差分確認で扱う。

| 最終Claude指摘 | 対応 |
|---|---|
| W-a：保存先404で毎回エラー | NotionApiErrorの404は入力を保持したblocked応答へ変更。他の例外は保留を保持して再送可能なエラーとする。権限由来も含む取得不能なので、物理削除が証明されたとは扱わず再作成しない |
| W-b：消失通知の応答紛失後に別IDも消失 | missingイベントの一意キーを走査ID＋メールIDに変更。同一連番で異なる消失IDも独立に保存し、各IDの再送は冪等。同じページの後続IDで応答を失い、次回までに先行IDも消える回帰ケースを追加 |
| W-c：手入力後も問い合わせの保留が残る | 自動解除は広げない。補完後に明示enqueueで既存台帳を再開し、返却・台帳・別理由保留を読戻す手順を記載 |
| W-d：復旧でdone行を再開 | sourceKeyの排他取得後に台帳を再読し、doneならstage・deliverを実行しない。異なる差分を渡しても完了済み処理を再実行しない |

追加確認2点：SkipTrackingDispatcherは`reason is not None`の場合だけ作成側結果を解釈し、Noneは通常dispatchへ進む。実HubCreationServiceのNone→実ラッパー→通常配送の試験で確認した。変換関数はnotion_webhook定義の同一オブジェクトをservice、notion_client、lead_inquiry_webhookが参照し、再エクスポートの同一性を試験した。

Claude最終回答の「300秒以内なので5分のDBタイムアウトには達しない」という断定は採用しない。関数とDBの境界時刻は同一とは限らないため、前節どおり記録確定失敗を未完了として扱う。


## 遅延登録回収の追加レビュー（本番反映前）

資料SHA256: `9f277c1ec9b4d826898d78090cbf4694f4702a7d044e892cef45374a96603519`。
Claude Opus 5.5（中）はBLOCKER 0 / WARN 4、Gemini思考モードはBLOCKER 1 / WARN 2。清書回答・URL・ハッシュはレビューmanifest参照。回答件数は外部モデルの報告であり、以下でコードと実測を照合した。

| 指摘 | 採否と対応 |
|---|---|
| Gemini B1: 9,000件到達でもhas_moreが先に選ばれる | 採用。has_moreかつ9,000件未満だけcursor継続し、それ以降は作成時刻境界へ進む。has_more=trueの回帰試験追加。 |
| Claude W1: 下書き・チャットが履歴に入る | 採用。新回収queryからDRAFTS/CHATSを除外。無関係な既存backfill全体は変更しない。 |
| Claude W2: baseline中の他項目更新が大量blockedになる | 採用。candidateとして参考件数へ分離し、通常の障害警告から外す。根拠なしに自動回収せず監査付き明示判断へ。 |
| Claude W3: blockedの出口・保留指紋 | 一部採用。現物指紋再照合・共通worker/page鍵・判断監査付きretry/dismissを追加。done/dismissed再開不可、期間と2日待機不変。指紋の全集合化は不採用。新規追加のたびに最古保留の警告年齢をリセットするため、最古指紋を保持し候補/保留件数を併記する。統合中を自動解除せず、統合完了後に現物を再照合して明示再開する。 |
| Claude W4: 1回1ジョブの停滞と正常待機の警告 | 採用。180秒予算内で各ジョブ10通ずつ順番に処理、1件の失敗で他を止めない。baseline予約は参考件数、activeでは各ジョブの進捗30分停滞/失敗と恒久保留を監視する。 |
| Gemini W1: RecordSyncBusyが巡回unknownになる | 採用。ジョブ単位で再試行待ちとして捕捉し、巡回errorを記録せず後続へ進む。 |
| Gemini W2: ANYの型推論 | 実害は独立実PG試験で再現なし。意図の明確化としてtext[]キャスト追加。 |
| Claude INFO: Notion時刻の分精度 | 採用。初回cutoffを分の先頭へ切り下げ、同じ分の新規作成をbaselineに吸収しない。 |
| Claude INFO: 消失ID重複 | 採用。消失IDは重複追加しない。 |
| Claude INFO: メールequalsの大小文字 | 既存ASCIIメール1件のlower/UPPERで同一の一意連絡先1件・has_more=falseを実API確認。全Unicodeの同一性保証ではない。 |
| Claude INFO: 実行上限/共通lock | 本番Vercel実設定fluid=true・functionDefaultTimeout=300を読み取り確認し180秒予算と整合。独立QAの実PGでselected job_ids境界・done再実行0を確認済み。 |
| Claude INFO: 後日追加した担当者/認証エラー分類 | 後日接続は既存連絡先を自動大量回収しない制約を明記。認証全体の設計変更は今回外。失敗ジョブは進捗保持し監視する。 |
| Gemini INFO: ページIDハイフン表記 | 今回はNotion API原本同士のIDを保存・比較する。表記相違は安全側で保留になるため追加変更なし。 |

新しい判断関数は隔離実PGで原本変更の拒否、監査保存、元期間/待機維持、消失IDだけの再開、done/dismissed保護を検証。追加差分は独立SEC/品質/QAで再確認し、BLOCKER 0 / WARN 0。本番結果は[実測記録](result.md)へ分けて記録する。
