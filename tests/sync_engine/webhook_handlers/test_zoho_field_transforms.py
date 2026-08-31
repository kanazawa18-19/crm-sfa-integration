from __future__ import annotations

import pytest

from src.db_schema.registry import get_schema
from src.sync_engine.webhook_handlers import zoho_field_transforms as module
from src.sync_engine.webhook_handlers.zoho_field_transforms import (
    ZOHO_LABEL_FIELD_MAPPINGS,
)


def test_all_mapped_notion_properties_exist_in_schema() -> None:
    # shirokuma-secレビューWARN対応（2026-08-14、kintone_field_transforms.pyの同種テストと
    # 同じ理由）: ZOHO_LABEL_FIELD_MAPPINGSのNotionプロパティ名がタイポ等で実スキーマに存在
    # しない場合、Dispatcher側のKeyErrorガードでそのフィールドだけスキップされ気づきにくい。
    for db_key, field_mapping in ZOHO_LABEL_FIELD_MAPPINGS.items():
        schema = get_schema(db_key)
        for zoho_label, (notion_property, _transform) in field_mapping.items():
            schema.get_property(notion_property)


# --- action.取引先/【Notion】取引先マスター（取引先マスターリレーション解決、2026-08-25、Round2） ---


def test_action_client_master_fields_map_to_relation_property() -> None:
    raw_name_property, raw_name_transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["取引先"]
    hint_property, hint_transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["【Notion】取引先マスター"]

    assert raw_name_property == "👨‍👩‍👧‍👦 取引先マスター"
    assert hint_property == "👨‍👩‍👧‍👦 取引先マスター"
    # 両ラベルとも同じ解決関数を指す（field22の埋め込みヒント優先、モジュールdocstring参照）。
    assert raw_name_transform is hint_transform


def test_action_client_master_field_resolves_via_relation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.sync_engine.webhook_handlers import zoho_field_transforms as module

    monkeypatch.setattr(
        module, "resolve_zoho_action_client_master_relation", lambda **kwargs: "notion-page-1"
    )
    _, transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["取引先"]

    with module.zoho_action_relation_context("77", {"field6": "テスト商事"}, None):
        assert transform("テスト商事") == "notion-page-1"


def test_action_client_master_field_returns_skip_field_when_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.sync_engine.webhook_handlers import zoho_field_transforms as module

    monkeypatch.setattr(module, "resolve_zoho_action_client_master_relation", lambda **kwargs: None)
    _, transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["取引先"]

    with module.zoho_action_relation_context("77", {"field6": "曖昧な会社名"}, None):
        assert transform("曖昧な会社名") is module.SKIP_FIELD


def test_action_client_master_field_returns_skip_field_without_context() -> None:
    """zoho_action_relation_context()を経由せずに呼ばれた場合（配線ミス等）も安全側に倒し、
    既存のリレーションを誤って上書きしないようSKIP_FIELDを返すこと。"""
    from src.sync_engine.webhook_handlers import zoho_field_transforms as module

    _, transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["取引先"]

    assert transform("テスト商事") is module.SKIP_FIELD


def test_action_client_master_field_passes_full_context_to_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.sync_engine.webhook_handlers import zoho_field_transforms as module

    calls: list[dict] = []
    monkeypatch.setattr(
        module,
        "resolve_zoho_action_client_master_relation",
        lambda **kwargs: calls.append(kwargs) or "notion-page-1",
    )
    _, transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["【Notion】取引先マスター"]
    sentinel_zoho_client = object()

    with module.zoho_action_relation_context(
        "action-record-77", {"field22": "hint", "field6": "テスト商事"}, sentinel_zoho_client
    ):
        transform("hint")

    assert calls == [
        {
            "record_id": "action-record-77",
            "changed_values": {"field22": "hint", "field6": "テスト商事"},
            "zoho_client": sentinel_zoho_client,
        }
    ]


def test_project_relation_fields_are_intentionally_excluded() -> None:
    # 案件(project)リレーションは今回もスコープ外のまま（モジュールdocstring・
    # _ACTION_ZOHO_LABEL_TO_NOTION_FIELD直前のコメント参照）。
    assert "案件名" not in ZOHO_LABEL_FIELD_MAPPINGS["action"]
    assert "案件" not in ZOHO_LABEL_FIELD_MAPPINGS["action"]


# --- アクション種別（2026-08-31、Zoho発の新規アクションが1件も作られていなかった原因） ------


class Test_アクション種別のマッピング:
    """`ZOHO_LABEL_FIELD_MAPPINGS["action"]` に「アクション種別」が無かったため、
    Zoho側にもマッピングファイル(field7→アクション種別)にも値があるのに、
    Notionプロパティへ対応付けられず必須プロパティ不足で新規作成が全件中止していた。

    Zohoとアクション履歴DBで**選択肢が一致していない**ので、正規化が要る。
        Zoho   : テレアポ / メルアポ / 訪問商談 / WEB商談 / 電話商談
        Notion : テレアポ / 訪問商談 / オンライン商談 / メール / 問い合わせメール /
                 飛び込み / 自動メール / その他
    """

    def _変換(self, value: object) -> object:
        _, transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["アクション種別"]
        return transform(value)

    def test_対応表に登録されている(self) -> None:
        assert "アクション種別" in ZOHO_LABEL_FIELD_MAPPINGS["action"]
        notion_property, _ = ZOHO_LABEL_FIELD_MAPPINGS["action"]["アクション種別"]
        assert notion_property == "アクション種別"

    @pytest.mark.parametrize(
        ("zoho値", "期待"),
        [
            ("テレアポ", "テレアポ"),
            ("訪問商談", "訪問商談"),
            # Notionに同名の選択肢が無いもの。移行時に確認済みの寄せ方に合わせる。
            ("メルアポ", "メール"),
            ("WEB商談", "オンライン商談"),
            ("電話商談", "テレアポ"),
        ],
    )
    def test_Zohoの選択肢5種がすべてNotionの選択肢へ落ちる(
        self, zoho値: str, 期待: str
    ) -> None:
        assert self._変換(zoho値) == 期待

    def test_変換結果はスキーマの選択肢に必ず含まれる(self) -> None:
        """Notionのselectに無い値を書くとAPIが弾く。実データの選択肢を全部通す。"""
        options = get_schema("action").get_property("アクション種別").options
        for zoho値 in ("テレアポ", "メルアポ", "訪問商談", "WEB商談", "電話商談"):
            assert self._変換(zoho値) in options

    @pytest.mark.parametrize("未入力", ["", "   ", "-None-", None])
    def test_未入力はNoneにして_その他_に丸めない(self, 未入力: object) -> None:
        """アクション種別は必須プロパティ。丸めると入力漏れに気づけなくなる。
        Noneのままなら missing_required_properties としてSlackへ通知され、
        Zoho側の入力を促せる（それが通知文の意図）。"""
        assert self._変換(未入力) is None


# --- 連絡先の取引先リレーション（2026-08-31） ----------------------------------------------


class Test_連絡先の取引先リレーション:
    """Zoho連絡先の「お取引先」（api_name=field25、ルックアップ項目）から、
    連絡先DBの「取引先マスター」リレーションを解決する。

    **これが無かったため、Zoho発の新規連絡先が1件も作られていなかった**
    （必須プロパティ「取引先マスター」が常に欠けて missing_required_properties で中止）。

    アクションで使っている解決関数はZoho APIを追加で叩いて関連レコードを辿るが、
    連絡先はルックアップ項目に会社名が入っているためAPIを叩かずに済む。
    """

    def _変換(self):
        _, transform = ZOHO_LABEL_FIELD_MAPPINGS["contact"]["お取引先"]
        return transform

    def test_対応表に登録されている(self) -> None:
        assert "お取引先" in ZOHO_LABEL_FIELD_MAPPINGS["contact"]
        notion_property, _ = ZOHO_LABEL_FIELD_MAPPINGS["contact"]["お取引先"]
        assert notion_property == "取引先マスター"

    def test_ルックアップの会社名で名寄せする(self, monkeypatch: pytest.MonkeyPatch) -> None:
        渡された名前: list[str] = []

        def _fake(raw_name: str, **_: object) -> str:
            渡された名前.append(raw_name)
            return "notion-page-5"

        monkeypatch.setattr(module, "resolve_client_master_relation", _fake)

        assert self._変換()({"name": "合同会社HB2", "id": "2233"}) == "notion-page-5"
        assert 渡された名前 == ["合同会社HB2"]

    def test_文字列で来ても扱える(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, "resolve_client_master_relation", lambda raw_name, **_: "p1")

        assert self._変換()("合同会社HB2") == "p1"

    def test_名寄せできなければ書き込まない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """曖昧・候補なしのまま書くと、別の取引先に紐づけてしまう。
        新規作成なら必須プロパティ不足としてSlackへ通知され、人が判断できる。"""
        monkeypatch.setattr(module, "resolve_client_master_relation", lambda raw_name, **_: None)

        # 書き込みをスキップするセンチネル（Noneは「値を明示的にクリアする」意味で
        # 既に使われているため、区別できる別の値を返す）。
        assert self._変換()({"name": "不明な会社"}) is module.SKIP_FIELD
