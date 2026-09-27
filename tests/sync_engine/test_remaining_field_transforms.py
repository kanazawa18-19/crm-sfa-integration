"""確定した変換と、全件通知の空欄保護。"""
import pytest
from src.sync_engine.webhook_handlers.zoho_field_transforms import ZOHO_LABEL_FIELD_MAPPINGS, SKIP_FIELD
from src.sync_engine.webhook_handlers.kintone_field_transforms import KINTONE_FIELD_TRANSFORMS, SKIP_FIELD as KINTONE_SKIP
from src.sync_engine.outbound_field_mapping import zoho_outbound_field_names, kintone_outbound_field_names


@pytest.mark.parametrize("value", [0, 100, 35, "50"])
def test_numeric_probability(value):
    name, convert = ZOHO_LABEL_FIELD_MAPPINGS["project"]["確度"]
    assert name == "確度（数値）"
    assert convert(value) == float(value)


@pytest.mark.parametrize("value", [-1, 101, "A", True, "NaN", "inf", {}])
def test_invalid_probability_does_not_clear(value):
    assert ZOHO_LABEL_FIELD_MAPPINGS["project"]["確度"][1](value) is SKIP_FIELD


@pytest.mark.parametrize("db,code", [("client_master", "文字列__複数行_"), ("project", "リンク"), ("project", "リンク_0")])
def test_kintone_full_record_blank_keeps_notion_value(db, code):
    convert = KINTONE_FIELD_TRANSFORMS[db][code][1]
    assert convert("") is KINTONE_SKIP
    assert convert(None) is KINTONE_SKIP


def test_probability_outbound_only_goes_to_zoho_numeric_field():
    assert zoho_outbound_field_names()["project"]["確度（数値）"] == "Probability"
    assert "確度" not in zoho_outbound_field_names()["project"]
    assert "確度（数値）" not in kintone_outbound_field_names()["project"]
