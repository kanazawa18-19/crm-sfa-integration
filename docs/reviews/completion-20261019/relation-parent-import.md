# 確定済み外部親の取込漏れ復旧

`scripts/import_missing_relation_parent.py` は、関連保留の Zoho アクションが指す既存取引先を、Notion に一度だけ取り込む運用用の入口。恒常 API/UI には追加しない。顧客 ID・名称・計画は私有領域に置く。

```text
実物プレビュー → 元action/外部IDの鍵 → 全件再照合 → 耐久予約
  → create_page_once → Notion ID保存 → marker/項目実読 → 対応表
```

- 管理者、未処理の関連保留、action の field6 にある確定 ID、原本の空の親関連を照合する。元本文・日時を含むプロパティとブロック、Zoho 親実物を計画の指紋に固定する。
- 同名・部分一致の候補は Notion の全ページ検索で確認する。原文全体・空白表記を解釈した全体・日本語の連続部分すべての AND を検索し、英単語一語だけでは検索しない。保存する名称は原文と完全一致が必須。
- 既存 `HubCreationAttempt` の専用 `sourceKey=relation-parent-import:zoho:<ID>`、`target=notion` を利用する。偽の同 DB の sourceId や Zoho 作成予約は作らない。
- 通常 Zoho 通知も専用予約を認識する。Notion ID 未確定の `reserved` 中は同 DB の未登録 Notion 新規作成を保留する。ID 確定後はその Notion 原本だけが対象。対応済みの他の原本の通常更新は止めない。
- 対応成立後の取込原本は通常更新だけを許可し、Hub 新規登録から欠損 kintone を自動作成しない。
- タイムアウト、応答不明、予約確定後の障害は未作成扱いにしない。`reserved` の再実行は POST しない。確認番号で実物を調べ、別途承認された復旧判断が必要。予約を削除して再実行しない。
- `created` で対応表保存だけが失敗した場合は、元の計画で再開する。POST 前と異なり元 action の編集・関連承認・親の活動日時変更では止めない。同じ Zoho 親 ID の実在・alias なし・一意対応と、Notion の DB・active・確認番号を検証する。対応未成立なら全予定項目も照合してから対応表を保存する。対応成立後は通常同期で変更された項目を元に戻さず、marker と一意な外部 ID 対応で完了を検証する。
- 関連の承認は本スクリプトでは行わない。取込後、既存 `RelationDecisions.compare/approve` で現在の原本を再照合し、本文・日時・既存外部 ID の保持を別途確認する。

適用前に kintone 側の同じ顧客の有無も運用で実読し、候補があれば先に対応を確定する。

実行は **共通通知ガードの本番配備を確認してから** 行う。計画ファイルは認証情報を含まないが顧客原本を含むので、リポジトリ外の私有パスへ 0600 で排他保存する。

```bash
python -m scripts.import_missing_relation_parent <私有計画パス> \
  --actor-id <管理者ID> --review-id <関連保留ID> --external-id <Zoho親ID>
python -m scripts.import_missing_relation_parent <同じ私有計画パス> \
  --actor-id <管理者ID> --apply
```

初回プレビューと適用は分ける。適用時に別 ID を指定するとエラーになる。環境は既存 `load_env(False)` を使用し、鍵用の `DATABASE_URL_UNPOOLED` を必要とする。Zoho/Notion/API の例外本文を共有ログへ転記しない。
