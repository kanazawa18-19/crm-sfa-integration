"""監査ログ用に、RELATION/USER型プロパティの値（Notionページ/ユーザーの生ID）を
人間が読める表示名へ解決する（obasan-qualityレビューWARN対応、2026-08-17）。

`HttpNotionClient.create_page`/`update_page`が監査ログを記録する直前にのみ使う。
解決結果は`AuditLog.changedFields`へ記録する表示用の値としてのみ使い、Notion API本体への
書き込み（`build_notion_properties()`が使う生の`properties`）には一切影響させない。

コスト面の設計判断: RELATION型の解決は対象ページ1件につき`GET /v1/pages/{id}`を1回追加で
呼ぶ（`update_page`の「変更前値取得のための追加GET」と同種のコスト）。通常1回の書き込みで
変更されるRELATIONプロパティの値は数件程度に留まるため許容範囲と判断したが、
`src/migration/migration_pipeline.py`の一括移行（最大148,000件規模）でこれを行うと
Notion APIリクエスト数が大きく膨らむため、`actorSource="migration"`の場合は解決自体を
スキップし、生のページIDのまま記録する（`resolve_display_values()`のactor_source引数）。

解決に失敗した場合（対象ページ削除済み、循環参照、`NOTION_API_KEY`のIntegrationに
「ユーザー情報の読み取り」権限が付与されていない等）は、監査ログの記録自体を諦めるのではなく
生のIDのままフォールバックする（表示上多少不親切になるだけで、監査ログとしての正確性・
可用性は損なわない。`record_notion_write()`自体の「失敗しても握りつぶす」方針と同じ考え方）。
"""

from __future__ import annotations

import logging
from typing import Any

from src.api.user_directory import NotionUserDirectory
from src.db_schema.base import PropertyType
from src.db_schema.registry import get_schema
from src.sync_engine.clients._http import HOOK_MAX_RETRIES, HOOK_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# obasan-qualityレビューWARN対応で追加した機能のためだけに、一括移行(148,000件規模)の
# Notion APIリクエスト数を実質的に増やしたくないため、このactorSourceの時だけ解決を
# スキップする(モジュールdocstring参照)。src/audit_log/actor_context.pyの文字列定数と
# 一致させる必要があるが、循環import回避のためここでは値をハードコードする。
_SKIP_RESOLUTION_FOR_ACTOR_SOURCES = frozenset({"migration"})


def _resolve_user_ids(user_ids: list[Any]) -> list[Any]:
    """`NotionUserDirectory`はワークスペース全ユーザーを`GET /v1/users`でまとめてロードして
    キャッシュするクラスだが、あえてここでは呼び出しのたびに新しいインスタンスを作る
    （モジュールレベルで使い回すと、プロセスの生存期間中ずっと初回ロード時点のユーザー一覧
    のまま固定されてしまい、後から追加されたユーザーの名前解決が永久に失敗し続ける。
    また複数リクエストを跨いだキャッシュはテスト間の状態汚染も引き起こしうる）。
    初期化・API呼び出しの失敗（`NOTION_API_KEY`のIntegrationに「ユーザー情報の読み取り」
    権限が付与されていない等）はここで握りつぶし、生のIDのままフォールバックする。

    タイムアウト・リトライ予算は`HOOK_TIMEOUT_SECONDS`/`HOOK_MAX_RETRIES`（`_http.py`、
    calendar_sync/lead_syncの副次連携フックと同じ値）を使う。監査ログの表示名解決は
    あくまで副次的な処理であり、本来のcreate_page/update_pageのレスポンスを既定値
    （タイムアウト10秒×最大3回リトライ）のまま数十秒規模で遅延させてよい理由が無いため
    （2026-08-17、実際にrequests_mockで500応答をテストした際、リトライで数秒待ってしまう
    ことに気付いて対応）。
    """
    try:
        directory = NotionUserDirectory(timeout=HOOK_TIMEOUT_SECONDS, max_retries=HOOK_MAX_RETRIES)
        return directory.resolve_many([str(uid) for uid in user_ids])
    except Exception:
        logger.warning("audit_log: failed to resolve Notion user ids to names", exc_info=True)
        return user_ids


def _resolve_relation_ids(page_ids: list[Any], *, target_db_key: str) -> list[Any]:
    """RELATION型の値（ページIDのリスト）を、参照先DBのタイトルへ解決する。

    タイムアウト・リトライ予算は`_resolve_user_ids`と同じ理由で`HOOK_TIMEOUT_SECONDS`/
    `HOOK_MAX_RETRIES`（副次連携フック向けの短い予算）を使う。
    """
    from src.sync_engine.clients.notion_client import HttpNotionClient  # 循環import回避の遅延import

    try:
        target_schema = get_schema(target_db_key)
        client = HttpNotionClient(
            target_db_key,
            target_schema.notion_database_id,
            timeout=HOOK_TIMEOUT_SECONDS,
            max_retries=HOOK_MAX_RETRIES,
        )
        title_property_name = target_schema.title_property.name
    except Exception:
        logger.warning(
            "audit_log: failed to prepare relation title resolver for target_db_key=%r",
            target_db_key,
            exc_info=True,
        )
        return page_ids

    resolved: list[Any] = []
    for page_id in page_ids:
        try:
            page = client.get_page(str(page_id))
            title = page.get(title_property_name) if page else None
            resolved.append(title if title else page_id)
        except Exception:
            logger.warning(
                "audit_log: failed to resolve relation title for page_id=%s (target_db_key=%r)",
                page_id,
                target_db_key,
                exc_info=True,
            )
            resolved.append(page_id)
    return resolved


def resolve_display_values(
    db_key: str, values: dict[str, Any], *, actor_source: str
) -> dict[str, Any]:
    """プロパティ名→値の辞書のうち、RELATION/USER型のものだけ表示名へ解決した新しい辞書を
    返す。それ以外の型はそのまま素通しする。`values`自体は書き換えず、新しい辞書を返す
    （呼び出し元がNotion APIへの実際の書き込みに使う生の値と、監査ログ表示用の値を
    完全に分離するため）。
    """
    if actor_source in _SKIP_RESOLUTION_FOR_ACTOR_SOURCES:
        return dict(values)

    try:
        schema = get_schema(db_key)
    except Exception:
        return dict(values)

    resolved: dict[str, Any] = {}
    for name, value in values.items():
        if value is None:
            resolved[name] = value
            continue
        try:
            prop_def = schema.get_property(name)
        except KeyError:
            resolved[name] = value
            continue

        if prop_def.property_type == PropertyType.USER:
            resolved[name] = _resolve_user_ids(value if isinstance(value, list) else [value])
        elif prop_def.property_type == PropertyType.RELATION:
            assert prop_def.relation_target is not None  # PropertyDefinition.__post_init__で保証
            resolved[name] = _resolve_relation_ids(
                value if isinstance(value, list) else [value],
                target_db_key=prop_def.relation_target,
            )
        else:
            resolved[name] = value
    return resolved
