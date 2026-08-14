# lead-researcher連携（POST /api/webhooks/lead-inquiry）

別リポジトリ`lead-researcher`（Slackに転送された問い合わせメールをClaudeで解析し、企業調査を
Slackスレッドへ返信するボット、`~/lead-researcher`）が、抽出したリード情報（会社名/名前/
メール/電話）を`POST /api/webhooks/lead-inquiry`へ送り、Notion連絡先DBへfind-or-createで
反映する（2026-08-14実装、`src/sync_engine/webhook_handlers/lead_inquiry_webhook.py`）。

`Dispatcher`/`IdMappingStore`は経由しない（`web_engagement_webhook.py`と同じ設計方針。
新規レコード作成はDispatcherのスコープ外のため）。

## 合意事項（金沢さん、2026-08-14）

1. **突合はメールアドレス優先、無ければ名前（完全一致）でフォールバック**。ただし同姓同名の
   別人誤マージを防ぐため、名前フォールバックは会社（取引先マスター）が完全一致で特定でき、
   かつ既存連絡先のリンク先と一致する場合のみ採用する。
2. **取引先マスターへのリンクは会社名の完全一致時のみ**。あいまい一致・新規作成は行わない
   （無数の重複取引先マスター作成を避けるため）。連絡先DBの`取引先マスター`は本来REQUIRED
   だが、この連携経由のレコードのみ意図的に空のまま作成されうる。
3. **既存連絡先への追記は「空欄なら埋める」方式**。上書きはしない。対象プロパティ
   （メールアドレス/携帯番号/取引先マスター）はsync_scope=ALL_TOOLSのため、Notion側Webhook
   経由でkintone/Zohoへ伝播する。メール本文からのLLM抽出は誤りうるが、このリスクは
   金沢さんに確認の上で許容している。

## 既知の制約

- 名前フォールバックで「名前は一致するが会社が食い違う」場合は別人とみなしマージしない
  （`logger.warning`で記録）。ただし会社名自体が完全一致しない表記ゆれ（例:
  「株式会社」の有無）は取引先マスターとのリンクに失敗し、結果的に名前フォールバックも
  効かず新規連絡先が作られる。表記ゆれ吸収は意図的に行っていない。
- `matched_client_master: false`は「companyが空だった」「companyはあったが一致しなかった」
  の両方を含む。後者のみログ（`logger.info`）に残る。

## 環境変数

- crm-sfa-integration側: `LEAD_RESEARCHER_WEBHOOK_SECRET`
- lead-researcher側: `CRM_SFA_LEAD_INQUIRY_WEBHOOK_URL` / `CRM_SFA_LEAD_INQUIRY_WEBHOOK_SECRET`
  （上記と対になる値）

lead-researcherは同じタイミングで、web-engagement-toolが日次プルしている
「【営業部】お問合せリード管理」Notion DBへも別途登録している（詳細は
`~/.claude/projects/-Users-cnctor/memory/project_lead_researcher.md`参照）。この連絡先DB
連携とは独立した別経路。
