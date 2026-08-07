from __future__ import annotations

import pytest

from src.document_generation.service_mapping import resolve_template_service


@pytest.mark.parametrize(
    ("project_service", "expected_template_service"),
    [
        ("リピッテ", "リピッテホテル"),
        ("メイリー", "メイリー"),
        ("ホテルラボ St＋", "ホテルラボ"),
        ("ホテルラボ St", "ホテルラボ"),
        ("ホテルラボ Ri＋", "ホテルラボ"),
        ("ホテルラボ Ri", "ホテルラボ"),
        ("ホテルラボ In", "ホテルラボ"),
        ("ホテルラボ WEBサポート", "ホテルラボ"),
        ("ホテルラボ レビュー（口コミ返信代行）", "口コミ返信"),
        ("Growth Cube（オルト（alt））", "オルト"),
        ("ノバシテ", "SNS運用"),
        ("ILCA（三密代官、HOTEL DX）", "ILCA"),
        ("LevGo（クリエイティブラボ）", "LevGo"),
        ("デザ丸", "デザ丸"),
        ("レベニューマネジメント", "ホテルラボRM"),
        ("レセプション", "レセプション"),
        ("フルスコ", "フルスコ"),
    ],
)
def test_resolve_template_service_returns_mapped_value(
    project_service: str, expected_template_service: str
) -> None:
    assert resolve_template_service(project_service) == expected_template_service


@pytest.mark.parametrize(
    "project_service",
    ["その他", "ビールオーダー", "パーソネル", "WEB制作（楽天CP・自社HP）", "未知のサービス"],
)
def test_resolve_template_service_returns_none_for_unmapped_service(project_service: str) -> None:
    assert resolve_template_service(project_service) is None
