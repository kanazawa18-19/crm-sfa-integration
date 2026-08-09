from typing import Any

from src.migration.notion_dedupe import (
    ClientMasterSnapshot,
    build_client_match_index,
    fetch_client_master_snapshots,
    match_existing_client,
)


def _title_prop(text: str) -> dict[str, Any]:
    return {"type": "title", "title": [{"plain_text": text}]}


def _rich_text_prop(text: str | None) -> dict[str, Any]:
    if text is None:
        return {"type": "rich_text", "rich_text": []}
    return {"type": "rich_text", "rich_text": [{"plain_text": text}]}


def _select_prop(name: str | None) -> dict[str, Any]:
    return {"type": "select", "select": ({"name": name} if name else None)}


class _FakeNotionClient:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def query_all_pages(self) -> list[dict[str, Any]]:
        return self._pages


def _make_page(page_id: str, title: str, postal_code: str | None = None, address: str | None = None) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "取引先名": _title_prop(title),
            "郵便番号": _rich_text_prop(postal_code),
            "都道府県": _select_prop(None),
            "住所": _rich_text_prop(address),
        },
    }


def test_fetch_client_master_snapshots_extracts_expected_fields() -> None:
    client = _FakeNotionClient(
        [_make_page("page-1", "株式会社サンプル", postal_code="100-0001", address="千代田区1-1-1")]
    )

    snapshots = fetch_client_master_snapshots(client)  # type: ignore[arg-type]

    assert snapshots == [
        ClientMasterSnapshot(
            page_id="page-1",
            title="株式会社サンプル",
            postal_code="100-0001",
            prefecture=None,
            address="千代田区1-1-1",
        )
    ]


def test_fetch_client_master_snapshots_skips_pages_without_title() -> None:
    client = _FakeNotionClient([_make_page("page-1", "")])

    snapshots = fetch_client_master_snapshots(client)  # type: ignore[arg-type]

    assert snapshots == []


def test_match_existing_client_basic_exact_match() -> None:
    index = build_client_match_index(
        [ClientMasterSnapshot(page_id="page-1", title="株式会社サンプル", postal_code=None, prefecture=None, address=None)]
    )

    result = match_existing_client("株式会社サンプル", None, index)

    assert result.matched is not None
    assert result.matched.page_id == "page-1"
    assert result.needs_review is False


def test_match_existing_client_strong_normalization_match() -> None:
    """実データ回帰確認: 全角半角・法人格表記ゆれのみが異なる場合、第二段階で一致する。"""
    index = build_client_match_index(
        [ClientMasterSnapshot(page_id="page-1", title="ストリングスホテル名古屋", postal_code=None, prefecture=None, address=None)]
    )

    result = match_existing_client("ストリングスホテル　名古屋", None, index)

    assert result.matched is not None
    assert result.matched.page_id == "page-1"
    assert result.needs_review is False


def test_match_existing_client_no_match_returns_none() -> None:
    index = build_client_match_index(
        [ClientMasterSnapshot(page_id="page-1", title="株式会社サンプル", postal_code=None, prefecture=None, address=None)]
    )

    result = match_existing_client("全く別の会社", None, index)

    assert result.matched is None
    assert result.needs_review is False


def test_match_existing_client_postal_code_conflict_flags_for_review() -> None:
    """会社名は一致するが郵便番号が明らかに食い違う場合、誤結合の可能性があるため
    自動確定せず要確認とする（2026-08-10金沢さん確認済みの方針）。"""
    index = build_client_match_index(
        [
            ClientMasterSnapshot(
                page_id="page-1", title="株式会社サンプル", postal_code="100-0001", prefecture=None, address=None
            )
        ]
    )

    result = match_existing_client("株式会社サンプル", "530-0001", index)

    assert result.matched is not None
    assert result.needs_review is True
    assert result.reason is not None


def test_match_existing_client_postal_code_formatting_differences_do_not_conflict() -> None:
    """ハイフンの有無・「〒」記号の有無等の表記ゆれは、数字だけを比較して無視する。"""
    index = build_client_match_index(
        [
            ClientMasterSnapshot(
                page_id="page-1", title="株式会社サンプル", postal_code="1000001", prefecture=None, address=None
            )
        ]
    )

    result = match_existing_client("株式会社サンプル", "〒100-0001", index)

    assert result.matched is not None
    assert result.needs_review is False


def test_match_existing_client_ambiguous_strong_match_flags_for_review() -> None:
    """第二段階の正規化で複数の既存ページにマッチしてしまう場合、どちらか自動選択せず
    要確認とする。"""
    index = build_client_match_index(
        [
            ClientMasterSnapshot(page_id="page-1", title="株式会社サンプル", postal_code=None, prefecture=None, address=None),
            ClientMasterSnapshot(page_id="page-2", title="サンプル株式会社", postal_code=None, prefecture=None, address=None),
        ]
    )

    result = match_existing_client("サンプル", None, index)

    assert result.matched is None
    assert result.needs_review is True
