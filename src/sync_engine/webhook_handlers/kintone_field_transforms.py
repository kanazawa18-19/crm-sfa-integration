"""kintoneフィールドコード → Notionプロパティ名 の変換テーブル（db_key別、部分更新用）。

kintone_webhook.py（kintone Webhook通知）はrecordの各フィールドを、kintoneの実
フィールドコードをキーとするdictで受け取る（kintone REST APIはフィールドを常にコード
で返すため、Zoho（api_name→ラベルの別途解決が必要）と異なりこの点の追加解決は不要）。
しかし実際のフィールドコードは、必ずしも同名のNotionプロパティに対応するとは限らず
（例: kintoneの「契約進捗状況」列 → Notionの「営業ステータス」プロパティ）、また値の
形式変換（選択肢の表記ゆれの正規化・日付のISO 8601化等）が必要なプロパティもある。
旧実装（フィールドコードをそのままNotionプロパティ名として扱う素朴な実装）のまま
kintone側のWebhookを有効化すると、ほぼ全てのプロパティが「スキーマに存在しない
プロパティ」としてDispatcherに黙ってスキップされ、kintone→Notionのリアルタイム反映が
実質機能しないまま「設定済み」に見えてしまう（2026-08-14、Zoho側で2026-08-12に発覚した
同種のBLOCKER、`zoho_field_transforms.py`参照、を教訓にkintone側のWebhook有効化前に
先回りして対応）。

この変換ロジックの「どのNotionプロパティに何の値を入れるか・どう変換するか」という判断
自体は新規に考案したものではなく、一度限りの一括移行コード
（`src/migration/project_mapping.py`/`kintone_client_master.py`/`action_mapping.py`）で
既に確定させたフィールドごとの判断を、部分更新（1フィールド単位）向けに移植したもの
（値変換の実装自体も重複させず、それらのモジュールから再利用する）。

**ただしキーとして使うkintoneフィールドコードは、CSVエクスポートの列名（＝多くの場合
フィールドの「ラベル」）とは全く別物であり、移行コード側の前提をそのまま流用できない**
（2026-08-14発覚。実際にkintone Webhookを有効化した直後、案件管理アプリの
「契約進捗状況」というラベルの実フィールドコードが`ドロップダウン_2`、「初期費用」という
ラベル文字列を含むコード`初期費用`/`初期費用_0`がそれぞれ「提案料金（イニシャル）」
「提案料金（ランニング）」という別ラベルを指す、といった食い違いが判明し、有効化した
Webhookが実質何もNotionへ反映しない状態になっていた）。以下のテーブルのキーは、
`config/.env`のkintone APIトークンで`GET /k/v1/app/form/fields.json`を実際に呼び出し
（`app`パラメータにKINTONE_APP_ID_*を指定）、返ってきた`properties[].label`と各Notion
プロパティの対応を人手で突き合わせて確定させた実際のフィールドコード（2026-08-14検証済み）。
ラベルとコードが一致しないフィールドが複数あるため、今後この対応表を変更する際は
必ずラベルではなくこの実APIレスポンスのコードを確認すること（フィールド構成が変わった
場合は同じ方法で再検証する）。

以下は意図的に対象外（`zoho_field_transforms.py`と同じ「1フィールド単位のWebhook
部分更新には不適切」という基準）:
- リレーション解決が必要なフィールドのうち、⑥アクション管理の「👨‍👩‍👧‍👦 取引先マスター」
  （kintoneフィールドコード`client_name`、自由入力の会社名テキスト）は例外
  （2026-08-25、`src/relation_sync/`によるツール間リレーション同期のリアルタイム化対応で
  下記の通り対象化した）。他のリレーション解決が必要なフィールド（担当営業/先方担当者/
  提案サービス等。migrationコードで`_`プレフィックスの内部専用キーとして扱われているもの）
  は引き続き対象外: Webhookイベント単体では関連ページIDを解決できない（migrationパッケージ
  の名寄せロジックのような重い処理を、同期的に応答する必要があるWebhookハンドラ内で行うのは
  非現実的）。「取引先マスター」だけが例外になれた理由は、Notion APIへの問い合わせではなく
  `ClientNameIndex`（取引先マスターDBの正規化済み取引先名→Notion page IDのPostgresローカル
  ミラー）へのSELECT一発で完結し、Webhookの同期応答時間内に収まるため。
  **同じ⑥アクション管理の「案件名」（案件管理DBへのリレーション）はこの例外に含めない**
  （意図的なスコープ外）: kintoneのアクション管理には案件を一意に特定できる情報が
  一切無く（`client_name`は取引先名の自由入力テキストのみで、案件そのものを絞り込める
  列が存在しない）、取引先名のような1対1に近い名寄せが成立しない。複数の案件候補から
  自動選択することはもちろん、レビューキューへ積んでも人間が判断できる材料が無く
  意味を持たないため、対応するField Transformもレビューキュー登録も行わない
  （将来、案件を特定できる列がkintone側に追加された場合は改めて検討する）。
- 派生値フィールド（①取引先マスターDBの「営業ステータス」は紐づく複数の④案件管理DB
  レコードから導出するため、単一レコードのイベントからは計算できない。
  `kintone_client_master.derive_client_sales_status`参照）。
- DBをまたぐフィールド（⑥アクション管理の「次回アクション日」は④案件管理DBの
  同名プロパティへ反映する設計だが、db_key単位のWebhookイベントでは別DBへの書き込みを
  一緒に行えない。`action_mapping.extract_next_action_date_for_project`参照）。
- kintone側の内部ID採番用キー（kintone_ID/kintone_Act_ID）: Notion書き込み対象ではない。
- チェックボックス項目（提案サービス等）: CSV由来の`parse_checkbox_columns`は
  `{prefix}[選択肢]`という複数列展開を前提にしているが、kintone REST APIの実際のレスポンス
  はCHECK_BOX型フィールドを単一キー・リスト値（`{"value": ["ホテラボ", ...]}`）で返す。
  加えてこのフィールドはリレーション解決も必要なため、上記の「リレーション解決が必要な
  フィールド」の理由でも対象外。

これらが将来必要になった場合は、既存のリレーション名寄せロジック（`src/migration/`）の
同期的な再利用を検討すること（本モジュールへ単純に足すのではなく設計を再検討する）。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from src.migration._utils import normalize_date
from src.migration.action_mapping import normalize_action_type
from src.migration.kintone_client_master import normalize_customer_type
from src.migration.project_mapping import normalize_project_status
from src.migration.zoho_client_master import normalize_prefecture
from src.relation_sync.resolve import resolve_client_master_relation

# 変換関数の戻り値は`None`が「Notion側の値を明示的にクリアする」という意味で使われている
# 既存の全エントリと共通（例: `lambda v: v or None`）。しかし
# `_resolve_client_master_for_kintone_action`だけは意味が異なり、「まだ解決できていない」
# ことを表す（この場合は取引先マスターのリレーション自体を触らずスキップしたい。誤って
# `{"relation": []}`を送ってしまうと既存のリレーションを消してしまう）。既存のNoneの意味と
# 衝突するため、このモジュール専用のセンチネル値`SKIP_FIELD`を返し、呼び出し元
# （kintone_webhook.py）側で区別する。
SKIP_FIELD = object()

# `resolve_client_master_relation()`はRelationReviewQueueへの記録用に呼び出し元のkintone
# レコードID（案件を横断するActionレコードのID）を必要とするが、このモジュールの変換関数は
# 既存の全エントリと同じ「1フィールドの値→Notion書き込み用の値」という単純な1引数
# シグネチャ（`Callable[[Any], Any]`）に統一されている。`src/audit_log/actor_context.py`が
# 「どの経路からの書き込みか」を暗黙に深く伝播させるのと同じ`contextvars`の手法を再利用し、
# kintone_webhook.py側がフィールド処理ループを開始する前に一度だけ設定する
# （`kintone_action_record_context()`参照）。
_current_kintone_action_record_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_kintone_action_record_id", default=None
)


@contextmanager
def kintone_action_record_context(record_id: str) -> Iterator[None]:
    """このwithブロック内での`_resolve_client_master_for_kintone_action()`呼び出しに、
    現在処理中のkintoneレコードID（RelationReviewQueueへの記録用）を伝播させる。"""
    token = _current_kintone_action_record_id.set(record_id)
    try:
        yield
    finally:
        _current_kintone_action_record_id.reset(token)


def _resolve_client_master_for_kintone_action(client_name: Any) -> Any:
    """kintoneの自由入力の会社名テキストを、取引先マスターDBへのリレーションへ解決する。

    解決できた場合はNotion page ID（`build_notion_property_value`のRELATION型は単一idも
    受け付ける）、解決できなかった場合（曖昧・候補なし。呼び出し先で
    RelationReviewQueueへ記録済み）は`SKIP_FIELD`を返す。

    アクション管理の`client_name`（顧客名）と、案件管理の`店舗名`（ラベル: 施設名（会社名））の
    両方から使う（2026-08-31に案件も対象化した）。書き込み先のNotionプロパティ名は
    呼び出し元の対応表が決めるため、この関数はDBごとの違いを知らない
    （アクションは「👨‍👩‍👧‍👦 取引先マスター」、案件は「取引先マスター」）。
    """
    record_id = _current_kintone_action_record_id.get()
    resolved = resolve_client_master_relation(
        str(client_name) if client_name is not None else "",
        source_tool="kintone",
        source_record_id=record_id or "unknown",
    )
    return resolved if resolved is not None else SKIP_FIELD


# 対象は transform_kintone_project() が実際にNotionプロパティへ書き込んでいるフィールドの
# うち、リレーション解決が不要なもののみ（src/migration/project_mapping.py参照）。
# キーは実フィールドコード（2026-08-14、GET /k/v1/app/form/fields.json?app=<project>で検証済み。
# コメントのラベルは検証時点の表示ラベル）。
_PROJECT_KINTONE_FIELD_TO_NOTION_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "ドロップダウン_2": ("営業ステータス", normalize_project_status),  # ラベル: 契約進捗状況
    "日付_3": ("契約日 / 予想契約日", normalize_date),  # ラベル: 課金開始予定日
    # 「月額費用」「初期費用」はPROJECT_SCHEMA上NUMBER型（build_notion_property_valueは
    # 型変換をせずそのままNotion APIへ渡すため、kintoneのNUMBER型フィールドが返す文字列を
    # そのまま渡すと{"number": "500000"}という不正な形になりNotion APIが拒否する）。
    # zoho_field_transforms.pyの同一プロパティと同じくfloat変換する（shirokuma-sec/
    # obasan-qualityレビューBLOCKER対応、2026-08-14）。
    # 紛らわしいがコード"初期費用_0"のラベルは「提案料金（ランニング）」＝月額費用側、
    # コード"初期費用"のラベルは「提案料金（イニシャル）」＝初期費用側（kintone側の
    # フィールド作成順に由来する命名で、コード文字列とラベルの対応が直感に反する）。
    "初期費用_0": ("月額費用", lambda v: float(v) if v not in (None, "") else None),  # ラベル: 提案料金（ランニング）
    "初期費用": ("初期費用", lambda v: float(v) if v not in (None, "") else None),  # ラベル: 提案料金（イニシャル）
    # 2026-08-31追加。ラベルは「施設名（会社名）」。アクション管理の`client_name`と同じく
    # `ClientNameIndex`（Postgresのローカルミラー）へのSELECT一発で解決できるため、
    # Webhookの同期応答時間内に収まる。
    # **案件名の組み立てにも同じ値を使うが、そちらは新規作成時だけの処理として
    # `new_record_builder.py`に置いている**（1フィールド→1プロパティ固定のこの表では
    # 同じフィールドから2つのプロパティへ書けないため）。
    "店舗名": ("取引先マスター", _resolve_client_master_for_kintone_action),
}

# 対象は transform_client_master() が実際にNotionプロパティへ書き込んでいるフィールドのうち、
# CSVエクスポート特有の重複列問題（担当者情報1〜3人分、remap_duplicate_contact_columns参照）に
# 関わらないもののみ（kintone REST APIのレコードはフィールドコードが一意なため、CSV固有の
# その問題はそもそも発生しない）。「営業ステータス」（derive_client_sales_statusによる派生値）
# と「本部名」（チェーンDBへのリレーション作成）は対象外（モジュールdocstring参照）。
# キーは実フィールドコード（2026-08-14、GET /k/v1/app/form/fields.json?app=<client_master>で検証済み）。
_CLIENT_MASTER_KINTONE_FIELD_TO_NOTION_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "顧客名": ("取引先名", lambda v: v),  # ラベル: 顧客名（法人・個人・施設）
    "顧客種別": ("顧客種別", normalize_customer_type),  # コード==ラベル
    "郵便番号": ("郵便番号", lambda v: v or None),  # ラベル: 〒
    # zoho_field_transforms.pyの同一プロパティと同じくnormalize_prefectureで選択肢検証する
    # （生値をそのまま渡すと、表記ゆれがあった場合にNotion側のSELECT型プロパティへ未知の
    # 選択肢が黙って新規作成されてしまう。shirokuma-secレビューWARN対応、2026-08-14）。
    "都道府県名": ("都道府県", normalize_prefecture),  # コード==ラベル
    "住所": ("住所", lambda v: v or None),  # ラベル: 住所（市区町村以下を記載）
    "TEL": ("TEL", lambda v: v or None),  # コード==ラベル
    "FAX": ("FAX", lambda v: v or None),  # コード==ラベル
}

# 対象は transform_kintone_action() が実際にNotionプロパティへ書き込んでいるフィールドのうち、
# チェックボックス解析が不要なもの、および取引先マスターリレーション（下記client_name、
# 2026-08-25追加）。キーは実フィールドコード。actionContent/commentの2件は2026-08-14、実際の
# kintone Webhook通知（本番）で確認済み（GET /k/v1/app/form/fields.json?app=<action>でも
# 再確認済み）。client_nameも同API検証済み（モジュールdocstring参照）。
_ACTION_KINTONE_FIELD_TO_NOTION_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "actionContent": ("アクション種別", normalize_action_type),  # ラベル: アクション内容
    "comment": ("履歴メモ", lambda v: v or None),  # ラベル: コメント
    # ラベル: 顧客名（法人・個人・施設）。自由入力テキストのため、ClientNameIndexでの
    # 名寄せによりNotion取引先マスターDBへのリレーションを解決する（モジュールdocstring
    # 参照）。戻り値は解決済みpage IDまたはSKIP_FIELD（未解決、プロパティ自体を書き込まない）。
    "client_name": ("👨‍👩‍👧‍👦 取引先マスター", _resolve_client_master_for_kintone_action),
}

# db_key -> (kintoneフィールドコード -> (Notionプロパティ名, 値変換関数))。
KINTONE_FIELD_TRANSFORMS: dict[str, dict[str, tuple[str, Callable[[Any], Any]]]] = {
    "project": _PROJECT_KINTONE_FIELD_TO_NOTION_FIELD,
    "client_master": _CLIENT_MASTER_KINTONE_FIELD_TO_NOTION_FIELD,
    "action": _ACTION_KINTONE_FIELD_TO_NOTION_FIELD,
}
