"""HttpNotionClient.get_page()が合成する予約キーの定義。

`notion_client.py`ではなくこの小さなモジュールに切り出しているのは、
`notion_client.py`が`webhook_handlers/notion_webhook.py`（`parse_notion_property_value`用）を
importしており、`notion_webhook.py`は`dispatcher.py`（`Dispatcher`/`DispatchResult`用）を
importしているため、`dispatcher.py`が`notion_client.py`を直接importすると
dispatcher → notion_client → notion_webhook → dispatcher の循環importになってしまうため。
このモジュールは他に何もimportしないため、`dispatcher.py`と`notion_client.py`の双方から
安全にimportできる。
"""

from __future__ import annotations

# get_page()の戻り値に、ページの実際の最終更新日時（Notion API生レスポンスの
# トップレベルフィールド`last_edited_time`。`page["properties"]`とは別物）を合成する際の
# キー。dispatcher.pyの05_同期・競合制御（コンフリクト判定）が「Notion側の現在値の
# 更新日時」として参照する（`resolve_conflict`が比較に使うupdated_at）。
#
# 単純に"updated_at"というキー名を使わないのは、`src/db_schema/base.py`の
# `common_internal_properties()`が"updated_at"という名前の実プロパティ（product/contact
# DBスキーマに存在、PropertyType.DATETIME）を定義しており、実際にそのプロパティが
# Notionページ上に存在する場合、`HttpNotionClient.get_page()`のループでresult["updated_at"]に
# 実プロパティ値が入ってしまい、ここで合成するページの`last_edited_time`と衝突するため。
# （他の4DBのスキーマには"updated_at"という名前の実プロパティは存在しないが、
# 衝突を確実に避けるため全DB共通でこの専用キーを使う。）
# 実プロパティ名は`src/db_schema/*.py`のPropertyDefinition.nameを見る限り
# アンダースコア始まりのものが1つも無いため、"_"始まりのキーであれば実プロパティ名との
# 衝突は起きない（`src/migration/zoho_mapping.py`の`_取引先Zoho_ID`等、内部専用キーに
# アンダースコア接頭辞を使う慣習は本コードベースの既存パターンでもある）。
NOTION_LAST_EDITED_TIME_KEY = "_notion_last_edited_time"
