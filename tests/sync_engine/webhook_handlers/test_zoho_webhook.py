from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.id_mapping import SQLiteIdMappingStore
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.webhook_handlers.zoho_webhook import handler, zoho_payload_to_sync_events

# MODULE_MAPはmodule_to_db_keyを明示的に上書きする一部テスト用（実レジストリの内容に
# 依存させないため）。moduleの値そのものは実際のZoho CRM APIモジュール名("Deals")と
# 一致させてある。config/zoho_field_mapping.json（フィールドapi_name -> ラベル変換）は
# db_key解決とは独立に、payload["module"]の値そのものを見るため、こうしておかないと
# フィールド変換テストが実際のマッピングファイルの内容と噛み合わなくなる。
MODULE_MAP = {"Deals": "project"}

# 他5db_key用のmodule_to_db_key（実際のzoho_api_moduleの値と一致させてある）。
ACTION_MODULE_MAP = {"CustomModule2": "action"}
CLIENT_MASTER_MODULE_MAP = {"Accounts": "client_master"}
CONTACT_MODULE_MAP = {"Contacts": "contact"}
PRODUCT_MODULE_MAP = {"Products": "product"}
CHAIN_MODULE_MAP = {"CustomModule3": "chain"}


DEFAULT_RECORD_ID = "4876876000000488001"
DEFAULT_SERVER_TIME_MS = 1754960400000
DEFAULT_OCCURRED_AT = datetime.fromtimestamp(DEFAULT_SERVER_TIME_MS / 1000, tz=timezone.utc)


def _payload(
    *,
    token: str | None = None,
    module: str = "Deals",
    operation: str = "update",
    ids: list[str] | None = None,
    affected_values: list[dict] | None = None,
    server_time: int | None = DEFAULT_SERVER_TIME_MS,
) -> dict:
    payload: dict = {
        "server_time": server_time,
        "module": module,
        "operation": operation,
        "ids": ids if ids is not None else [DEFAULT_RECORD_ID],
        "affected_values": (
            affected_values
            if affected_values is not None
            else [
                {
                    "record_id": DEFAULT_RECORD_ID,
                    # Stage/fieldは実際にconfig/zoho_field_mapping.jsonへ登録済みの
                    # 実在するapi_name（それぞれZohoラベル「ステージ」「初期費用」に対応。
                    # 「ステージ」はzoho_field_transforms.pyのper-fieldマッピングで
                    # Notionプロパティ「営業ステータス」へ生の値のまま変換される）。
                    "values": {"Stage": "商談中(B)", "field": 500000},
                }
            ]
        ),
        "channel_id": "1000000068001",
    }
    if server_time is None:
        del payload["server_time"]
    if token is not None:
        payload["token"] = token
    return payload


class SpyDispatcher:
    """dispatchが呼ばれたかどうかだけ記録するテスト用スタブ。"""

    def __init__(self) -> None:
        self.dispatched: list[object] = []

    def dispatch(self, event: object) -> DispatchResult:
        self.dispatched.append(event)
        return DispatchResult(skipped=False)


def test_zoho_payload_to_sync_events_builds_expected_event() -> None:
    events = zoho_payload_to_sync_events(_payload(), {}, module_to_db_key=MODULE_MAP)

    assert len(events) == 1
    event = events[0]
    assert event.source_tool is Tool.ZOHO
    assert event.db_key == "project"
    assert event.external_id == DEFAULT_RECORD_ID
    assert event.occurred_at == DEFAULT_OCCURRED_AT
    assert event.properties == {"営業ステータス": "商談中(B)", "初期費用": 500000.0}
    assert event.sync_system_id is None


def test_zoho_payload_to_sync_events_reads_sync_system_id_header() -> None:
    events = zoho_payload_to_sync_events(
        _payload(), {HEADER_NAME: "自社CRM-Engine"}, module_to_db_key=MODULE_MAP
    )

    assert events[0].sync_system_id == "自社CRM-Engine"


def test_zoho_payload_to_sync_events_unknown_module_raises() -> None:
    with pytest.raises(ValueError):
        zoho_payload_to_sync_events(_payload(), {}, module_to_db_key={})


def test_zoho_payload_to_sync_events_uses_registry_zoho_api_module_by_default() -> None:
    """BLOCKER4: 逆引きはzoho_key（表示ラベル）ではなくzoho_api_module（実際のAPI module値）で行う。
    module_to_db_keyを省略し、実際のALL_SCHEMASレジストリでの解決を確認する。
    """
    events = zoho_payload_to_sync_events(_payload(), {})

    assert events[0].db_key == "project"


def test_zoho_payload_to_sync_events_display_label_is_not_a_valid_module_by_default() -> None:
    """BLOCKER4回帰確認: zoho_key（「案件」等の日本語ラベル）では逆引きできない。"""
    payload = _payload()
    payload["module"] = "案件"  # zoho_key（表示ラベル）であり実際のAPI module値ではない

    with pytest.raises(ValueError):
        zoho_payload_to_sync_events(payload, {})


def test_zoho_payload_to_sync_events_end_to_end_with_real_registry_and_field_mapping() -> None:
    """モックのMODULE_MAPを使わず、実際のALL_SCHEMAS（db_key解決）と実際の
    config/zoho_field_mapping.json（Stage -> ステージ等のapi_name -> ラベル変換）の
    両方を通した変換チェーン全体を確認する（個々の部品だけでなく全体が噛み合っていることの確認）。
    """
    events = zoho_payload_to_sync_events(_payload(), {})

    assert events[0].db_key == "project"
    assert events[0].properties == {"営業ステータス": "商談中(B)", "初期費用": 500000.0}


def test_zoho_payload_to_sync_events_ignores_unknown_api_name_with_warning_without_blocking_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _payload()
    payload["affected_values"][0]["values"]["field_not_in_mapping_9999"] = "何かの値"

    with caplog.at_level("WARNING"):
        events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {"営業ステータス": "商談中(B)", "初期費用": 500000.0}
    assert any(
        "field_not_in_mapping_9999" in record.getMessage() for record in caplog.records
    )


def test_zoho_payload_to_sync_events_matches_affected_values_by_record_id_not_index() -> None:
    """1通知にids/affected_valuesが複数件含まれる場合、先頭を無条件に使わずrecord_idで対応する
    エントリを探す。"""
    payload = _payload(
        ids=["record-b"],
        affected_values=[
            {"record_id": "record-a", "values": {"Stage": "見込み(A)"}},
            {"record_id": "record-b", "values": {"Stage": "商談中(B)"}},
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert len(events) == 1
    assert events[0].external_id == "record-b"
    assert events[0].properties == {"営業ステータス": "商談中(B)"}


def test_zoho_payload_to_sync_events_batched_notification_converts_all_ids_not_just_first() -> None:
    """BLOCKER回帰確認（2026-08-12）: 1通知に複数idが含まれる場合、ids[0]のみではなく
    全件をそれぞれ独立したSyncEventへ変換する。これを怠ると、バッチ通知の2件目以降が
    HTTP 200のまま黙って失われる（Zohoはリトライしないため恒久的なデータ消失になる）。"""
    payload = _payload(
        ids=["record-a", "record-b", "record-c"],
        affected_values=[
            {"record_id": "record-a", "values": {"Stage": "見込み(A)"}},
            {"record_id": "record-b", "values": {"Stage": "商談中(B)"}},
            {"record_id": "record-c", "values": {"Stage": "受注(Won)", "field": 1000000}},
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert len(events) == 3
    by_id = {e.external_id: e for e in events}
    assert set(by_id) == {"record-a", "record-b", "record-c"}
    assert by_id["record-a"].properties == {"営業ステータス": "見込み(A)"}
    assert by_id["record-b"].properties == {"営業ステータス": "商談中(B)"}
    assert by_id["record-c"].properties == {"営業ステータス": "受注(Won)", "初期費用": 1000000.0}
    assert all(e.db_key == "project" for e in events)
    assert all(e.source_tool is Tool.ZOHO for e in events)
    # 通知全体共通のserver_timeを全イベントが共有する。
    assert all(e.occurred_at == DEFAULT_OCCURRED_AT for e in events)


def test_zoho_payload_to_sync_events_missing_affected_values_key_results_in_empty_properties() -> None:
    """insert/delete等、フィールド単位の変更が無い通知はaffected_valuesが空/欠落しうる。"""
    payload = _payload()
    del payload["affected_values"]
    payload["operation"] = "insert"

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {}


def test_zoho_payload_to_sync_events_empty_affected_values_results_in_empty_properties() -> None:
    payload = _payload(affected_values=[])
    payload["operation"] = "delete"

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {}


def test_zoho_payload_to_sync_events_no_matching_record_id_in_affected_values_results_in_empty_properties() -> None:
    payload = _payload(
        ids=["record-x"],
        affected_values=[{"record_id": "record-y", "values": {"Stage": "商談中(B)"}}],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {}


# --- projectのper-fieldマッピング（zoho_field_transforms.py）------------------------------
# 2026-08-12、実際のZoho本番編集（「ステージ」を「与件整理」→「口頭受注」へ変更）がHTTP 200で
# 受理されたにもかかわらずNotionページへ反映されなかったBLOCKERの回帰確認。
# 原因は「ステージ」というZohoラベルをそのまま`schema.get_property("ステージ")`のキーとして
# 扱っていたため（実際のNotionプロパティ名は「営業ステータス」）、KeyErrorで無言スキップ
# されていたこと。


def test_zoho_payload_to_sync_events_stage_field_maps_to_eigyo_status_raw_passthrough() -> None:
    """実際に本番で発生したバグの再現ケース。Stage（Zohoラベル「ステージ」）の値は
    「営業ステータス」へ変換・圧縮せず生の値のまま書き込まれる
    （zoho_field_transforms.py docstring: 金沢さんの「Zohoの生の値をそのまま使いたい」方針）。"""
    payload = _payload(
        affected_values=[{"record_id": DEFAULT_RECORD_ID, "values": {"Stage": "口頭受注"}}],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {"営業ステータス": "口頭受注"}


def test_zoho_payload_to_sync_events_renamed_field_next_action() -> None:
    """「【Notion】次回アクション」（field35）→「次回アクション」のようにZohoラベルと
    Notionプロパティ名が異なるフィールドが正しくリネームされることを確認する。"""
    payload = _payload(
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field35": "来週再訪問"}}
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {"次回アクション": "来週再訪問"}


def test_zoho_payload_to_sync_events_renamed_field_decision_maker() -> None:
    """「決裁者」（field8）→「決裁者名」のリネームを確認する。"""
    payload = _payload(
        affected_values=[{"record_id": DEFAULT_RECORD_ID, "values": {"field8": "山田部長"}}],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {"決裁者名": "山田部長"}


def test_zoho_payload_to_sync_events_date_field_is_normalized() -> None:
    """「失注日」（field2）はnormalize_date()でZohoの漢字区切り日付をISO 8601へ正規化する。"""
    payload = _payload(
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field2": "2024年5月10日"}}
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {"失注日": "2024-05-10"}


def test_zoho_payload_to_sync_events_closing_date_maps_to_same_property_as_contract_date() -> None:
    """Zoho標準フィールド「完了予定日」（Closing_Date）は、カスタムフィールド
    「契約日 / 予想契約日」（field50）と同じNotionプロパティへ同期する
    （2026-08-12、金沢さん確認済みの方針）。"""
    payload = _payload(
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"Closing_Date": "2026-08-21"}}
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {"契約日 / 予想契約日": "2026-08-21"}


def test_zoho_payload_to_sync_events_boolean_fields_are_parsed_from_string() -> None:
    """「かつやさん」（field16）「問合せ」（field48）はCHECKBOX型で、Zoho側の
    "true"/"false"文字列をbool値へ変換する（Python の bool("false") は True になるため、
    _parse_bool()による明示的な文字列比較が必要）。"""
    payload = _payload(
        affected_values=[
            {
                "record_id": DEFAULT_RECORD_ID,
                "values": {"field16": "true", "field48": "false"},
            }
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {"かつやさん": True, "問合せ": False}


def test_zoho_payload_to_sync_events_multi_value_field_is_split() -> None:
    """「サイトコントローラー」（field20）はMULTI_SELECT型でカンマ区切りの複数値がありうる
    （2026-08-11の本番移行事故: "なし, リンカーン"のようなカンマ区切りをparse_multi_value()で
    分割しないとNotion APIから拒否される）。"""
    payload = _payload(
        affected_values=[
            {
                "record_id": DEFAULT_RECORD_ID,
                "values": {"field20": "なし, リンカーン"},
            }
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {"サイトコントローラー": ["なし", "リンカーン"]}


def test_zoho_payload_to_sync_events_deliberately_excluded_field_is_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """「確度」（Probability）はNotionのA/B/C/D選択肢とZoho側の0〜100%値で尺度が異なり
    意図的にマッピングしない（transform_zoho_project()のdocstring参照）。Zohoラベルへの
    解決自体は成功するが、per-fieldマッピングに無いため警告ログを出しつつ静かにスキップされ、
    書き込まれず、クラッシュもしない。"""
    payload = _payload(
        affected_values=[{"record_id": DEFAULT_RECORD_ID, "values": {"Probability": "50"}}],
    )

    with caplog.at_level("WARNING"):
        events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {}
    assert any("Probability" in record.getMessage() for record in caplog.records)


def test_zoho_payload_to_sync_events_transform_raising_skips_only_that_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """値変換関数が例外を送出しても（例: normalize_date()へ文字列以外の壊れた値が渡り
    AttributeErrorになる場合）、当該フィールドのみスキップし、バッチ全体を落とさない
    （2026-08-12のバッチ処理修正と同じ「1件/1フィールド単位で失敗を閉じ込める」方針）。"""
    payload = _payload(
        affected_values=[
            {
                "record_id": DEFAULT_RECORD_ID,
                "values": {"field2": 12345, "field": 500000},  # field2=失注日に不正な型
            }
        ],
    )

    with caplog.at_level("WARNING"):
        events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].properties == {"初期費用": 500000.0}
    assert any("field2" in record.getMessage() for record in caplog.records)


# --- chain（CustomModule3）のper-fieldマッピング ------------------------------------------
# 2026-08-12、モジュール取り違えを調査・修正済み（zoho_field_transforms.pyのdocstring参照）。
# CustomModule3が正しいチェーンモジュールで、実際のライブAPI（config/zoho_field_mapping.json）
# のapi_name/ラベルをそのまま使う（project/action等の他db_keyと同じ書き方）。


def test_zoho_payload_to_sync_events_chain_same_name_field_passthrough() -> None:
    payload = _payload(
        module="CustomModule3",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field5": "株式会社サンプル本社"}}
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=CHAIN_MODULE_MAP)

    assert events[0].properties == {"本社": "株式会社サンプル本社"}


def test_zoho_payload_to_sync_events_chain_renamed_field_url() -> None:
    """「チェーンURL」（Zohoラベル、URL1）→「URL」（Notionプロパティ名）のリネームを確認する。"""
    payload = _payload(
        module="CustomModule3",
        affected_values=[
            {
                "record_id": DEFAULT_RECORD_ID,
                "values": {"URL1": "https://example.com/chain"},
            }
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=CHAIN_MODULE_MAP)

    assert events[0].properties == {"URL": "https://example.com/chain"}


def test_zoho_payload_to_sync_events_chain_approach_status_is_normalized() -> None:
    """「アプローチ状況」（field12）はnormalize_approach_status()でCHAIN_SCHEMAの既存選択肢と
    照合される（未知の値はNoneへフォールバック、zoho_chain.py参照）。"""
    payload = _payload(
        module="CustomModule3",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field12": "アポ確定済み"}}
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=CHAIN_MODULE_MAP)

    assert events[0].properties == {"アプローチ状況": "アポ確定済み"}


def test_zoho_payload_to_sync_events_chain_deliberately_excluded_field_is_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """「その他」（field）はCHAIN_SCHEMA上書き込み可能なTEXT型プロパティだが、
    transform_zoho_chain()が一度も書き込んでいないため対象外（zoho_field_transforms.py参照）。"""
    payload = _payload(
        module="CustomModule3",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field": "リンカーン"}}
        ],
    )

    with caplog.at_level("WARNING"):
        events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=CHAIN_MODULE_MAP)

    assert events[0].properties == {}
    assert any("label='その他'" in record.getMessage() for record in caplog.records)


# --- action（CustomModule2）のper-fieldマッピング ------------------------------------------


def test_zoho_payload_to_sync_events_action_same_name_field_passthrough() -> None:
    payload = _payload(
        module="CustomModule2",
        affected_values=[{"record_id": DEFAULT_RECORD_ID, "values": {"field": "架電済み"}}],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=ACTION_MODULE_MAP)

    assert events[0].properties == {"履歴メモ": "架電済み"}


def test_zoho_payload_to_sync_events_action_renamed_field_action_name_to_title() -> None:
    """「アクション名」（Zohoラベル、Name）→「商談回数・電話回数・メール回数（何回目）」
    （ACTION_SCHEMAのtitleプロパティ）のリネームを確認する。"""
    payload = _payload(
        module="CustomModule2",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"Name": "【電話】4回目"}}
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=ACTION_MODULE_MAP)

    assert events[0].properties == {"商談回数・電話回数・メール回数（何回目）": "【電話】4回目"}


def test_zoho_payload_to_sync_events_action_date_field_is_normalized() -> None:
    payload = _payload(
        module="CustomModule2",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field4": "2024年5月10日"}}
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=ACTION_MODULE_MAP)

    assert events[0].properties == {"アクション日": "2024-05-10"}


def test_zoho_payload_to_sync_events_action_deliberately_excluded_field_is_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """「アクション種別」（field7）はACTION_SCHEMA上書き込み可能なSELECT型だが、
    transform_zoho_action()ではこの列を直接読まず「アクション名」から間接的に算出しており、
    1ラベル→1プロパティ固定の本テーブルでは同時に2プロパティへ書き込めないため対象外
    （zoho_field_transforms.py参照）。"""
    payload = _payload(
        module="CustomModule2",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field7": "テレアポ"}}
        ],
    )

    with caplog.at_level("WARNING"):
        events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=ACTION_MODULE_MAP)

    assert events[0].properties == {}
    assert any("field7" in record.getMessage() for record in caplog.records)


# --- client_master（Accounts）のper-fieldマッピング -----------------------------------------


def test_zoho_payload_to_sync_events_client_master_same_name_field_passthrough() -> None:
    payload = _payload(
        module="Accounts",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field11": "東京都渋谷区1-1-1"}}
        ],
    )

    events = zoho_payload_to_sync_events(
        payload, {}, module_to_db_key=CLIENT_MASTER_MODULE_MAP
    )

    assert events[0].properties == {"住所": "東京都渋谷区1-1-1"}


def test_zoho_payload_to_sync_events_client_master_renamed_field_phone_to_tel() -> None:
    """「電話番号」（Zohoラベル、Phone）→「TEL」のリネームを確認する。"""
    payload = _payload(
        module="Accounts",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"Phone": "03-1234-5678"}}
        ],
    )

    events = zoho_payload_to_sync_events(
        payload, {}, module_to_db_key=CLIENT_MASTER_MODULE_MAP
    )

    assert events[0].properties == {"TEL": "03-1234-5678"}


def test_zoho_payload_to_sync_events_client_master_prefecture_is_normalized() -> None:
    """「都道府県」はnormalize_prefecture()でCLIENT_MASTER_SCHEMAの既存選択肢と照合される。"""
    payload = _payload(
        module="Accounts",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field14": "東京都"}}
        ],
    )

    events = zoho_payload_to_sync_events(
        payload, {}, module_to_db_key=CLIENT_MASTER_MODULE_MAP
    )

    assert events[0].properties == {"都道府県": "東京都"}


def test_zoho_payload_to_sync_events_client_master_deliberately_excluded_field_is_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """「施設名」（field10）はtransform_zoho_client_master()で一度も書き込まれていないため
    対象外。"""
    payload = _payload(
        module="Accounts",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field10": "サンプルホテル"}}
        ],
    )

    with caplog.at_level("WARNING"):
        events = zoho_payload_to_sync_events(
            payload, {}, module_to_db_key=CLIENT_MASTER_MODULE_MAP
        )

    assert events[0].properties == {}
    assert any("field10" in record.getMessage() for record in caplog.records)


# --- contact（Contacts）のper-fieldマッピング ------------------------------------------------
# contactはtransform_zoho_contact()自体が単純なpassthrough（normalize_date等の複雑な値変換が
# 一つも無い）ため、「値変換」カテゴリの代わりに空文字列→Noneへの変換（`v or None`）を確認する。


def test_zoho_payload_to_sync_events_contact_same_name_field_passthrough() -> None:
    payload = _payload(
        module="Contacts",
        affected_values=[{"record_id": DEFAULT_RECORD_ID, "values": {"field2": "部長"}}],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=CONTACT_MODULE_MAP)

    assert events[0].properties == {"役職": "部長"}


def test_zoho_payload_to_sync_events_contact_renamed_field_department() -> None:
    """「部署名」（Zohoラベル、field4）→「部署」のリネームを確認する。"""
    payload = _payload(
        module="Contacts",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field4": "営業部"}}
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=CONTACT_MODULE_MAP)

    assert events[0].properties == {"部署": "営業部"}


def test_zoho_payload_to_sync_events_contact_empty_string_becomes_none() -> None:
    """transform_zoho_contact()が持つ唯一の値変換らしい変換（`v or None`による空文字列の
    None化）を確認する。"""
    payload = _payload(
        module="Contacts",
        affected_values=[{"record_id": DEFAULT_RECORD_ID, "values": {"field2": ""}}],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=CONTACT_MODULE_MAP)

    assert events[0].properties == {"役職": None}


def test_zoho_payload_to_sync_events_contact_deliberately_excluded_field_is_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """「名刺交換日」（field6）はCONTACT_SCHEMA上`RequirementLevel.AUTO`かつEight連携専用の
    プロパティで、今回のZoho連携では意図的に書き込まない（transform_zoho_contact()参照）。"""
    payload = _payload(
        module="Contacts",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"field6": "2026-08-01"}}
        ],
    )

    with caplog.at_level("WARNING"):
        events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=CONTACT_MODULE_MAP)

    assert events[0].properties == {}
    assert any("field6" in record.getMessage() for record in caplog.records)


# --- product（Products）のper-fieldマッピング ------------------------------------------------
# productは3フィールドとも全てZohoラベルとNotionプロパティ名が異なる（同名パススルーの
# フィールドが1つも無い）ため、「同名・変換無し」カテゴリの代わりに全フィールドがリネームで
# あることを示すテストとする。


def test_zoho_payload_to_sync_events_product_renamed_field_name_to_title() -> None:
    """「サービス・商品名」（Zohoラベル、Product_Name）→「名前」のリネームを確認する。"""
    payload = _payload(
        module="Products",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"Product_Name": "リンカーン"}}
        ],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=PRODUCT_MODULE_MAP)

    assert events[0].properties == {"名前": "リンカーン"}


def test_zoho_payload_to_sync_events_product_initial_fee_is_cast_to_float() -> None:
    """「初期費用」（field）→「標準初期費用」はリネームかつfloatキャストの両方が必要。"""
    payload = _payload(
        module="Products",
        affected_values=[{"record_id": DEFAULT_RECORD_ID, "values": {"field": "100000"}}],
    )

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=PRODUCT_MODULE_MAP)

    assert events[0].properties == {"標準初期費用": 100000.0}


def test_zoho_payload_to_sync_events_product_deliberately_excluded_field_is_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """「サービス・商品カテゴリー」（Product_Category）はtransform_zoho_product()が一度も
    書き込んでいないため対象外。「課金形態」もPRODUCT_SCHEMA上REQUIREDだがZoho側に対応する
    列が存在せず常に固定の既定値のため同様に対象外（zoho_field_transforms.py参照）。"""
    payload = _payload(
        module="Products",
        affected_values=[
            {"record_id": DEFAULT_RECORD_ID, "values": {"Product_Category": "宿泊"}}
        ],
    )

    with caplog.at_level("WARNING"):
        events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=PRODUCT_MODULE_MAP)

    assert events[0].properties == {}
    assert any("Product_Category" in record.getMessage() for record in caplog.records)


def test_zoho_payload_to_sync_events_converts_server_time_epoch_millis_to_utc_datetime() -> None:
    payload = _payload(server_time=1718115953625)

    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)

    assert events[0].occurred_at == datetime.fromtimestamp(1718115953625 / 1000, tz=timezone.utc)


def test_zoho_payload_to_sync_events_falls_back_to_now_when_server_time_missing() -> None:
    payload = _payload(server_time=None)

    before = datetime.now(timezone.utc)
    events = zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)
    after = datetime.now(timezone.utc)

    assert before <= events[0].occurred_at <= after


def test_zoho_payload_to_sync_events_missing_ids_raises() -> None:
    payload = _payload()
    payload["ids"] = []

    with pytest.raises(ValueError):
        zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)


def test_zoho_payload_to_sync_events_absent_ids_key_raises_same_as_empty() -> None:
    """"ids"が欠落している場合も空配列の場合と同じエラーになる（どちらも「対象レコードが
    特定できない」という同じ結果のため、区別してレスポンスを変える意味が無い）。"""
    payload = _payload()
    del payload["ids"]

    with pytest.raises(ValueError, match="no ids"):
        zoho_payload_to_sync_events(payload, {}, module_to_db_key=MODULE_MAP)


def test_handler_dispatches_to_injected_dispatcher_when_zoho_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    store = SQLiteIdMappingStore(":memory:")
    dispatcher = Dispatcher(store, {})
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None, dispatcher=dispatcher)

    store.close()
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "results": [{"external_id": DEFAULT_RECORD_ID, "skipped": True}]  # unknown_record
    }


def test_handler_dispatches_all_events_for_a_batched_notification_with_multiple_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLOCKER回帰確認（2026-08-12）: handler()は"ids"に複数件含まれる通知でも、全件を
    dispatcherへ渡す（先頭のみで打ち切らない）。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    spy = SpyDispatcher()
    payload = _payload(
        ids=["record-a", "record-b"],
        affected_values=[
            {"record_id": "record-a", "values": {"Stage": "見込み(A)"}},
            {"record_id": "record-b", "values": {"Stage": "商談中(B)"}},
        ],
    )
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler(event, context=None, dispatcher=spy)  # type: ignore[arg-type]

    assert response["statusCode"] == 200
    dispatched_ids = [e.external_id for e in spy.dispatched]
    assert dispatched_ids == ["record-a", "record-b"]
    assert json.loads(response["body"]) == {
        "results": [
            {"external_id": "record-a", "skipped": False},
            {"external_id": "record-b", "skipped": False},
        ]
    }


class PartiallyFailingDispatcher:
    """バッチ内の特定external_idのみdispatch時に想定外の例外を起こすテスト用スタブ。

    それ以外のexternal_idについては正常にdispatchし、記録する
    （「1件の失敗が他の独立したレコードの処理を止めない」ことを確認するため）。
    """

    def __init__(self, failing_external_id: str) -> None:
        self._failing_external_id = failing_external_id
        self.dispatched: list[object] = []

    def dispatch(self, event: object) -> DispatchResult:
        if getattr(event, "external_id", None) == self._failing_external_id:
            raise RuntimeError("boom (simulated unexpected dispatch failure)")
        self.dispatched.append(event)
        return DispatchResult(skipped=False)


def test_handler_continues_dispatching_remaining_batch_events_after_one_unexpectedly_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """バッチ内の1イベント（record-b）で想定外の例外が起きても、他の独立したレコード
    （record-a, record-c）のdispatchは試みられる（all-or-nothingにしない）。
    Dispatcher.dispatch()はstale_eventチェックにより同一SyncEventの再dispatchが冪等なため、
    レスポンス全体としては500を返しZohoのリトライを促しても、既に成功したrecord-a/record-cの
    分が二重処理される実害は無い。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    dispatcher = PartiallyFailingDispatcher(failing_external_id="record-b")
    payload = _payload(
        ids=["record-a", "record-b", "record-c"],
        affected_values=[
            {"record_id": "record-a", "values": {"Stage": "見込み(A)"}},
            {"record_id": "record-b", "values": {"Stage": "商談中(B)"}},
            {"record_id": "record-c", "values": {"Stage": "受注(Won)"}},
        ],
    )
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler(event, context=None, dispatcher=dispatcher)  # type: ignore[arg-type]

    assert response["statusCode"] == 500
    dispatched_ids = [e.external_id for e in dispatcher.dispatched]
    assert dispatched_ids == ["record-a", "record-c"]


def test_handler_skips_entirely_when_zoho_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "False")
    spy = SpyDispatcher()
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None, dispatcher=spy)  # type: ignore[arg-type]

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": "zoho_disabled"}
    assert spy.dispatched == []


# --- BLOCKER5: 不正・欠損ペイロード時のエラーハンドリング -------------------------------


def test_handler_returns_400_for_malformed_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": "{not valid json", "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


@pytest.mark.parametrize("body", ["null", "[1, 2, 3]", "42", '"hello"', "true"])
def test_handler_returns_400_for_syntactically_valid_json_that_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """BLOCKER2回帰確認: 構文的に正しいJSONだが辞書でないbody（null/配列/数値/文字列/真偽値）は、
    verify_webhook_body_token()のpayload.get()呼び出しでAttributeErrorとして未捕捉のまま
    漏れることなく、JSONパース失敗と同様にfail-closedで400を返す。未認証でも到達可能な経路。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": body, "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_missing_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    payload = _payload()
    payload["ids"] = []
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_unknown_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": json.dumps(_payload()), "headers": {}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
            lambda: {},
        )
        response = handler(event, context=None)

    assert response["statusCode"] == 400


# --- BLOCKER7: 共有トークン検証(body内のtokenフィールド方式) --------------------------
# Zoho Notifications（watch）APIは着信リクエストへ任意のHTTPヘッダーを付与できないため、
# 他ハンドラのX-Webhook-Secretヘッダー方式ではなく、通知ペイロードbody内の"token"フィールドを
# ZOHO_WEBHOOK_SECRETと照合するverify_webhook_body_token()を使う（zoho_webhook.py参照）。


def test_handler_returns_401_when_body_token_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload(token="wrong-secret")), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_body_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_body_token_has_different_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hmac.compare_digest利用の確認: 長さの異なるtokenも（単に短絡せず）確実に拒否される。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload(token="short")), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_body_token_is_non_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """WARN6回帰確認: verify_webhook_body_token()はtokenフィールドが文字列以外（例: 数値）の
    場合もisinstanceガードによりfail-closed（401）で拒否する。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    payload = _payload()
    payload["token"] = 12345
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_secret_env_unset_and_unsigned_not_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ZOHO_WEBHOOK_SECRET未設定時はfail-closed（ALLOW_UNSIGNED_WEBHOOKSが無ければ拒否）。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.delenv("ZOHO_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    event = {"body": json.dumps(_payload(token="anything")), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_succeeds_when_secret_env_unset_and_unsigned_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.delenv("ZOHO_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 200


def test_handler_succeeds_when_body_token_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    event = {
        "body": json.dumps(_payload(token="correct-secret")),
        "headers": {},
    }

    response = handler(event, context=None)

    assert response["statusCode"] == 200
