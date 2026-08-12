"""Zoho フィールドラベル → Notionプロパティ名 の変換テーブル（db_key別、部分更新用）。

zoho_webhook.py（Notification Webhook）は`affected_values`から得たZohoフィールド
ラベル（`src.sync_engine.zoho_field_mapping.resolve_zoho_field_label()`でapi_nameから
変換済み）を、そのままDatabaseSchemaのプロパティ名として扱っていた。しかし実際には
Zohoラベルがそのまま同名のNotionプロパティに対応するとは限らず（例:
Zohoの「ステージ」列 → Notionの「営業ステータス」プロパティ）、また値の形式変換
（日付の正規化・文字列boolのbool化・カンマ区切りの複数値分解等）が必要なプロパティも
ある（2026-08-12、実際のZoho本番編集がNotionへ反映されなかったことで発覚したBLOCKER）。

この変換ロジックは新規に考案したものではなく、一度限りの一括移行コード
`src/migration/zoho_project.py`の`transform_zoho_project()`で、金沢さんの承認を得て
フィールドごとに既に確定させたものと同一。`transform_zoho_project()`は移行CSVの
1レコード（全列揃った状態）を丸ごと変換する前提のため、Webhookの部分更新（delta、
1〜数フィールドのみ）にはそのまま使えない。そのため、同じフィールドごとの判断を
「Zohoラベル1個 → (Notionプロパティ名, 値変換関数)」という1フィールド単位で引ける形へ
移植したのが本モジュール。値変換の実装自体（`normalize_date`/`parse_multi_value`/
`_parse_bool`/`_parse_first_touch`）は重複させずzoho_project.py/_utils.pyから再利用する。

「project」（Zoho案件モジュール = Notion④案件管理DB）のみを対象とする。他モジュール
（chain/contact/client_master/product/action）はまだWebhookトラフィックが無いため未着手。
`ZOHO_LABEL_FIELD_MAPPINGS`にdb_key単位でエントリを追加していけば拡張できる構造にしてある。

なぜ「ステージ」の値を圧縮・変換せず「営業ステータス」へそのまま書き込むのか:
`transform_zoho_project()`のモジュールdocstring（2026-08-10確認）が既に記録している通り、
Notion「営業ステータス」プロパティ自体は実データで100%空欄であり、実質的なステータス情報は
Zoho「ステージ」列（契約済/失注/解約（処理済み）/返信なし等）にしか無い。金沢さんの方針
「Notionの営業ステータスをマスターにしたくない、Zohoの生の値をそのまま使いたい」により、
意図的に変換・圧縮せずZohoの生の値をそのまま書き込む。将来「もっと賢く変換すべきでは」と
直したくなるかもしれないが、これは既に確認済みの製品判断であり、バグではない。
"""

from __future__ import annotations

from typing import Any, Callable

from src.migration._utils import normalize_date, parse_multi_value
from src.migration.zoho_project import _parse_bool, _parse_first_touch

# Zohoラベル -> (Notionプロパティ名, 値変換関数)
# 対象は transform_zoho_project() が実際にNotionプロパティへ書き込んでいるフィールドのみ。
# 以下は意図的に含めない（transform_zoho_project()のdocstring参照）:
# - `_`プレフィックスの内部専用キー（_サービス名リスト/_取引先_zoho_id/_取引先_notion_page_id）
#   ： 取引先マスター等へのリレーション解決が必要で、1フィールド単位のWebhook部分更新には
#   不適切な、別の複雑な処理のため対象外。
# - `zoho_ID`: Notionのプロパティ書き込み対象ではない。
# - `確度`: NotionのA/B/C/D選択肢とZoho側の0〜100パーセント値で尺度が異なり、機械的な
#   変換対応表が無い。
# - `例外スイッチ`/`ショット`: 対応するZoho列が実データと意味が一致しない。
# - FORMULA/ROLLUP型のNotionプロパティ（粗利/個人粗利/契約スピード/失注経過日数（日）/
#   初期フィー/フィー率/経過日数/予算組のタイミング/アクション日/決算月/チェーン本社/
#   アクションログ）: Notion側で自動計算される読み取り専用プロパティで、書き込もうとすると
#   確実に失敗する。
_PROJECT_ZOHO_LABEL_TO_NOTION_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    # Zohoラベル == Notionプロパティ名だが、値変換が必要なもの（"同名だから変換不要"ではない）。
    "案件名": ("案件名", lambda v: v),
    "初期費用": ("初期費用", lambda v: float(v) if v not in (None, "") else None),
    "月額費用": ("月額費用", lambda v: float(v) if v not in (None, "") else None),
    "メモ": ("メモ", lambda v: v or None),
    "サイトコントローラー": ("サイトコントローラー", parse_multi_value),
    "かつやさん": ("かつやさん", _parse_bool),
    "問合せ": ("問合せ", _parse_bool),
    "ネックポイント": ("ネックポイント", lambda v: v or None),
    "失注理由": ("失注理由", lambda v: v or None),
    "失注日": ("失注日", normalize_date),
    "メールアドレス": ("メールアドレス", lambda v: v or None),
    "電話番号": ("電話番号", lambda v: v or None),
    "契約日 / 予想契約日": ("契約日 / 予想契約日", normalize_date),
    # Zohoラベル != Notionプロパティ名。
    # 「ステージ」→「営業ステータス」: Zohoの生の値をそのまま書き込む（変換しない）。
    # 上記モジュールdocstring参照。値変換関数を挟むと将来"賢い変換"を誤って追加しかねないため
    # 明示的に恒等関数にしている。
    "ステージ": ("営業ステータス", lambda v: v),
    "【Notion】テキスト": ("テキスト", lambda v: v or None),
    "【Notion】ファーストタッチ": ("ファーストタッチ", _parse_first_touch),
    "【Notion】担当者名": ("担当者名", lambda v: v or None),
    "決裁者": ("決裁者名", lambda v: v or None),
    "【Notion】次回アクション": ("次回アクション", lambda v: v or None),
    "【Notion】サービス数（施設数）": (
        "サービス数（施設数）",
        lambda v: float(v) if v not in (None, "") else None,
    ),
}

# db_key -> (Zohoラベル -> (Notionプロパティ名, 値変換関数))。
# project以外のdb_key（chain/contact/client_master/product/action）はまだWebhookトラフィックが
# 無いため未着手。追加する場合はここへエントリを増やすだけでよい。
ZOHO_LABEL_FIELD_MAPPINGS: dict[str, dict[str, tuple[str, Callable[[Any], Any]]]] = {
    "project": _PROJECT_ZOHO_LABEL_TO_NOTION_FIELD,
}
