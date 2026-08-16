"""`HttpNotionClient.create_page`/`update_page`から呼ばれる監査ログ記録の本体。

監査ログはあくまで副次的な記録であり、本来のNotion書き込み自体を失敗させてはならない。
そのため`record_notion_write()`は内部で発生した例外（DATABASE_URL未設定、DB接続失敗等）を
すべて握りつぶし、warningログのみを残す（`src/gmail_sync/sync.py`の
「副次機能は失敗してもメインを止めない」方針と同じ考え方）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from src.audit_log import db
from src.audit_log.actor_context import get_actor

logger = logging.getLogger(__name__)


def _to_jsonable(value: Any) -> Any:
    """Notionプロパティ値をJSON保存可能なシンプルな値へ変換する。

    `HttpNotionClient.get_page()`が返す値（`parse_notion_property_value()`の変換結果）は
    基本的にstr/int/float/bool/list[str]/Noneのみで既にJSON化可能だが、`create_page`/
    `update_page`に渡される「これから書き込む値」側は呼び出し元次第でdatetime/date等が
    混じりうる（例: 各種フィールド変換関数の実装次第）。生のNotion API JSON構造
    （`{"type": "rich_text", "rich_text": [...]}`のようなネスト構造）をそのまま保存しない
    という要件を満たすため、既知でない型は`str()`へフォールバックする。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return str(value)


def record_notion_write(
    *,
    db_key: str,
    notion_page_id: str,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    """1回のNotion書き込み（create_page/update_page成功後）を監査ログへ記録する。

    `action="create"`: `before`は常にNone扱いとし、`after`の全プロパティを
    `{"before": null, "after": ...}`として記録する。
    `action="update"`: `before`（更新直前にNotionから読み直した現在値。取得失敗時はNone）が
    Noneの場合、正しい差分が取れないため記録自体をスキップする（誤った内容を記録するより、
    記録が抜ける方が安全という判断）。`before`が取れた場合は`after`のキーのうち実際に値が
    変わったものだけを記録し、変化が無ければ（=実質的に何も更新されなかった）記録しない。

    本関数はモジュールdocstring通り内部の例外を全て握りつぶす（呼び出し元の`create_page`/
    `update_page`は監査ログの成否に関わらず成功として扱う）。当初は`db.insert_audit_log()`
    呼び出しのみをtry/exceptで囲んでおり、`action`の分岐（未知の`action`文字列を渡した場合の
    `ValueError`）がその外側にあったため、将来`action`の種類が増えた際の実装ミス等で
    ここが例外を送出すると、呼び出し元からは「Notion書き込み自体が失敗した」ように見えて
    しまう恐れがあった（obasan-qualityレビューWARN対応、2026-08-17）。それを避けるため、
    関数本体全体を1つのtry/exceptで囲む。
    """
    try:
        if action == "update":
            if before is None:
                logger.warning(
                    "audit_log: skipping update log for db_key=%r page_id=%s "
                    "(failed to fetch current values before update)",
                    db_key,
                    notion_page_id,
                )
                return
            changed_fields = {
                name: {"before": _to_jsonable(before.get(name)), "after": _to_jsonable(new_value)}
                for name, new_value in after.items()
                if before.get(name) != new_value
            }
            if not changed_fields:
                return
        elif action == "create":
            changed_fields = {
                name: {"before": None, "after": _to_jsonable(value)} for name, value in after.items()
            }
        else:
            raise ValueError(f"unsupported audit_log action: {action!r}")

        actor = get_actor()
        if actor.source == "unknown":
            # 新しい書き込み経路が追加されたのにset_actor()を仕込み忘れている可能性がある
            # （actor_context.pyのget_actor()docstring参照）。記録自体はactorSource="unknown"の
            # まま続行する（記録が欠けるより、経路不明とわかった状態で残る方が良いため）。
            logger.warning(
                "audit_log: recording db_key=%r page_id=%s with actorSource='unknown' "
                "(no set_actor() context was active)",
                db_key,
                notion_page_id,
            )

        db.insert_audit_log(
            db_key=db_key,
            notion_page_id=notion_page_id,
            action=action,
            changed_fields=changed_fields,
            actor_source=actor.source,
            actor_label=actor.label,
        )
    except Exception:
        logger.exception(
            "audit_log: failed to record %s for db_key=%r page_id=%s",
            action,
            db_key,
            notion_page_id,
        )
