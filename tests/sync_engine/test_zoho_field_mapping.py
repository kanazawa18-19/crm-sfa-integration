"""src/sync_engine/zoho_field_mapping.py の単体テスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.sync_engine import zoho_field_mapping
from src.sync_engine.zoho_field_mapping import resolve_zoho_field_label


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """モジュールレベルキャッシュがテスト間で残らないようにする。"""
    zoho_field_mapping.reset_cache()
    yield
    zoho_field_mapping.reset_cache()


def _use_mapping_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "zoho_field_mapping.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(zoho_field_mapping, "DEFAULT_MAPPING_PATH", path)
    return path


def test_resolve_zoho_field_label_returns_label_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_mapping_file(monkeypatch, tmp_path, {"Deals": {"field71": "営業ステータス"}})

    assert resolve_zoho_field_label("Deals", "field71") == "営業ステータス"


def test_resolve_zoho_field_label_returns_none_for_unknown_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_mapping_file(monkeypatch, tmp_path, {"Deals": {"field71": "営業ステータス"}})

    assert resolve_zoho_field_label("Leads", "field71") is None


def test_resolve_zoho_field_label_returns_none_for_unknown_api_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_mapping_file(monkeypatch, tmp_path, {"Deals": {"field71": "営業ステータス"}})

    assert resolve_zoho_field_label("Deals", "field_unknown") is None


def test_resolve_zoho_field_label_missing_file_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(zoho_field_mapping, "DEFAULT_MAPPING_PATH", tmp_path / "does_not_exist.json")

    assert resolve_zoho_field_label("Deals", "field71") is None


def test_resolve_zoho_field_label_caches_loaded_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """一度ロードすると、reset_cache()を呼ばない限りファイルの再読込は行わない
    （config/zoho_field_mapping.jsonは実行中に変化しない静的設定という前提のキャッシュ）。"""
    path = _use_mapping_file(monkeypatch, tmp_path, {"Deals": {"field71": "営業ステータス"}})
    assert resolve_zoho_field_label("Deals", "field71") == "営業ステータス"

    path.write_text(json.dumps({"Deals": {"field71": "変更後ラベル"}}, ensure_ascii=False), encoding="utf-8")
    assert resolve_zoho_field_label("Deals", "field71") == "営業ステータス"  # まだ古い値のまま

    zoho_field_mapping.reset_cache()
    assert resolve_zoho_field_label("Deals", "field71") == "変更後ラベル"


def test_default_mapping_path_matches_repo_config() -> None:
    assert zoho_field_mapping.DEFAULT_MAPPING_PATH == (
        Path(__file__).resolve().parents[2] / "config" / "zoho_field_mapping.json"
    )


def test_resolve_zoho_field_label_reads_real_repo_config_file() -> None:
    """本番Zoho APIから取得済みの実際のconfig/zoho_field_mapping.jsonが正しく読めることを確認する
    （デフォルトのDEFAULT_MAPPING_PATHをmonkeypatchしていない状態）。"""
    assert resolve_zoho_field_label("Deals", "field71") == "営業ステータス"
    assert resolve_zoho_field_label("Deals", "field20") == "サイトコントローラー"
    assert resolve_zoho_field_label("Deals", "field_definitely_not_in_mapping") is None
    assert resolve_zoho_field_label("NotARealModule", "field71") is None
