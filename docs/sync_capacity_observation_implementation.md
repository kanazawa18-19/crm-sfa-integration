# 同期計測の実装とローカル隔離検証

2026-09-10 主機CX。着手時 `06b8f43`。
[観測計画](sync_capacity_observation_plan.md)の不足する計測を補うイシュー。
本番配備、継続観測の開始、クラウド資源の作成はこの作業に含めない。

## 計測の読み方

```text
受付 → 新規保存 / 重複 / 保存結果不明
                     ↓
実行枠取得 → 初期化 → 業務処理 → 結果保存の確認
    ↓          ↓          ↓              ↓
 取得時間    初期化時間   業務時間       保存時間
```

新規job数、HTTP再送数、workerの実行試行数は別の値。
保存が確定した記録と、保存の応答が失われた記録を足して新規job数にしない。
初期化失敗後のretry保存が確認できなければ、retry確定とは扱わない。
処理が強制停止して終了記録が無い試行は欠測であり、0秒や成功に置換しない。

各区間の時間は単調増加時計で測る。保存文書の更新日時と作成日時の差は
待機・再試行を含むため、業務時間の代用にしない。
ログの完全性・保持期間・配備先ごとの収集可否は配備後の別確認が必要。

専用のstderr出力は `sync_capacity ` に続くJSON。UTCの `timestamp` と
`severity` を持つ。アプリ全体や他ライブラリのログ設定は変えない。
区間秒数は直前・直後で計測し、区間外のログ出力時間は除く。
`attempt_seconds` はclaim開始前から保存確認までの全体時間で、ログ出力を含む。
DB枠の厳密な所有時間とは異なる。`claim_seconds` はclaim呼出し全体なので、
競合を分散する待ち時間と比較失敗時の再試行待ちも含む。純粋なDB通信時間ではない。

| event | 意味・集計上の扱い |
|---|---|
| enqueue / outcome=new | 保存確認済みの新規job |
| enqueue / outcome=duplicate | 保存済みjobの再受付。新規jobに足さない |
| enqueue_conflict | 同じ通知IDで内容が違う。旧内容は保持 |
| enqueue_unconfirmed | 保存の成否が不明。未保存・新規保存のいずれにも断定しない |
| receipt_response | 受付の応答準備結果。HTTP1件として数え、保存側イベントと二重加算しない |
| enqueue_scope_unavailable | 保存先の設定不整合により今回の保存を開始できず。過去の同jobの存在は否定しない |
| local_worker_busy | 同じプロセスで先行workerが動作中 |
| worker_store_unavailable | 保存先の初期化失敗。claim未開始 |
| claim_started / processing | claim開始 / 取得確認。ownerで試行を関連付ける |
| idle_or_full | 対象なし、または共有枠が満杯。両者を推定で分けない |
| initialization_finished / initialization_failed | 業務準備の成否。失敗時は業務未実行 |
| execution_finished | 業務呼出し終了。結果保存の確認ではない |
| finish_started / finished | 結果保存開始 / 保存確認。completedとneeds_attentionは別計数 |
| claim_unconfirmed / finish_unconfirmed | 取得・保存の応答が不明。WARNINGで記録 |

ログ出力に失敗しても業務を再試行しない。したがってログ単独では
保存件数の完全な台帳にならず、待機列との照合と収集途絶の検知が必要。

`receipt_response` は応答を作った時点の記録で、送信元への到達確認ではない。
`receipt_id` はHTTP要求ごとの乱数、`job_id` は通知をSHA256化した識別子。
同じ通知が再送されるとjob_idは同じでもreceipt_idは異なる。保存イベントにも
同じreceipt_idを付け、要求の結果と保存の結果を結び付ける。

| receipt_response.outcome | 確認できた範囲 |
|---|---|
| rejected_before_save | フラグ・入力・認証の段階で拒否 |
| store_unavailable | 保存先を初期化できずenqueue未開始 |
| scope_unavailable | 保存先設定が合わず今回のcommit未開始。過去の同jobの存在は不明 |
| content_conflict | 同じ通知IDの内容不一致。旧内容を保持 |
| save_unconfirmed | enqueueの結果を確定できない。例外の型だけで未保存と断定しない |
| accepted | 保存済み状態を取得し、成功応答を準備した |

このHTTP記録は待機列が有効な対象経路（および有効化フラグ不正）のみ。
無効時・他経路・配備先全体のHTTP総流量はプラットフォームの要求記録から取得する。

## 待機列全件の読み取り

`python -m src.sync_capacity observe` は設定されたscopeを読み取る操作。
本番で試すためのコマンドではなく、まず隔離環境で利用する。
既存の `inspect --limit` は個別調査用として残す。

- `state` と `created_at` だけを取得し、本文・認証値は取得しない。
- 全件走査なので、500件を超える待機列も対象。処理量と読取り費用は文書数に比例する。
- 取得途中で失敗した場合、途中の件数や0件を成功結果として返さない。
- 全件走査は一つの時点で固定した集計ではない。走査中の更新が混在しうるため、
  同じ時刻の厳密な整合性確認にはworkerを停止した隔離試験が必要。
- 最古待機時間はpending/retryの初回受付からの経過。再試行までの待ち時間も含む。
  不正・欠損・観測開始より後の日時があれば、最古待機時間を確定しない。

件数を毎分全走査する監視や自動通知はまだ登録していない。
日時の欠測、未知の状態、観測コマンドの失敗・途絶は、それ自体が要確認。
nullを「待機なし」と判定しない。processingの長期滞留は件数だけで自動判定できず、
個別inspectと開始記録を照合する。processing経過監視の自動化は後続の監視準備に残す。
全件scanの所要時間・費用・途中失敗率は、完了文書を含む実規模相当で配備前に検証する。
運用頻度は実際の文書数と費用を見て決める。保存・実行の仕組みに
計測専用の書込みを増やす方式は、このイシューでは採用しない。

## 未検証の境界

ローカル公式Firestore emulatorの結果は、実FirestoreのIAM・複合index・
永続性・課金・本番性能を証明しない。実Postgres/Neon接続ピーク、実外部同期先、
全配備の総流入、実Bot送達、定期途絶通知は別途検証する。
隔離資源の作成はproject ID・地域・費用範囲を具体化し、本人の指示を受けて進める。

## レビューと検証の記録

実装者とは別の担当がSEC/品質を読み取りレビューし、別のQA担当が試験を実行した。
品質担当の追加起動はツールのスレッド上限で拒否されたため、SEC担当が
品質観点も別に点検した。実装者の自己レビューを独立レビューとは扱っていない。

- 初回品質WARN：ログの発生時刻欠落、保存結果不明ログの重要度低下。
  UTC時刻・severity追加とWARNING維持で解消。最終SEC/品質はBLOCKER 0・WARN 0。
- 親レビュー：既定ログ設定でも値が出ること、未初期化scopeの偽0防止、
  ローカルworker混雑・保存先初期化失敗、区間時間へのログI/O混入を修正・検証。
- 独立の公式emulator試験：合成1,103文書（pending 1,102・retry 1）の全件数と最古時刻が一致。
  全文書・scopeの更新時刻不変、観測のcommit 0。途中取得例外は部分成功なし。
- 保存後応答喪失：claimとfinishの計2commit、業務実行1回、保存状態completed。
  追加解放・再実行なし。通常設定の専用プロセスから時刻付き計測JSON 7行を確認し、
  finish_unconfirmedはWARNING、誤ったfinished記録は0行。

### 他社レビューの採否

Gemini Pro・Claude Opus 5（中）を新規チャットで選択・表示確認した。
Geminiへの資料添付が自動承認レビューに拒否された。
非公開コードは認証情報がなくても機密になりうるため、具体的資料への直前承認が必要、
という理由。本人「承認しますよ」の後、同じ資料を両社へ送信した。

送信候補は `/private/tmp/crm-sync-observation-review.txt`、49,753文字。
SHA256 `63fb881421da743a5163717ee83f91b686f15bfe6f70e7cc47fd7a5941780051`。
差分・関連コード・合成試験・依頼文のみ。認証情報の指定パターン一致0件、
顧客情報・健康情報なしを内容確認した。本番ログ・本番環境変数は含めていない。
Geminiはファイル添付。Claudeはファイル選択がタイムアウトしたため、同一本文を
クリップボードから貼り付けて添付化し、プレビュー全文を照合後に送信。
空白除去後37,961文字・hash897657208が一致。キーによる文字入力は使用していない。

| Gemini Proの指摘（BLOCKER 0・WARN 3） | 採否 |
|---|---|
| 不正日時を除いて最古値を返す | 不採用。不正日時のjobが本当の最古かもしれず過小表示になる。nullと欠測件数を維持し、欠測自体を監視する |
| StreamHandlerへstderrを明示 | 不採用。実行環境の標準ライブラリは引数Noneをsys.stderrに置換する。明示しても差替え耐性は変わらず、通常設定の子プロセスで実出力確認済み |
| 大量文書で実行時間・資源を確認 | 配備前検証として採用。逐次集計で全件をリスト保持していないが、時間・費用・失敗率は実規模相当で確認が必要 |

上記は独立SEC担当も採否を再評価し同じ結論。大量文書の継続観測は未実施。

| Claude Opus 5（中）の指摘（BLOCKER 2・WARN 6） | 採否 |
|---|---|
| B1 HTTP拒否と保存結果不明の命名・照合不足 | 採用。HTTP応答準備をreceipt_responseへ分離し固定分類とreceipt_id/既知job_idで照合。HTTPと保存イベントは別計数 |
| B2 設定失敗まで保存結果不明扱い | 部分採用。scope不整合のみ明確に区別。ValueError/KeyErrorは保存前後を型だけで確定できないため一律の未保存分類を不採用 |
| W1 要確認・初期化失敗・内容競合がINFO | 採用。WARNINGで記録 |
| W2 最古nullを正常値の下限に置換 | 不採用。確定値を維持し、欠測自体を監視条件と明記。実測のない時計許容幅で0秒へ丸めない |
| W3 全件scanのタイムアウト・肥大化 | Geminiと共通。配備前の実規模検証に採用。ページングは実規模の必要性を見て別途判断。上限で打切った値を全件と呼ばない |
| W4 claim時間に待機・再試行を含む | 呼出し全体の時間であることを明記。計測のために既存の競合対策を移動しない |
| W5 processingの経過時間がない | 現時点の範囲を明記。個別inspectと開始ログ照合、自動滞留監視は後続準備 |
| W6 ログ故障が無音 | 業務を再送させない設計を維持。欠測・収集途絶監視を後続準備に残す。障害中の同じ出力先への通知を保証とはしない |

B1/B2は独立SECの評価では保存や再送を壊すBLOCKERではなく計測の意味を明確にするWARN。
必要箇所を修正し、過去の別HTTPによる保存を「未保存」と否定しない。
INFO 8件は、CLIヘルプ・空行整理を採用。イベント開始記録は強制停止の位置特定に必要なため維持。
job_idはdomain.submissionでSHA256化することを一次コードで確認した。
CLIのcomplete=trueは走査完走のみを表し、同一時点のsnapshot保証ではない。


- Gemini会話：`https://gemini.google.com/app/f199608327f4194c?hl=ja`
- Claude会話：`https://claude.ai/chat/cca959c1-1738-4929-a874-247e1dace744`


### 独立QA最終結果

| 検証 | 結果 |
|---|---|
| 全pytest | 2,926成功・10skip、29.30秒 |
| 対象＋公式emulator | 88成功、2.68秒。78件は全pytestと重複、残り10件がemulator専用 |
| 合成1,103件・保存応答喪失 | 前述の件数・時刻・書込み回数が一致。外部レビュー反映後も再確認 |
| HTTP→実emulatorの新規・重複 | HTTPと保存のreceipt_id/job_id一致 |
| enqueue実保存後の応答喪失 | HTTP503/save_unconfirmed/WARNING、commit1回・pending保存を再読、accepted記録0 |
| 計測の秘密非出力・時刻・重要度 | 通常設定の独立プロセスでも確認 |
| 試験サーバー | 今回起動したプロセスのみ停止、8787番待受なし確認 |

各pytest実行に既存Starlette警告1件。今回の新規警告ではない。
空の専用HOME、env -i、demoプロジェクトと127.0.0.1だけで実行した。
証跡は `/private/tmp/crm-capacity-qa/observation_full_reviewed.log`、
`observation_target_reviewed.log`、`observation_independent_reviewed.log`。
HTTP試験証跡は同ディレクトリの `observation_http_reviewed.log`。
独立試験本文は `observation_independent.py` と `observation_http_independent.py`。
一時ファイルが消えても再開できるよう、合格条件と件数は本資料に残す。

外部レビュー反映後の最終SEC/品質/QAはBLOCKER 0・WARN 0。
今回の計測実装・ローカル隔離試験のイシューは完了。本番容量保証は未達で配備保留。
