"""積んだ行を作り直す（2026-09-07、outboxの後半）。

`src/sync_engine/spreadsheet_outbox.py`に積まれた「シートの行を作れなかったレコード」を
取り出し、**Notionの現在値を読み直して**行を追記する。日次のcron
（`/api/cron/spreadsheet-outbox-drain`）から呼ばれる。

■ なぜ値ではなくレコードを積んだのか

失敗時のスナップショットを貯めて後から流すと、**その間に入った新しい値を古い値で
巻き戻す**。マスターはNotionなので、作り直す瞬間にNotionを読めば、その事故が原理的に
起きない。バックフィル（`scripts/backfill_spreadsheet_rows.py`）が全件に対してやって
いることを、対象を絞って自動で回すもの、と考えるのが正しい。

■ 1件ずつ、上限と時間予算を守る

```
   Vercelの実行上限         300秒（`maxDuration`。超えると途中で殺される）
   このバッチの時間予算      既定240秒。超えたら残りは次の回へ
   1回で処理する上限        既定200件
```

滞留が多いときに全部を1回で片付けようとしない。**残っていることは`pending`の件数として
診断に出る**ので、放置には気づける（`/api/diagnostics/integrations?only=spreadsheet_outbox`）。

■ 何を「解決」と呼ぶか

```
   行を作れた                          done
   もう行があった                      done（別のワーカー・次のイベントが先に作った）
   IDマッピングがもう無い               done（レコードごと消えた。作る先が無い）
   シートへ書ける項目が1つも無い         done（作っても同期キーだけの行になる）
   Notionが読めない・書き込みが落ちた    もう一度（規定回数を超えたら failed）
   シート・Notionが構成できていない      もう一度（同上。**差し戻しにしない**）
   別のワーカーが作成中・時間予算切れ     差し戻し（試したうちに入れない）
```

`failed` は**消さない**。人が対処するために残す。
"""

from __future__ import annotations

import dataclasses
import logging
import sys
import time
from typing import Any

from src.db_schema.base import Tool
from src.db_schema.registry import ALL_SCHEMAS, get_schema
from src.sync_engine import spreadsheet_outbox
from src.sync_engine.id_mapping import IdMappingStore
from src.sync_engine.record_sync_lock import (
    RecordSyncBusy, acquire_record_sync_lock, validate_record_sync_storage,
)
from src.sync_engine.production_wiring import (
    build_id_mapping_store,
    build_notion_clients_by_db,
    build_spreadsheet_targets_by_db,
    spreadsheet_row_creation_enabled,
)
from src.sync_engine.slack_notifier import SlackNotifier, WebhookSlackNotifier
from src.sync_engine.spreadsheet_row_fields import spreadsheet_row_properties
from src.sync_engine.spreadsheet_row_lock import acquire_row_creation_lock

logger = logging.getLogger(__name__)

#: 1回のcronで処理する上限。Sheetsの書き込みQuota（100リクエスト/100秒）に張り付かせない。
DEFAULT_LIMIT = 200

#: 時間予算（秒）。Vercelの`maxDuration`は300秒で、超えると途中で殺される。
DEFAULT_BUDGET_SECONDS = 240.0

#: ロックを取れなかったときに差し戻す間隔（分）。相手が作り終わるのを待つだけなので短くてよい。
_LOCK_BUSY_RETRY_MINUTES = 10

#: 1回のcronで棚卸しする`failed`の件数。シートを1件1リクエスト読むので控えめにする。
_FAILED_SWEEP_LIMIT = 50


def drain_spreadsheet_outbox(
    *,
    limit: int = DEFAULT_LIMIT,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    store: IdMappingStore | None = None,
    notion_clients: dict[str, Any] | None = None,
    spreadsheet_targets: dict[str, Any] | None = None,
    slack_notifier: SlackNotifier | None = None,
) -> dict[str, Any]:
    """積まれた行を作り直す。戻り値はcronのレスポンスにそのまま出す集計。

    引数は全てテストのための差し替え口で、本番では何も渡さない
    （`production_wiring`の組み立て済みのものを使う）。
    """
    started = time.monotonic()

    enabled_db_keys = [
        schema.key for schema in ALL_SCHEMAS if spreadsheet_row_creation_enabled(schema.key)
    ]
    if not enabled_db_keys:
        # 行の新規作成が1つも許可されていない。積まれていても作りに行けないので、
        # `pending`のまま置く（試行回数だけ空に減らして`failed`にしない）。
        logger.info(
            "spreadsheet outbox drain: 行の新規作成が許可されたDBがありません。何もしません"
        )
        return {
            "status": "skipped",
            "reason": "row creation is not enabled for any db_key",
            "claimed": 0,
        }

    store = store if store is not None else build_id_mapping_store()
    # 共通設定の不備で全件の再試行回数を使い切らない。失敗時はキューに触れず終了する。
    validate_record_sync_storage(store)
    entries = spreadsheet_outbox.claim_due(db_keys=enabled_db_keys, limit=limit)
    notion_clients = notion_clients if notion_clients is not None else build_notion_clients_by_db()
    spreadsheet_targets = (
        spreadsheet_targets
        if spreadsheet_targets is not None
        else build_spreadsheet_targets_by_db()
    )
    slack_notifier = slack_notifier if slack_notifier is not None else WebhookSlackNotifier()

    counts = {
        "created": 0,
        "already_present": 0,
        "not_applicable": 0,
        "retry": 0,
        "gave_up": 0,
        "deferred": 0,
    }
    out_of_budget = 0

    for index, entry in enumerate(entries):
        if time.monotonic() - started > budget_seconds:
            # 予算切れ。**取り出し済みの残りは差し戻す**（試したうちに入れない）。
            out_of_budget = len(entries) - index
            for remaining in entries[index:]:
                spreadsheet_outbox.release(
                    db_key=remaining.db_key,
                    notion_key=remaining.notion_key,
                    retry_after_minutes=1,
                )
            logger.info(
                "spreadsheet outbox drain: 時間予算（%.0f秒）を使い切りました。"
                "残り%d件は次の回に回します",
                budget_seconds,
                out_of_budget,
            )
            break

        outcome = _repair_one(
            entry,
            store=store,
            notion_clients=notion_clients,
            spreadsheet_targets=spreadsheet_targets,
            slack_notifier=slack_notifier,
        )
        counts[outcome] = counts.get(outcome, 0) + 1

    # **諦めた行の棚卸し。** 人が`backfill_spreadsheet_rows.py`で作り直しても、
    # そのスクリプトはこのキューを触らないため`failed`が残り、診断が永久に赤くなる
    # （2026-09-07、おばさん指摘）。行がもうできていれば静かに閉じる。
    recovered = _sweep_failed(
        enabled_db_keys,
        store=store,
        spreadsheet_targets=spreadsheet_targets,
        budget_seconds=budget_seconds,
        started=started,
    )

    purged = spreadsheet_outbox.purge_resolved()
    result = {
        "status": "success",
        # **諦めたものが出た回は、レスポンスだけ見ても分かるようにする**
        # （2026-09-07、おばさん指摘。Slack通知は別途出るが、cronの応答が
        # 一律`success`だと「成功＝問題なし」に読めてしまう）。
        "needs_attention": counts["gave_up"] > 0,
        "claimed": len(entries),
        "out_of_budget": out_of_budget,
        "recovered_manually": recovered,
        "purged": purged,
        "enabled_db_keys": sorted(enabled_db_keys),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        **counts,
    }
    logger.info("spreadsheet outbox drain: %s", result)
    return result


def _sweep_failed(
    db_keys: list[str],
    *,
    store: IdMappingStore,
    spreadsheet_targets: dict[str, Any],
    budget_seconds: float,
    started: float,
) -> int:
    """諦めた行のうち、**もう行ができているもの**を閉じる。戻り値は閉じた件数。

    自動での再試行はしない（`failed`は「8回試して駄目だった」もの）。
    ここでやるのはシートを1件読んで確かめるだけで、**人が直したことを検知して
    赤信号を消す**のが目的。書き込みは一切しない。
    """
    closed = 0
    for entry in spreadsheet_outbox.list_failed(db_keys=db_keys, limit=_FAILED_SWEEP_LIMIT):
        if time.monotonic() - started > budget_seconds:
            break
        target = spreadsheet_targets.get(entry.db_key)
        if target is None:
            continue
        try:
            row = target.find_row_by_sync_key(entry.notion_key)
        except Exception:  # noqa: BLE001 (棚卸しの失敗で本体を止めない)
            logger.warning(
                "spreadsheet outbox drain: 諦めた行の確認に失敗しました "
                "(db_key=%r, notion_key=%r)",
                entry.db_key,
                entry.notion_key,
                exc_info=True,
            )
            continue
        if row is None:
            continue
        mapping = None
        try:
            mapping = store.get(entry.notion_key)
        except Exception:  # noqa: BLE001 (行番号の控えは無くても困らない)
            logger.warning(
                "spreadsheet outbox drain: IDマッピングを引けませんでした (notion_key=%r)",
                entry.notion_key,
                exc_info=True,
            )
        if mapping is not None:
            _remember_row(store, mapping, row)
        spreadsheet_outbox.mark_done(
            db_key=entry.db_key,
            notion_key=entry.notion_key,
            resolution=spreadsheet_outbox.RESOLUTION_RECOVERED_MANUALLY,
            note="諦めた後に行ができていた（手動のバックフィル等）",
        )
        logger.info(
            "spreadsheet outbox drain: 諦めていた行が既にできていたので閉じました "
            "(db_key=%r, notion_key=%r, row=%s)",
            entry.db_key,
            entry.notion_key,
            row,
        )
        closed += 1
    return closed


def _repair_one(entry, *, store, notion_clients, spreadsheet_targets, slack_notifier) -> str:
    try:
        with acquire_record_sync_lock(store, entry.db_key, entry.notion_key):
            return _repair_one_locked(
                entry, store=store, notion_clients=notion_clients,
                spreadsheet_targets=spreadsheet_targets, slack_notifier=slack_notifier,
            )
    except RecordSyncBusy:
        spreadsheet_outbox.release(
            db_key=entry.db_key, notion_key=entry.notion_key, retry_after_minutes=1,
        )
        return "deferred"
    except Exception:
        return _fail(entry, "レコード同期の排他または修復に失敗", slack_notifier)


def _repair_one_locked(
    entry: spreadsheet_outbox.OutboxEntry,
    *,
    store: IdMappingStore,
    notion_clients: dict[str, Any],
    spreadsheet_targets: dict[str, Any],
    slack_notifier: SlackNotifier | None,
) -> str:
    """1件を作り直す。戻り値は集計用のラベル（モジュールdocstringの表と対応する）。

    **1件の失敗で全体を止めない。** ここでの例外は全て握って、そのレコードだけを
    「もう一度」に落とす（次のcronが拾う）。
    """
    db_key = entry.db_key
    notion_key = entry.notion_key

    target = spreadsheet_targets.get(db_key)
    if target is None:
        # **構成ミスは「失敗」として数える**（2026-09-07、シロクマとGeminiが独立に指摘）。
        # 当初は差し戻し（試行回数を戻す）にしていたが、シートのタブが恒常的に
        # 未設定だと`MAX_ATTEMPTS`に永遠に到達せず、Slackも鳴らず、診断も緑のまま
        # **静かに滞留し続ける**。差し戻してよいのは「他ワーカーが作成中」「時間予算切れ」
        # のような、放っておけば数分で解ける一時的な見送りだけ。
        return _fail(entry, "シートのタブが未設定（設定を確認）", slack_notifier)

    try:
        mapping = store.get(notion_key)
    except Exception:  # noqa: BLE001 (ストア実装依存)
        logger.warning(
            "spreadsheet outbox drain: IDマッピングを引けませんでした (notion_key=%r)",
            notion_key,
            exc_info=True,
        )
        return _fail(entry, "IDマッピングの取得に失敗", slack_notifier)

    if mapping is None:
        # レコードごと消えた（Notionページの削除等）。作る先が無いので、これで解決。
        logger.info(
            "spreadsheet outbox drain: IDマッピングが無いため作り直しません "
            "(db_key=%r, notion_key=%r)",
            db_key,
            notion_key,
        )
        spreadsheet_outbox.mark_done(
            db_key=db_key,
            notion_key=notion_key,
            resolution=spreadsheet_outbox.RESOLUTION_NOT_APPLICABLE,
            note="IDマッピングが存在しない",
        )
        return "not_applicable"

    try:
        row = target.find_row_by_sync_key(notion_key)
    except Exception:  # noqa: BLE001 (Sheets APIの障害)
        return _fail(entry, "シートの同期キー検索に失敗", slack_notifier)

    if row is not None:
        # 別のワーカーか次のイベントが先に作っていた。行番号だけ記録して解決。
        _remember_row_locked(store, mapping, row)
        spreadsheet_outbox.mark_done(
            db_key=db_key,
            notion_key=notion_key,
            resolution=spreadsheet_outbox.RESOLUTION_ALREADY_PRESENT,
            note="既に行があった",
        )
        return "already_present"

    notion = notion_clients.get(db_key)
    if notion is None:
        # 上と同じ理由で、構成ミスは失敗として数える。
        return _fail(entry, "Notionクライアントが未設定（認証を確認）", slack_notifier)

    try:
        page = notion.get_page(notion_key)
    except Exception:  # noqa: BLE001 (Notion APIの障害)
        return _fail(entry, "Notionページの取得に失敗", slack_notifier)

    if page is None:
        # Notionページが無い（削除・アーカイブ）。作る元が無いので解決とする。
        logger.info(
            "spreadsheet outbox drain: Notionページが見つからないため作り直しません "
            "(db_key=%r, notion_key=%r)",
            db_key,
            notion_key,
        )
        spreadsheet_outbox.mark_done(
            db_key=db_key,
            notion_key=notion_key,
            resolution=spreadsheet_outbox.RESOLUTION_NOT_APPLICABLE,
            note="Notionページが存在しない",
        )
        return "not_applicable"

    try:
        schema = get_schema(db_key)
        values = spreadsheet_row_properties(page, schema, db_key)
    except Exception:  # noqa: BLE001 (スキーマの設定漏れ・デプロイ不整合)
        return _fail(entry, "シートへ流す項目を決められない", slack_notifier)

    if not values:
        # 書ける項目が1つも無い。作っても同期キーだけの行になるので作らない
        # （`_append_spreadsheet_row_for_created_record()`と同じ判断）。
        logger.warning(
            "spreadsheet outbox drain: シートへ書ける項目が無いため行は作りません "
            "(db_key=%r, notion_key=%r)",
            db_key,
            notion_key,
        )
        spreadsheet_outbox.mark_done(
            db_key=db_key,
            notion_key=notion_key,
            resolution=spreadsheet_outbox.RESOLUTION_NOT_APPLICABLE,
            note="シートへ書ける項目が無い",
        )
        return "not_applicable"

    try:
        # **行を作る瞬間だけ排他する**（`spreadsheet_row_lock`のdocstring参照）。
        # ここを飛ばすと、同じレコードの行をWebhook側と二重に作る。
        with acquire_row_creation_lock(db_key, notion_key) as acquired:
            if not acquired:
                # 別のワーカーが作成中。**試したうちに入れない**（混み合っただけで
                # `failed`へ落ちてしまう）。
                spreadsheet_outbox.release(
                    db_key=db_key,
                    notion_key=notion_key,
                    retry_after_minutes=_LOCK_BUSY_RETRY_MINUTES,
                )
                return "deferred"

            # ロックを取ってから、もう一度だけ探す。待っている間に相手が作り終えている。
            row = target.find_row_by_sync_key(notion_key)
            if row is not None:
                _remember_row_locked(store, mapping, row)
                spreadsheet_outbox.mark_done(
                    db_key=db_key,
                    notion_key=notion_key,
                    resolution=spreadsheet_outbox.RESOLUTION_ALREADY_PRESENT,
                    note="待っている間に行ができた",
                )
                return "already_present"

            created = target.append_row_with_sync_key(values, notion_key)
    except Exception:  # noqa: BLE001 (Sheets API・Postgres接続いずれも起こりうる)
        return _fail(entry, "行の追記に失敗", slack_notifier)

    _remember_row_locked(store, mapping, created)
    spreadsheet_outbox.mark_done(
        db_key=db_key,
        notion_key=notion_key,
        resolution=spreadsheet_outbox.RESOLUTION_CREATED,
        note=f"行を作った（{len(values)}項目）",
    )
    logger.info(
        "spreadsheet outbox drain: 行を作り直しました "
        "(db_key=%r, notion_key=%r, row=%s, 項目数=%d)",
        db_key,
        notion_key,
        created,
        len(values),
    )
    return "created"


def _remember_row(store, mapping, row_number) -> None:
    try:
        with acquire_record_sync_lock(store, mapping.db_key, mapping.notion_key):
            _remember_row_locked(store, mapping, row_number)
    except Exception:
        logger.warning("spreadsheet outbox drain: 行番号の排他記録を見送りました")


def _remember_row_locked(store: IdMappingStore, mapping: Any, row: Any) -> None:
    """行番号を`IdMapping`へ記録する。**失敗しても行の作成は取り消さない。**

    行番号はあくまで次回の読み直しを減らすための控えで、正は**シートに書かれた
    同期キー**（`Dispatcher._write_spreadsheet_value()`のdocstring）。記録に失敗しても
    同期キーから引けるため、重複行にはならない。

    ■ **保存の直前にストアを読み直す**（2026-09-07、シロクマ指摘）

    `IdMappingStore.upsert()`は全カラムを無条件に上書きする。ここで持っている
    `mapping`は`_repair_one()`の冒頭で読んだスナップショットで、その後Sheets・Notionへ
    何度もAPIを叩き、ロックまで取っている。**その間に別のWebhookが更新した
    `kintone_id`/`zoho_id`/`last_synced_at`を巻き戻す**（lost update）。

    `dispatcher.py`の`_register_spreadsheet_row()`が2026-08-31に同じ指摘で直した箇所と
    まったく同じ形。**同じ轍を踏まない。**

    行番号は`int`（`IdMapping.spreadsheet_row: int | None`）。文字列を入れると
    次回の比較・`row_matches_sync_key()`が静かに食い違う。
    """
    try:
        row_number = int(row)
    except (TypeError, ValueError):
        logger.warning(
            "spreadsheet outbox drain: 追記された行番号が数値ではありません "
            "(notion_key=%r, row=%r)",
            mapping.notion_key,
            row,
        )
        return
    try:
        latest = store.get(mapping.notion_key)
        base = latest if latest is not None else mapping
        if base.spreadsheet_row == row_number:
            return
        store.upsert(dataclasses.replace(base, spreadsheet_row=row_number))
    except Exception:  # noqa: BLE001 (記録の失敗で作り直しを失敗扱いにしない)
        logger.warning(
            "spreadsheet outbox drain: 行番号の記録に失敗しました (notion_key=%r, row=%s)",
            mapping.notion_key,
            row_number,
            exc_info=True,
        )


def _fail(
    entry: spreadsheet_outbox.OutboxEntry, error: str, slack_notifier: SlackNotifier | None
) -> str:
    """今回の試行を失敗として記録する。上限に達したらSlackで人を呼ぶ。

    **例外の全文は渡さない**（接続先やユーザー名が載る）。種類だけを残し、
    全文はサーバー側のログに置く（CLAUDE.md「認証情報をログ・エラーメッセージに出さない」）。
    """
    logger.warning(
        "spreadsheet outbox drain: %s (db_key=%r, notion_key=%r, 試行=%d)",
        error,
        entry.db_key,
        entry.notion_key,
        entry.attempts,
        # 例外の中で呼ばれたときだけスタックが付く（構成ミスの経路は例外ではない）。
        exc_info=sys.exc_info()[0] is not None,
    )
    status = spreadsheet_outbox.record_failure(
        db_key=entry.db_key, notion_key=entry.notion_key, error=error
    )
    if status != spreadsheet_outbox.STATUS_FAILED:
        return "retry"

    # **諦めた。ここは黙らない。** 自動で復旧しないものが残っていることを人へ伝える。
    logger.error(
        "spreadsheet outbox drain: %d回試して作れませんでした。**このレコードは"
        "自動では復旧しません** (db_key=%r, notion_key=%r, 最後の失敗=%s)",
        entry.attempts,
        entry.db_key,
        entry.notion_key,
        error,
    )
    if slack_notifier is not None:
        try:
            slack_notifier.notify_update_skipped(
                db_key=entry.db_key,
                source_tool=Tool.NOTION,
                external_id=entry.notion_key,
                reason="spreadsheet_row_outbox_gave_up",
                detail=(
                    f"シートの行を{entry.attempts}回作り直そうとして失敗しました"
                    f"（最後の失敗: {error}）。**自動での再試行は止めました。**"
                    "`scripts/backfill_spreadsheet_rows.py --db-key "
                    f"{entry.db_key} --apply` で作り直してください。"
                ),
            )
        except Exception:  # noqa: BLE001 (通知の失敗で全体を止めない)
            logger.warning(
                "spreadsheet outbox drain: 打ち切りのSlack通知に失敗しました", exc_info=True
            )
    return "gave_up"
