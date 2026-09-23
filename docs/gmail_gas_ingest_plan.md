# Gmail 取り込みを GAS の時間トリガーに置き換える案 — 設計検討

作成: 2026-09-24（主機CC）。**設計検討のみ・未実装。** コードも GAS プロジェクトも作っていない。

きっかけは 2026-09-21 の本番障害（Pub/Sub の再送の嵐で API が実質ダウン）と、
その後の本人の一言「gas とかうまく使えないの？」。

## 結論（先に）

**条件付きで推奨。** 「作って並走させ、2週間の実測で切り替えを判断する」まで進める価値がある。

```
   今の仕組み（Push）                     GAS 案
   ─────────────────────────────         ─────────────────────────────
   届いた瞬間に記録（秒〜分）             5〜10分遅れで記録
   部品が多く、壊れ方が4種類ある          部品が少なく、壊れ方は2種類
   担当者は画面から自分で接続できる       担当者ごとに GAS を配る手間
   GCP・OAuth鍵・watch延長を面倒みる       GAS プロジェクト1つを面倒みる
```

| 判断 | 内容 |
|---|---|
| 推奨する理由 | Push 方式の障害4種（再送の嵐・historyId 固着・watch 失効・鍵不一致）が**構造ごと消える**。CRM 側の判定・保存コードはそのまま使える |
| 条件 | ① 遅延が 5〜10 分になることを本人が許容する ② 担当者が 1〜3 名のうちは「各自 GAS」で運用できる ③ 並走 2 週間で取りこぼし 0 を実測する |
| 見送るなら | 高優先度インシデントの即時通知を「秒」で出したい場合、または営業担当が近く 5 名を超える見込みなら、GAS ではなく §8 の代替案（サーバー側ポーリング）を先に検討する |

## 1. 構成図 — 何が消えて何が残るか

### 現行（Push 方式）— 2026-09-24 時点のコードで確認

```
 Gmail ──新着──▶ Pub/Sub ──POST──▶ /api/webhooks/gmail-push
                 （再送あり）              │ ?token=… で認証
                                           ▼
                            pg_try_advisory_lock（同時実行を1本に）
                                           │
                                           ▼
                            refreshTokenEnc を復号 ──▶ アクセストークン
                            （TOKEN_ENCRYPTION_KEY）
                                           │
                                           ▼
                            history.list（historyId 起点で差分）
                                           │
                                           ▼
                            messages.get ──▶ classify_message
                                                 │
                              ┌──────────────────┼──────────────────┐
                              ▼                  ▼                  ▼
                   find_contact_page_id     score_email     insert_email_log
                   （Notion 連絡先照合）   （インシデント）  （Postgres）
                                                                    │
                                          Notion「最終メール日時」更新
                                          Slack 即時通知（high のみ）
                                          MA（web-engagement）へ通知

 毎日 02:00 UTC  gmail-watch-renewal   users.watch() を延長（7日で失効）
 毎日 03:00 UTC  gmail-sync            直近2日を総なめ（安全網）
```

### GAS 案

```
 担当者の Google アカウント（kanazawa@）
 ┌──────────────────────────────────────────────┐
 │ GAS 時間トリガー（5分ごと）                    │
 │   Gmail を検索（after:前回時刻 − 余白）        │
 │   件名・From/To・snippet・messageId を集める   │
 │   まとめて POST（X-Webhook-Secret 付き）       │
 └──────────────────────┬───────────────────────┘
                        ▼
              /api/webhooks/gmail-ingest（新設）
                        │ 共有シークレットで認証
                        ▼
              classify_message ──▶ find_contact_page_id
                        │            score_email
                        ▼
              insert_email_log（gmailMessageId 一意で重複を弾く）
                        │
                        ▼
              Notion「最終メール日時」／Slack 即時通知／MA 通知（今と同じ）
```

### 消えるもの・残るもの

| | 現行 | GAS 案 |
|---|---|---|
| 担当者ごとの OAuth リフレッシュトークン（Gmail 用） | 要る | **消える**（GAS が本人の権限で読む） |
| `TOKEN_ENCRYPTION_KEY` | 要る | Gmail 用途は消える。**Drive 連携（見積書承認）で残る** |
| `users.watch()` と 7 日ごとの延長 cron | 要る | **消える** |
| `historyId` の管理 | 要る | **消える** |
| Google Cloud の Pub/Sub トピック・購読 | 要る | **消える** |
| Pub/Sub の再送・その嵐 | 起きる | **起きない**（呼ぶ側は GAS 1 本、間隔は固定） |
| `pg_try_advisory_lock`（同時実行の鍵） | 要る | 不要（§2.6） |
| ダッシュボードの「Gmail 連携」接続画面 | 要る | 不要（GAS の設定に置き換わる） |
| 連絡先照合・向き判定・インシデント判定 | `src/gmail_sync/sync.py` | **そのまま** |
| `EmailLog` への保存・Notion 更新・各通知 | `src/gmail_sync/` | **そのまま** |
| 未返信リマインド・返信傾向分析・日次ダイジェスト | `EmailLog` を読む | **そのまま**（保存先が同じなので影響なし） |

## 2. GAS 案の具体設計

### 2.1 トリガー間隔

**5 分ごと** を初期値にする。

```
   1分   ── 最短。だが GAS のトリガーは指定時刻から数分ずれる（§4）ので
            「1分」にしても実効は 2〜5 分。実行回数だけ 5 倍になる
   5分   ── 実効 5〜10 分。1日 288 回。上限（§4）に対して十分余裕
   15分  ── 実効 15〜20 分。営業の「今メールが来た」に対して遅すぎる
```

### 2.2 Gmail の検索クエリ

`after:` を使う（`newer_than:` ではなく）。

| | `newer_than:2d` | `after:<epoch秒>` |
|---|---|---|
| 粒度 | 日単位（`h` が効くかは要確認） | 秒単位 |
| 5分ごとに読む件数 | 直近 2 日分を毎回（数百件） | 直近 2〜3 時間分（数件〜数十件） |
| 現行コードでの前例 | `list_recent_messages()`（日次の安全網） | `scripts/backfill_gmail_history.py`（過去分の取り込み） |

クエリ案（`after:` に epoch 秒を渡せることは広く使われているが、公式の表記例は日付。**要実測**）:

```
   after:<前回成功時刻 − 2時間>  -in:drafts  -in:chats
```

- **2 時間の余白** を取る理由: Gmail の検索索引は届いた直後は反映が遅れることがある
  （現行コードも同じ理由で 2 日の余白を取っている。`gmail_client._SEARCH_WINDOW_DAYS` の注記）。
  余白で同じメールを何度も送ることになるが、重複は CRM 側で弾く（§2.5）ので害はない
- 迷惑メール・ゴミ箱は検索の既定で除外される。下書きとチャットは明示的に外す
- 送信済みメールも `after:` だけの検索に含まれる（ラベルを絞らない）。
  現行の `users.watch()` は `labelIds: ["INBOX"]` で登録しており（`watch_mailbox()`）、
  送信済みは次の受信の通知に相乗りして拾われている。GAS 案では送信も受信も同じ検索で拾う

**GAS 側で何を保存するか**: 「前回成功した実行の時刻」1 つだけ（Script Properties）。
送った messageId の一覧は保存しない（重複排除は CRM 側の一意制約に任せ、GAS を状態を持たない
形に近づける）。CRM が 200 を返さなかったときは時刻を進めず、次回また同じ窓を送る。

**Gmail をどう読むか**: GAS の「高度なサービス（Advanced Gmail Service）」を使う。
これは Gmail REST API そのものなので、`messages.list` → `messages.get(format=metadata)` の
呼び方が `src/gmail_sync/gmail_client.py` の `list_messages_page()` / `get_message()` と 1:1 で
対応し、取れる項目（`id` / `threadId` / `internalDate` / `snippet` / ヘッダー）も同じになる。
簡易版の `GmailApp` は `snippet` を返さず本文から自分で切り出すことになるので使わない。

### 2.3 CRM 側に新設する取り込み API

`POST /api/webhooks/gmail-ingest`（`src/api/routes/webhooks.py` に追加。既存の Webhook と
同じく `_run_off_event_loop()` 経由で走らせ、イベントループを止めない）。

**入力**（1 回の POST に最大 50 通。50 通を超える分は GAS が次の POST に分ける）

```json
{
  "rep_email": "kanazawa@cnctor.jp",
  "dry_run": false,
  "messages": [
    {
      "id": "18f1a2b3c4d5e6f7",
      "thread_id": "18f1a2b3c4d5e6f7",
      "from": "山田太郎 <yamada@example.com>",
      "to": "金沢 <kanazawa@cnctor.jp>",
      "subject": "お見積りの件",
      "snippet": "お世話になっております。先日の…",
      "internal_date_ms": "1727150400000",
      "date_header": "Tue, 24 Sep 2026 12:00:00 +0900"
    }
  ]
}
```

この 1 通分は `gmail_client.GmailMessage`（`id, from_header, to_header, subject, date_header,
snippet, thread_id, internal_date_ms`）と同じ項目。**CRM 側は受け取った JSON を `GmailMessage`
に詰め替えるだけで、以降は現行の判定関数をそのまま呼べる**（§3）。

**`EmailLog` の列との対応**（`dashboard/prisma/schema.prisma` の `model EmailLog` で確認）

| `EmailLog` の列 | 値の出どころ |
|---|---|
| `gmailMessageId`（一意） | 入力 `id` |
| `gmailThreadId` | 入力 `thread_id` |
| `repEmail` | **サーバー側で決める**（§2.4。本文の `rep_email` は照合にだけ使う） |
| `contactPageId` / `contactEmail` | `classify_message()` → `find_contact_page_id()` の結果 |
| `direction` | `classify_message()` の結果（社外アドレスが From なら inbound、To なら outbound） |
| `subject` / `snippet` | 入力そのまま（snippet は 500 文字で切る） |
| `sentAt` | `_parse_sent_at()`（`internal_date_ms` 優先、無ければ `date_header`） |
| `incidentScore` / `incidentPriority` | `score_email()`（inbound のみ、現行と同じ） |
| `createdAt` | `now()` |
| `digestedAt` | 触らない（日次ダイジェストが使う） |

**出力**

```json
{
  "received": 12,
  "inserted": 3,
  "skipped_existing": 8,
  "skipped_unmatched": 1,
  "would_insert": 0,
  "errors": 0
}
```

- `dry_run: true` のときは判定だけして保存も通知もせず、`would_insert` に「保存するはずだった数」を
  返す。**並走期間の比較（§6）はこれで行う**
- 認証失敗は 401、形式不正（50 通超・必須項目欠落）は 400、それ以外は 200。
  GAS は 200 以外なら「前回成功時刻」を進めない

### 2.4 認証

既存の Webhook と同じ **共有シークレットをヘッダーで渡す** 方式。
`src/sync_engine/webhook_handlers/_common.py` の `verify_webhook_secret(headers, env_var)` を
そのまま使う（確認済み: ヘッダー名 `X-Webhook-Secret`、環境変数が未設定なら拒否する
fail-closed、比較は `hmac.compare_digest`）。

```
   GAS の Script Properties        CRM の環境変数
   ─────────────────────────      ─────────────────────────
   CRM_INGEST_URL                  GMAIL_INGEST_WEBHOOK_SECRET
   CRM_INGEST_SECRET      ◀────▶   GMAIL_INGEST_REP_EMAIL
                                   （このシークレットが誰のものか）
```

- **`repEmail` は本文ではなくサーバー側の対応表で決める。** 本文の `rep_email` を信じると、
  シークレットを 1 つ知っている人が他の担当者名でログを書けてしまう。担当者が増えたら
  「シークレット → 担当者」の対応を環境変数（または `RepGmailConnection` に代わる小さな表）に
  1 行ずつ足す
- 現行の Gmail Push はクエリ引数 `?token=` 方式だが、これは Pub/Sub がヘッダーを付けられない
  ための妥協（`gmail_push_webhook.py` の冒頭）。GAS の `UrlFetchApp` はヘッダーを付けられるので
  弱い方式を引き継がない
- シークレットは GAS の Script Properties に本人が入力する。コード・ログ・チャットには出さない。
  GAS プロジェクトは共有しない（Script Properties はプロジェクトの編集者全員に見える）

### 2.5 冪等性（同じメールを何度送っても 1 行にする）

`EmailLog.gmailMessageId` の一意制約を軸にする。

```
   GAS が同じメールを再送
        │
        ▼
   INSERT … ON CONFLICT ("gmailMessageId") DO NOTHING RETURNING id
        │
        ├──▶ 行が返った（新規）   ──▶ Notion 更新・通知を出す
        └──▶ 行が返らない（既存） ──▶ 何もしない
```

**通知は「実際に行が入ったとき」だけ出す**のが要点。GAS は余白のぶん同じメールを毎回送って
くるので、「存在確認 → 挿入 → 通知」を別々にやると、確認と挿入の間に別の POST が割り込んだとき
Slack 通知が 2 回出る。現行の `db.insert_email_log()` は重複で例外を投げる作りなので、
`ON CONFLICT DO NOTHING RETURNING` で「入ったかどうか」を返す関数を 1 つ足す
（`db.insert_email_logs()` に `ON CONFLICT DO NOTHING` の前例がある。ただしあちらは意図的に
インシデントスコアを付けない過去分取り込み用なので、そのままは使えない）。

### 2.6 同時実行

GAS 側は `LockService` で「前の実行がまだ走っていたら今回は何もしない」にする。
CRM 側は §2.5 の作りにしておけば、万一 2 本並んでも二重記録・二重通知にならないので、
現行の `pg_try_advisory_lock`（非 pooled 接続が要る、という癖つき）は不要になる。

### 2.7 取り込み状況の可視化

既存の Webhook は認証を通った受信を `webhook_receipts.record_webhook_receipt(source)` で
記録している（`webhooks.py` の `_record_authenticated_receipt()`）。新 API も
`receipt_source="gmail_ingest"` で同じ記録を残し、連携状況画面に「最後に GAS から届いた時刻」が
出るようにする。**30 分以上届いていなければ異常**（GAS が止まった・シークレットが変わった・
本人のアカウントで権限が切れた）。GAS 側の失敗は所有者へのメール通知しか無いので、
CRM 側で「来ていない」を検知する方が確実。

## 3. 既存コードで再利用できる部分（実際に読んで確認）

`src/gmail_sync/` は「Gmail を叩く」「判定する」「保存・通知する」がファイル単位で分かれている。

```
 ┌─ Gmail を叩く（GAS 側へ移る。CRM では使わなくなる）───────────────┐
 │  gmail_client.py                                                   │
 │    refresh_access_token()  list_recent_messages()                  │
 │    list_messages_page()  list_history()  get_message()             │
 │    watch_mailbox()                                                 │
 │  watch_registration.py                                             │
 │    register_or_renew_watch()  renew_all_watches()                  │
 │  token_crypto.py                                                   │
 │    decrypt_token()（Drive 連携では引き続き使う）                   │
 │  db.py                                                             │
 │    try_acquire_push_sync_lock()  update_history_id()               │
 │    update_watch_state()  update_watch_expiration()                 │
 └────────────────────────────────────────────────────────────────────┘

 ┌─ 判定する（そのまま再利用）────────────────────────────────────────┐
 │  sync.py                                                           │
 │    classify_message(message, rep_email, internal_domains,          │
 │                     resolve_contact)                               │
 │      → ClassifiedMessage(contact_page_id, contact_email,           │
 │                          direction, sent_at, thread_id) か None    │
 │    _extract_addresses()   From/To から社外アドレスだけ取り出す     │
 │    _parse_sent_at()       internalDate 優先で日時を決める          │
 │    internal_domains_from_env()   INTERNAL_EMAIL_DOMAINS を読む     │
 │  matcher.py                                                        │
 │    find_contact_page_id(contact_client, email)                     │
 │      Notion 連絡先を 1 件引く                                      │
 │  incident_detection/scorer.py                                      │
 │    score_email(subject, snippet) → (score, priority)               │
 └────────────────────────────────────────────────────────────────────┘

 ┌─ 保存・通知する（そのまま再利用。§2.5 の関数を 1 つ足す）─────────┐
 │  db.py                                                             │
 │    insert_email_log()  email_log_exists()  insert_email_logs()     │
 │  sync.py 内                                                        │
 │    contact_client.update_page(「最終メール日時」)                  │
 │  incident_detection/notify.py                                      │
 │    notify_managers_immediate()（high のみ）                        │
 │  notify.py                                                         │
 │    notify_web_engagement_tool()                                    │
 └────────────────────────────────────────────────────────────────────┘
```

**1 か所だけ切り出しが要る。** 現行の `sync._process_message_ref()` は
「`get_message()` で Gmail から取る → `classify_message()` → 保存 → 通知」を 1 つの関数で
やっている。GAS 案では最初の「Gmail から取る」だけが外に出るので、`GmailMessage` を受け取って
以降を行う関数（例: `record_message(message, rep_email, contact_client, internal_domains)`）
に分け、`_process_message_ref()` はそれを呼ぶだけにする。判定ロジックのコピーは作らない
（`classify_message` を切り出したときと同じ理由。過去分取り込みとの二重実装を避けるため）。

`classify_message()` の `resolve_contact` は「メールアドレス → 連絡先ページ ID」の関数を
渡す作りで、日次同期は Notion に 1 件ずつ問い合わせ、過去分取り込みは事前に作った辞書を
渡している（`scripts/backfill_gmail_history.py` で確認）。GAS 案は 1 回 50 通以下なので
日次同期と同じ「1 件ずつ Notion に聞く」でよい。同じアドレスが同じ POST に何度も出るときだけ
POST の中で結果を覚えておく。

## 4. GAS の制約と対策

数値は 2026-09-24 時点の自分の知識で書いており、**すべて要確認**（Google の Apps Script
quotas ページで実測前に見直す）。既存文書 `docs/notion_raw_blank_count_gas_plan.md` が同じ
ページを引いて「1 回 6 分、Properties は 1 値 9KB・合計 500KB」と書いているので、その 2 つは
整合している。

| 制約 | 値（要確認） | この案での使用量 | 対策 |
|---|---|---|---|
| 1 回の実行時間 | 6 分 | 数秒〜数十秒（数十通 × `messages.get` ＋ POST 1 回） | 4 分で打ち切り、残りは次回へ。1 回 50 通で POST を分ける |
| トリガーの 1 日合計実行時間 | 90 分（個人）／6 時間（Workspace） | 288 回 × 10 秒 ≒ 48 分 | 5 分間隔なら収まる。1 分間隔にすると個人アカウントでは超える恐れ |
| `UrlFetchApp` の 1 日回数 | 20,000（個人）／100,000（Workspace） | 288〜600 回 | 問題なし |
| Gmail の読み取り（GmailApp 経由） | 20,000／日（個人）、50,000／日（Workspace） | 高度なサービスは Gmail API 側の枠（1 ユーザー 250 単位／秒）で数える見込み。要確認 | 高度なサービスを使う |
| Script Properties | 1 値 9KB、合計 500KB | 時刻 1 つ | 問題なし |
| トリガーの最短間隔 | 1 分 | 5 分 | — |
| トリガーの時刻ずれ | 指定から数分遅れることがある | — | 遅延の実測値を並走で記録する（§6） |
| 同時実行数 | 1 ユーザー 30 | 1 | `LockService` で 1 本に絞る |
| 所有者の権限 | トリガーは作成者の権限で動く | — | §7 のリスク。パスワード変更・2 段階認証のリセットで止まりうる |

**メール量は未計測。** 現行コードの「直近 2 日で最大 100 件」（`_MAX_MESSAGES_PER_SYNC`）は
上限であって実測ではない。並走の最初の 1 週間で「1 日あたり何通・そのうち連絡先に一致する
のは何通」を数え、上の表の使用量を実数に置き換える。

### 複数担当者への広げ方

```
   A. 担当者ごとに GAS を持つ       B. 1 本で全員分を読む
   ───────────────────────────      ───────────────────────────
   各自が自分のアカウントで          Workspace 管理者がサービス
   GAS をコピー・承認・設定          アカウントに全社委任（DWD）
   配布と設定の手間 × 人数           管理者設定 1 回で全員分
   1 人の失効は 1 人分だけ止まる     1 本止まると全員止まる
   秘密は各自 1 つ                   読める範囲が広い秘密が 1 つ
```

- **1〜3 名のうちは A。** 配布用の手順書（GAS をコピー → 高度なサービスを有効化 →
  Script Properties に URL とシークレット → トリガーを作る）を 1 枚用意する
- **B を選ぶなら、GAS を使う理由が薄くなる。** DWD を付けたサービスアカウントは
  サーバー側（Python）から直接 Gmail API を叩けるので、§8 の「サーバー側ポーリング」で
  OAuth トークンも GAS も無しに同じことができる。GAS は「管理者権限を触らずに個人の権限で
  済ませたい」ときの道具、と割り切る
- `INTERNAL_EMAIL_DOMAINS`（社内ドメインを連絡先候補から外す）は今のまま。担当者が増えても
  変わらない

## 5. 比較表 — 現行 Push 方式 vs GAS 方式

| 観点 | 現行（Push） | GAS |
|---|---|---|
| 記録までの遅延 | 秒〜数十秒（正常時） | **5〜10 分**（5 分トリガー＋ずれ） |
| 高優先度インシデントの Slack 即時通知 | ほぼ即時 | 5〜10 分後 |
| 障害モード | ① Pub/Sub 再送の嵐（2026-09-21）② historyId 固着（2026-08-25〜26、170 回連続失敗）③ watch 失効 ④ 鍵不一致でトークン復号失敗（2026-08-15〜25、9 日間 0 件） | ① GAS トリガー停止（権限失効・上限超過）② シークレット不一致 |
| 障害の見つけ方 | ログ・連携状況画面 | 連携状況画面の「最後に届いた時刻」（§2.7） |
| 部品（面倒を見る対象） | GCP（Pub/Sub）、OAuth クライアント、暗号鍵、watch 延長 cron、日次安全網 cron、排他ロック | GAS プロジェクト、トリガー、Script Properties、取り込み API |
| 復旧手順 | 連携解除→再接続、Pub/Sub 購読の削除、historyId のクリア（過去 3 件とも手作業） | トリガーを作り直す、Script Properties を入れ直す |
| 複数担当者 | 画面から各自接続（作り済み） | 各自 GAS を配る（手順書が要る） |
| コード量（CRM 側） | 今のまま | 取り込み API 1 本を足す。Gmail を叩くコードは減らせる |
| コスト | Pub/Sub は無料枠内、Vercel の関数実行 | GAS は無料。Vercel の関数実行（回数は 288 回／日／人） |
| 本人のアカウント依存 | 連携画面で接続した本人の OAuth トークン | GAS の所有者（同じく本人） |
| Vercel のプラン依存 | Pub/Sub は関係ない | GAS 側は関係ない（§8 の代替案はプランに依存する） |

## 6. 移行手順案

```
   0. CRM に取り込み API を作る（dry_run 付き）        1〜2 日
      └ テスト: 認証・50 通上限・重複・通知が 1 回だけ
   1. GAS を kanazawa@ で作り、dry_run=true で 5 分ごとに送る
   2. 並走 2 週間（Push が正、GAS は答え合わせ）
      └ 毎日: would_insert / skipped_existing / 遅延 を記録
   3. 判断（下の基準）
   4. GAS を dry_run=false にする。Push はまだ動かしたまま 3 日
      └ 両方が書くが gmailMessageId で 1 行になる
   5. Push を止める（順番は下）
   6. 1 か月後: 日次安全網 cron と OAuth 接続画面をどうするか決める
```

**並走中の正は Push。** GAS が `dry_run` で送っている限り DB には何も書かないので、
比較だけできて事故が起きない。

### 切り替え判断の基準（2 週間の実測で）

| 基準 | 合格ライン |
|---|---|
| 取りこぼし | `would_insert` が毎回 0（Push が先に全部拾えている＝GAS も同じものを見えている） |
| 拾いすぎ | GAS が拾って Push が拾っていないものがあれば内訳を見る（送信済み・ラベル違い等）。理由が説明できること |
| 遅延 | `internalDate` から POST 到着までの中央値が 10 分以内 |
| 安定性 | GAS の実行失敗が 0、または自動で回復（次回で取り戻す） |
| 判定の一致 | `dry_run` の `skipped_unmatched` と Push 側の未一致が同じ（連絡先照合が同じ関数なので一致するはず） |

### Push を止める順番

```
   ① Pub/Sub の push 購読を削除
        ──▶ 通知が来なくなる。watch 登録は残るが無害
   ② vercel.json から gmail-watch-renewal を外す
        ──▶ 7 日で watch が自然に失効
   ③ /api/webhooks/gmail-push を残すか消すか
        ──▶ 購読が無ければ呼ばれない。急がない
   ④ 日次の gmail-sync（安全網）は 1 か月残す
        ──▶ OAuth トークンが要るので接続は解除しない
   ⑤ 1 か月後に ④・接続画面・RepGmailConnection の扱いを決める
```

①→② の順にするのは、②を先にやると watch が切れて通知が減り「GAS のおかげで拾えている」のか
「Push が死んでいるだけ」なのか分からなくなるため。

## 7. 見送る理由になりうるリスク（正直に）

| リスク | 中身 | 軽くする手 |
|---|---|---|
| 遅延 | 最短の 1 分トリガーでも実効は数分ずれる。5 分設定なら 5〜10 分。「メールが来た瞬間に Slack で知りたい」には応えられない | 高優先度インシデントだけ現行 Push を残す手もあるが、それでは Push の部品が消えず本末転倒。**遅延を飲むかどうかが最初の分岐** |
| 所有者依存 | GAS は kanazawa@ の権限で動く。パスワード変更・2 段階認証のリセット・アカウント停止で黙って止まる。異動・退職時は誰かが作り直す | §2.7 の「30 分届かなければ異常」で気づけるようにする。手順書を残す |
| 配布 | 営業担当が増えるたび、非エンジニアに GAS のコピー・承認・設定をしてもらう。承認画面（Gmail の読み取り・外部 URL への接続）で不安になる人が出る | 1〜3 名は手順書＋同席。5 名を超えるなら §8 へ |
| 無音の失敗 | GAS の実行失敗は所有者へのメール通知だけ。上限超過も同じ | CRM 側の受信記録で検知（§2.7）。GAS 側の失敗通知メールを Slack に転送する手も |
| 検索索引の遅れ | 届いた直後のメールが `after:` の検索に出ないことがある | 2 時間の余白（§2.2）。並走で「Push は拾ったが GAS が同じ窓で拾えなかった」件数を数える |
| 出口の IP | GAS の接続元 IP は Google のもので固定できない | CRM の API 側に IP 制限があれば除外が要る。**Python API 側の IP 制限の有無は未確認** |
| `after:` の epoch 秒 | 広く使われているが公式表記は日付 | 実測で確かめてから採用。ダメなら日付＋余白 1 日で `newer_than:` に戻す |
| 秘密の置き場 | Script Properties は GAS の編集者全員に見える | プロジェクトを共有しない。担当者ごとに別シークレット |

## 8. 見送った案・代替案

| 案 | 中身 | 見送った／併記する理由 |
|---|---|---|
| サーバー側ポーリング | GAS を使わず、Vercel cron を 5 分ごとに回して現行の `sync_all()`（`newer_than:2d`）を叩く | Pub/Sub・watch・historyId は消えるが、**OAuth トークンと暗号鍵は残る**（障害④は消えない）。Vercel の分単位 cron は Pro 限定で、今の Pro はトライアル（10/05 まで）。Hobby に戻すと日次しか回せず、この案は成立しない。GAS は Vercel のプランに依存しない |
| DWD＋サーバー側ポーリング | サービスアカウントにドメイン全体の委任を付け、サーバーが全員分を読む | 担当者が 5 名を超えたときの本命。管理者設定が要るので今は見送り。GAS 案の判定・保存コードはこの案でもそのまま使える（取り込み API に自分で POST する形にできる） |
| Push を直して使い続ける | 2026-09-21 に排他ロックと時間予算を入れて再送の嵐は抑えた | 抑えたのであって、部品は減っていない。障害①〜④の構造は残る。「うまく使えないの？」への答えにならない |
| GAS で判定まで行う | GAS の中で連絡先照合・インシデント判定をして結果だけ POST する | 判定ロジックが Python と GAS の 2 か所に分かれ、片方だけ直したときに静かにズレる。`classify_message` を切り出したときの方針と逆行する |

## 9. 確認した事実と、確認していないこと

**確認した（コードを読んだ）**

- 現行の経路と関数名（§1・§3）は `src/gmail_sync/sync.py` `gmail_client.py` `db.py` `matcher.py`
  `watch_registration.py` `notify.py`、`src/sync_engine/webhook_handlers/gmail_push_webhook.py`
  `_common.py`、`src/api/routes/webhooks.py` `cron.py`、`vercel.json` を 2026-09-24 に読んだもの
- `EmailLog` の列は `dashboard/prisma/schema.prisma` の `model EmailLog`（`gmailMessageId @unique`、
  `gmailThreadId String?`、`incidentScore` / `incidentPriority` / `digestedAt` を含む）
- 過去 3 件のインシデントは `docs/gmail_sync_activation_note.md`、2026-09-21 の障害は
  `gmail_client.py` と `db.py` のコメントによる
- Gmail 連携済みの担当者が kanazawa@ の 1 名であることは依頼文の前提。DB は読んでいない

**確認していない（推測・要確認）**

- §4 の GAS の上限値すべて
- `after:` に epoch 秒を渡す書き方、`newer_than:` の時間単位
- 高度なサービス経由の Gmail 読み取りがどちらの上限で数えられるか
- 1 日のメール通数（未計測）
- Python API 側の IP 制限の有無
- GAS のトリガーの実際のずれ幅（並走で実測する）
