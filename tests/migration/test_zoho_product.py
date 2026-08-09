from src.migration.zoho_product import transform_zoho_product


def test_transform_zoho_product_maps_expected_fields() -> None:
    record = {
        "データID": "zcrm_123",
        "サービス・商品名": "リピッテホテル",
        "初期費用": "29800",
        "月額費用": "8800",
    }

    result = transform_zoho_product(record)

    assert result == {
        "zoho_ID": "zcrm_123",
        "名前": "リピッテホテル",
        "課金形態": "イニシャルスポット",
        "標準初期費用": 29800.0,
        "標準月額費用": 8800.0,
    }


def test_transform_zoho_product_missing_fees_become_none() -> None:
    record = {"データID": "zcrm_456", "サービス・商品名": "メイリー"}

    result = transform_zoho_product(record)

    assert result["標準初期費用"] is None
    assert result["標準月額費用"] is None
