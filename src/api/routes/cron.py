"""定期実行（cron）エンドポイント群（2026-08-28にsrc/api/app.pyから分割）。

いずれもスケジューラ（Vercel Cron、およびGitHub Actions）から叩かれる。パスは
`vercel.json`等の外部設定に登録済みで、変更すると呼ばれなくなる。
`tests/api/test_route_registry.py`がパスの集合と「全cronが認証依存を持つこと」を固定している。
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from src.api.auth import (
    verify_cron_secret,
    verify_document_approval_cron_secret,
    verify_email_reminder_cron_secret,
)
from src.api.dependencies import wiring_dependency
from src.api.token_encryption_healthcheck import run_token_encryption_healthcheck
from src.api.user_directory import NotionUserDirectory
from src.document_generation.approval_poll import poll_document_approvals
from src.email_reminders.reminder_check import run_reminder_check
from src.gmail_sync.sync import sync_all
from src.gmail_sync.watch_registration import (
    GmailWatchNotConfiguredError,
    renew_all_watches,
)
from src.incident_detection.notify import run_incident_digest
from src.project_mirror.sync import refresh_all_projects
from src.relation_sync.sync import refresh_all_client_names
from src.reports.batch import run_report_batch
from src.sync_engine.webhook_events import purge_old_events
from src.sync_engine.clients._http import INTERACTIVE_MAX_RATE_LIMIT_RETRIES
from src.sync_engine.clients.zoho_client import ZohoApiError
from src.sync_engine.production_wiring import ProductionSyncWiring
from src.sync_engine.zoho_watch_channel import (
    ZohoWatchChannelNotConfiguredError,
    build_zoho_client_from_env,
    renew_zoho_watch_channel,
)

import logging

logger = logging.getLogger(__name__)

router = APIRouter()


# --- 定期実行バッチ（日報・週報） -----------------------------------------------------------


@router.get("/api/cron/daily-batch", dependencies=[Depends(verify_cron_secret)])
def run_daily_batch() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、日報・週報配信バッチのエントリポイント。

    日報は毎日、週報は金曜日のみ配信する（`src.reports.batch.run_report_batch`参照）。

    あわせて、Webhookの再送を弾くためのイベントID記録を掃除する
    （`src/sync_engine/webhook_events.py`。溜め続けないため、2026-09-01）。
    掃除に失敗しても日報の配信は止めない。
    """
    result = run_report_batch()
    try:
        result = {**result, "purged_webhook_events": purge_old_events()}
    except Exception:  # noqa: BLE001 (掃除の失敗で日報を止めない)
        logger.warning("Webhookイベント記録の掃除に失敗しました", exc_info=True)
    return result


@router.get("/api/cron/token-encryption-healthcheck", dependencies=[Depends(verify_cron_secret)])
def run_token_encryption_healthcheck_cron() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、`TOKEN_ENCRYPTION_KEY`の自己診断エントリポイント
    (2026-08-18)。詳細は`src/api/token_encryption_healthcheck.py`のモジュールdocstring参照。
    """
    return run_token_encryption_healthcheck()


@router.get("/api/cron/gmail-sync", dependencies=[Depends(verify_cron_secret)])
def run_gmail_sync() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、Gmail連携(src/gmail_sync/)の同期エントリポイント。

    Gmail連携済みの営業担当ごとに直近のメールをポーリングし、連絡先DBとメアド一致した
    ものだけをEmailLogへ記録・Notion連絡先ページの「最終メール日時」を更新する。
    対応するweb-engagement-tool側のLeadがあれば、あわせてWebhookで通知する
    (`src/gmail_sync/notify.py`、未設定なら通知はスキップされ同期処理自体は継続する)。
    """
    return sync_all()


@router.get("/api/cron/gmail-watch-renewal", dependencies=[Depends(verify_cron_secret)])
def run_gmail_watch_renewal() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、Gmail Push通知(`users.watch()`)の自動延長エントリ
    ポイント(2026-08-16)。

    Gmailのwatchは登録・延長時点から最大7日で失効し、放置すると`/api/webhooks/gmail-push`
    への通知が無音で止まる(この場合も日次の`sync_all()`セーフティネットが拾うため即座に
    データが失われるわけではないが、リアルタイム性が失われる)。`renew_all_watches()`は
    失効が近い(残り2日以内)/未登録の担当者だけを対象に登録・延長する。

    Pub/Subトピック(`GMAIL_PUBSUB_TOPIC_NAME`)が未設定の場合は、成功したように見える
    no-opにせず明確な500エラーとして表面化させる(`renew_zoho_watch_channel()`と同じ方針)。
    """
    try:
        return renew_all_watches()
    except GmailWatchNotConfiguredError as exc:
        logger.error("gmail watch renewal failed (not configured): %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/cron/incident-digest", dependencies=[Depends(verify_cron_secret)])
def run_incident_digest_cron() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、インシデント・アクシデント検知
    (`src/incident_detection/`)の中優先度日次ダイジェスト配信エントリポイント(2026-08-16)。

    高優先度(スコア8点以上)は`src/gmail_sync/sync.py`側で受信メール記録時に即座に
    Slack通知される。このcronは中優先度(4〜7点)を直近24時間分まとめて1通で配信する。
    """
    return run_incident_digest()


@router.get("/api/cron/email-reminder-check", dependencies=[Depends(verify_email_reminder_cron_secret)])
def run_email_reminder_check() -> dict[str, Any]:
    """GitHub Actionsのscheduled workflow(`.github/workflows/email-reminder-check.yml`、
    1時間おき)から呼ばれる、未返信メールリマインド(`src/email_reminders/`)のエントリ
    ポイント(2026-08-16)。

    Vercel Hobbyプランのcron制約(1日1回まで)では1時間おきの実行が組めないため、
    `vercel.json`には登録せず、GitHub Actions側から専用シークレット
    (`EMAIL_REMINDER_CRON_SECRET`、GitHub Secrets側と対になる値)付きで直接叩く方式にする。
    既存の`CRON_SECRET`(Vercel Cron専用に運用中の値)とは意図的に分離している
    (`verify_email_reminder_cron_secret`参照)。
    """
    return run_reminder_check()


@router.get(
    "/api/cron/document-approval-poll",
    dependencies=[Depends(verify_document_approval_cron_secret)],
)
def run_document_approval_poll() -> dict[str, Any]:
    """GitHub Actionsのscheduled workflow(`.github/workflows/document-approval-poll.yml`、
    1時間おき)から呼ばれる、見積書承認リクエスト(`src/document_generation/approval_poll.py`)の
    状態確定ポーリングエントリポイント(2026-08-18)。

    Drive Approvalsはpush通知を持たないため、`email-reminder-check`と同じ理由
    （Vercel Hobbyプランのcron制約(1日1回まで)では1時間おきの実行が組めない）で
    `vercel.json`には登録せず、GitHub Actions側から専用シークレット
    (`DOCUMENT_APPROVAL_CRON_SECRET`)付きで直接叩く方式にする。
    """
    return poll_document_approvals()


@router.get("/api/cron/zoho-webhook-renewal", dependencies=[Depends(verify_cron_secret)])
def run_zoho_webhook_renewal() -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、Zoho CRM Notifications（watch）チャンネルの
    自動延長（`PUT /crm/v3/actions/watch`）エントリポイント。

    Zohoのwatchチャンネルは登録・延長時点から最大1日で失効し、放置すると`/api/webhooks/zoho`
    への通知が無音で止まる（`docs/zoho_webhook_activation_note.md`参照）。Vercel Hobbyプランの
    制約でcronは1日1回しか実行できないため、`renew_zoho_watch_channel()`は毎回、Zoho上限の
    24hではなく21h先のchannel_expiryを要求し、3時間分の安全マージンを確保する
    （`expiry_days`未指定時の既定値`CRON_RENEWAL_EXPIRY_DAYS`）。対象モジュールも省略時は
    `DEFAULT_MODULES`（`Deals`/`CustomModule3`/`CustomModule2`/`Accounts`/`Contacts`/`Products`
    の6モジュール）全てを1つのwatchチャンネルでまとめて延長する。実際の延長ロジック・
    channel_idの一次情報源（環境変数`ZOHO_WATCH_CHANNEL_ID`）の設計判断は
    `src/sync_engine/zoho_watch_channel.py`の`renew_zoho_watch_channel()`を参照。

    延長対象のchannel_idが未設定、またはZoho API呼び出し自体が失敗した場合は、
    成功したように見えるno-opにせず、明確な500エラー（Vercel Cronからは失敗実行として
    検知される）として表面化させる。
    """
    try:
        client = build_zoho_client_from_env()
        result = renew_zoho_watch_channel(client, token=os.environ.get("ZOHO_WEBHOOK_SECRET"))
    except ZohoWatchChannelNotConfiguredError as exc:
        logger.error("zoho watch channel renewal failed (not configured): %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ZohoApiError as exc:
        logger.error("zoho watch channel renewal failed (zoho api error): %s", exc)
        raise HTTPException(status_code=502, detail=f"zoho api error: {exc}") from exc
    except Exception:
        # 上記2種類以外の想定外の例外（Zohoレスポンスの形が想定外だった場合の取りこぼし等）が
        # 生のトレースバック形状のままHTTP層へ漏れないようにする。
        # src/sync_engine/webhook_handlers/zoho_webhook.py の handler() の
        # `except Exception: logger.exception(...)` パターンと同じ方針（本エンドポイントには
        # 同種のガードが無かったため、後追いで揃える）。
        logger.exception("zoho watch channel renewal failed (unexpected error)")
        raise HTTPException(
            status_code=500, detail="internal error during zoho webhook renewal"
        ) from None

    logger.info(
        "zoho watch channel renewed: channel_id=%s channel_expiry=%s",
        result["channel_id"],
        result["channel_expiry"],
    )
    return {
        "status": "success",
        "channel_id": result["channel_id"],
        "channel_expiry": result["channel_expiry"],
    }


@router.get("/api/cron/project-mirror-reconcile", dependencies=[Depends(verify_cron_secret)])
def run_project_mirror_reconcile(
    wiring: ProductionSyncWiring = Depends(wiring_dependency),
) -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、案件管理DBのPostgresミラー（`ProjectMirror`）の夜間
    reconciliationエントリポイント（2026-08-17）。

    Webhook経由のリアルタイム同期（`project_mirror_sync`）だけでは、Webhook購読登録前の
    既存データ・Webhook配信失敗・ページ削除等を取りこぼしうるため、`refresh_all_projects()`
    （初回バックフィルと共通の全件反映処理）をフル実行して整合させる。

    `PROJECT_MIRROR_SYNC_ENABLED`（既定false）が未設定の場合は書き込みをスキップする
    （shirokuma-sec/obasan-qualityレビューWARN対応、2026-08-17）。cronの`vercel.json`登録
    自体は「インフラ整備のみ」段階でも行うため、このガードが無いと環境変数を何も設定して
    いなくてもcron登録した時点で毎晩`ProjectMirror`への書き込みが始まってしまい、計画上の
    「インフラ整備のみでは本番挙動は変わらない」前提と食い違う
    （`build_project_mirror_sync_callable`と同じ「未設定なら無効化」パターンに揃える）。

    `notion_client`には必ず`wiring.project_mirror_notion_client`（案件管理DB専用クライアント）
    を渡すこと。`wiring.any_db_page_client`（Dispatcherが使うクライアント群のいずれか1つが
    入る、どのDBかは不定の変数）を渡してはならない
    （`refresh_all_projects()`が内部で呼ぶ`query_all_pages()`はクライアントに固定された
    database_idの全件を返すdb_key依存の操作であり、2026-08-26に実際に`any_db_page_client`
    （当時の変数名は`notion_page_client`）を渡してしまっていたことで、取引先マスターDBの
    全件を`ProjectMirror`へ誤って書き込む事故が発生した。詳細は
    `docs/project_mirror_activation_note.md`参照）。
    """
    if os.environ.get("PROJECT_MIRROR_SYNC_ENABLED", "").strip().lower() != "true":
        return {"skipped": "PROJECT_MIRROR_SYNC_ENABLED is not set"}
    if wiring.project_mirror_notion_client is None:
        logger.error(
            "run_project_mirror_reconcile: NOTION_API_KEY等が未設定のため実行できません"
        )
        raise HTTPException(status_code=500, detail="notion sync is not configured")
    # NotionUserDirectory()はNotionDataSourceの`_cached("user_directory", ...)`（プロセス内
    # 使い回し）とは意図的に異なり、cron実行のたびに新規構築する。本cronは1日1回・低頻度
    # である一方、担当メンバー名の解決結果（ワークスペースメンバー一覧）を毎回最新化したい
    # ため（コールドスタートを跨いだ古いキャッシュに固定されたくない）。
    user_directory = NotionUserDirectory(
        max_rate_limit_retries=INTERACTIVE_MAX_RATE_LIMIT_RETRIES
    )
    return refresh_all_projects(
        notion_client=wiring.project_mirror_notion_client, user_directory=user_directory
    )


@router.get("/api/cron/relation-sync-reconcile", dependencies=[Depends(verify_cron_secret)])
def run_relation_sync_reconcile(
    wiring: ProductionSyncWiring = Depends(wiring_dependency),
) -> dict[str, Any]:
    """Vercel Cronから1日1回呼ばれる、取引先マスターDBの正規化取引先名→Notion page ID
    インデックス（`ClientNameIndex`）の夜間reconciliationエントリポイント（2026-08-25、
    shirokuma-sec/obasan-qualityレビューBLOCKER対応: ClientNameIndexへの投入経路が本番に
    一切配線されていなかった問題への対応。`run_project_mirror_reconcile`と同じ設計）。

    Webhook経由のリアルタイム同期（`client_name_index_sync`）だけでは、Webhook購読登録前の
    既存データ・Webhook配信失敗・ページ削除等を取りこぼしうるため、`refresh_all_client_names()`
    （初回バックフィルと共通の全件反映処理）をフル実行して整合させる。

    `RELATION_SYNC_ENABLED`（既定false）が未設定の場合は書き込みをスキップする
    （`run_project_mirror_reconcile`と同じ理由: cronの`vercel.json`登録自体は「インフラ整備
    のみ」段階でも行うため、このガードが無いと環境変数を何も設定していなくてもcron登録した
    時点で毎晩`ClientNameIndex`への書き込みが始まってしまい、「インフラ整備のみでは本番挙動は
    変わらない」前提と食い違う）。

    `notion_client`には必ず`wiring.client_master_notion_client`（取引先マスターDB専用
    クライアント）を渡すこと。`wiring.any_db_page_client`を渡してはならない
    （`run_project_mirror_reconcile`と同じ理由・同じ事故のリスク。`refresh_all_client_names()`
    が内部で呼ぶ`query_all_pages()`もdb_key依存の操作であり、`run_project_mirror_reconcile`と
    同様に`any_db_page_client`（当時の変数名は`notion_page_client`）を渡していたため、
    「たまたま辞書の先頭が取引先マスターDBだった」場合にのみ正しく動く状態になっていた
    （2026-08-26修正）。詳細は`docs/project_mirror_activation_note.md`参照）。
    """
    if os.environ.get("RELATION_SYNC_ENABLED", "").strip().lower() != "true":
        return {"skipped": "RELATION_SYNC_ENABLED is not set"}
    if wiring.client_master_notion_client is None:
        logger.error(
            "run_relation_sync_reconcile: NOTION_API_KEY等が未設定のため実行できません"
        )
        raise HTTPException(status_code=500, detail="notion sync is not configured")
    return refresh_all_client_names(notion_client=wiring.client_master_notion_client)
