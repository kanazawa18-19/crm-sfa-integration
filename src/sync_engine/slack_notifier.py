"""Slack管理チャンネルへのアラート通知（05_同期・競合制御「アラート通知」）。

重要項目（config/conflict_alert_properties.json）のコンフリクトが自動解決された際、
対象案件・変更項目・採用データ・却下データをSlackへ即時通知する。dispatcherはこの
Protocolを介して通知を行い、テストではモックNotifierを注入できるようにする。

`notify_new_record_created`/`notify_new_record_issue`は、新規レコード作成
（`AUTO_CREATE_NEW_RECORDS_ENABLED`、2026-08-25、Round2）の運用可視性のために追加した
（shirokuma-sec/obasan-qualityレビューWARN対応）。特に`notify_new_record_issue`は、Notion
ページ作成後にIdMapping登録が失敗した「孤児ページ」の検知（BLOCKER1対応）にも使われる。

`notify_update_skipped`は、既に`IdMapping`が存在するレコードへの**通常の更新イベント**
（`Dispatcher.dispatch()`本体、`_try_create_new_record()`とは別経路）で、コンフリクト
判定・書き込み判断に使う「現在値」の取得自体が例外で失敗し、この同期イベントの以降の
プロパティの書き込みを中止（スキップ）した場合に使う（2026-08-27本番障害対応の残存リスク
への決着、`docs/relation_sync_activation_note.md`参照）。`notify_new_record_issue`と役割が
異なる（あちらは「まだ何も作られていない新規作成」の問題、こちらは「本来適用されるはずだった
既存レコードへの更新が失われた」ことの通知）ため、専用のメソッドとして分離している。

**1つの`SyncEvent`は複数プロパティを持ちうる**（BLOCKER1対応、2026-08-28）: `detail`引数の
文面は`Dispatcher`側（`_build_update_skip_detail()`）で組み立てており、このイベント内で
「どのプロパティの処理中に失敗したか」「同じイベント内で既に他ツールへ書き込み済みの
プロパティがあるか（あれば何か）」を含む。以前は「書き込みは行われていません」と一律に
断定していたが、複数プロパティのイベントで1つ目が既に書き込み済みのまま2つ目以降で失敗
した場合にこれが事実と異なり、運用者に誤った状況認識を与える危険があったための対応。

■ 通知先について（2026-08-25、送信先変更）: `notify_conflict`は引き続き
`SLACK_WEBHOOK_URL_ALERT`環境変数のIncoming Webhookへ送る（Round1から変更なし、本番で
このWebhookは設定済み・運用実績あり）。一方`notify_new_record_created`/
`notify_new_record_issue`（Round2）は、本番環境に`SLACK_WEBHOOK_URL_ALERT`が未設定だった
ことが判明したため、`src/incident_detection/notify.py`と同じ「`User.isManager = true`の
全ユーザーへSlack DM」方式（`src/notifications/manager_dm.py`、`SLACK_BOT_TOKEN`を使用、
新規env変数なし）に変更した。通知先をハードコードせず、dashboard管理画面で`isManager`
フラグをON/OFFすることで動的に増減できる（金沢さん要望: まずは金沢のDM、将来的には
マネージャー陣のDMへ拡張）。

■ 例外を投げない設計について（2026-08-25、3回目最終レビューBLOCKER対応。DM送信方式への
変更後も踏襲）: `WebhookSlackNotifier`の各`notify_*`メソッドは、送信手段が
Incoming Webhook（`_post()`）でもSlack DM（`_notify_managers()`、内部で
`manager_dm.notify_managers()`を呼ぶ）でも、送信失敗（`requests`が送出する例外・
Slack API側のエラーレスポンス・DB接続失敗等）を**呼び出し元へ一切伝播させない**
（内部でtry/exceptし、失敗時はログのみ残して静かに戻る）。
`Dispatcher._handle_uncertain_notion_page_creation()`/`_handle_orphaned_notion_page()`のような
「他の保護ロジックが失敗した後の最終防衛線」でこの通知を呼んでいる箇所があり、もし通知自体が
例外を投げると、その例外がWebhookハンドラの広い`except Exception`まで伝播して500応答となり、
kintone/Zoho側のリトライで同じイベントが再送され、Round2全体が防ごうとしていた重複ページ
作成が（保護ロジックそのものは正しく動いたにもかかわらず）通知処理の失敗だけを引き金に
再現してしまう。呼び出し箇所ごとに個別のtry/exceptを重ねるのではなく、Notifier自体を
「絶対に失敗しない」実装にすることで、全ての呼び出し箇所を一括で守る。
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import requests

from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.conflict_resolver import RejectedData

logger = logging.getLogger(__name__)

# `notify_new_record_issue`のreasonごとの一言アクション（obasan-qualityレビュー対応、
# 2026-08-25）。深夜・休日に届きうる緊急通知（特に🚨の2件）でも、内部識別子の生値だけでなく
# その場で次に何をすればよいか分かるようにする。
#
# ■ docs/relation_sync_activation_note.md との同期について: 本dictの文言は
# `docs/relation_sync_activation_note.md`の「Round2のロールアウト手順」節・
# 「動作確認チェックリスト」に記載の対処手順と一致させてある。本dictを変更した場合は、
# 必ず同ドキュメントの当該節も合わせて更新すること（二重管理を避けるため、詳細な手順自体は
# ドキュメント側に譲り、DM本文には要点のみを埋め込む）。
_ISSUE_REASON_ACTION_HINTS: dict[str, str] = {
    "missing_required_properties": (
        "⚠️ 必須プロパティが不足しているためページは作成されていません。kintone/Zoho側の"
        "入力を確認し、必須項目を補ってください（自動での再作成は行われません）。"
    ),
    "mapping_registration_failed": (
        "🚨 通知に含まれるNotion page IDを直接開き、アーカイブ済みか確認してください。"
        "アーカイブされていなければ手動でアーカイブするか、内容を確認して正式なマッピングを"
        "手動登録してください。"
    ),
    "notion_creation_status_unknown": (
        "🚨 監査ログとNotion上を突き合わせて実際にページが作成されたか確認してください。"
        "見つかった場合は正式なIdMappingを手動登録するか、不要であればアーカイブしてください。"
    ),
    "source_record_fetch_failed": (
        "🚨 kintone/Zoho側APIの障害・レート制限、または対象レコードのapp/IDの不整合が"
        "疑われます。ログのexternal_id/db_keyから該当レコードを特定し、kintone/Zoho側で"
        "手動確認してください（ページはまだ作成されていません。自動での再作成は行われ"
        "ないため、原因が解消すれば当該ツール側の再更新等で再送させる必要があります）。"
    ),
}

# `notify_update_skipped`のreasonごとの一言アクション（`_ISSUE_REASON_ACTION_HINTS`と同じ
# パターン）。既存のNotionリレーション同期の残存リスク対応（2026-08-27〜28）で追加。
#
# ■ docs/relation_sync_activation_note.md との同期について: 本dictを変更した場合は、
# `_ISSUE_REASON_ACTION_HINTS`と同様に同ドキュメントの対応する記載も合わせて更新すること。
_UPDATE_SKIP_REASON_ACTION_HINTS: dict[str, str] = {
    "property_write_skipped": (
        "送り先は分かっているのに書き込めていません。"
        "IDマッピングの状態と、そのツールの認証情報を確認してください。"
        "相手側で同時に編集されていた場合は、もう一度その項目を編集し直すと反映されます"
    ),
    "update_notion_value_fetch_failed": (
        "🚨 上記詳細の通り、このイベントに含まれる一部プロパティは既に他ツールへ書き込み"
        "済みの場合があります（詳細を必ず確認してください）。Notion APIの障害・レート制限、"
        "または対象ページの削除・権限不足が疑われます。ログのnotion_key/db_keyから対象ページを"
        "特定し、Notion側の状態を確認してください。原因が解消すれば、送信元ツール側で"
        "対象レコードを再度更新するなどして再送させる必要があります（自動リトライは"
        "行われません。未適用のプロパティは再送されるまで反映されないままです）。"
    ),
    "update_target_value_fetch_failed": (
        "🚨 上記詳細の通り、このイベントに含まれる一部プロパティは既に他ツールへ書き込み"
        "済みの場合があります（詳細を必ず確認してください）。対象ツールのAPI障害・レート制限、"
        "または対象レコードの不整合が疑われます。ログのexternal_id/db_keyから対象レコードを"
        "特定し、当該ツール側の状態を確認してください。原因が解消すれば、送信元ツール側で"
        "対象レコードを再度更新するなどして再送させる必要があります（自動リトライは"
        "行われません。未適用のプロパティは再送されるまで反映されないままです）。"
    ),
}


def _db_key_display_name(db_key: str) -> str:
    """`db_key`の人間向け表示名（例: "取引先マスターDB"）を返す。未知のdb_keyの場合は
    `db_key`自体をそのまま返す（DM本文の生成自体を失敗させないため、安全側に倒す）。
    """
    try:
        return get_schema(db_key).display_name
    except KeyError:
        return db_key


class SlackNotifier(Protocol):
    """アラート通知を送るための最小インターフェース。"""

    def notify_conflict(self, rejected: RejectedData) -> None: ...

    def notify_new_record_created(
        self, *, db_key: str, source_tool: Tool, external_id: str, notion_page_id: str
    ) -> None:
        """新規Notionページが作成されたことを通知する。"""
        ...

    def notify_new_record_issue(
        self,
        *,
        db_key: str,
        source_tool: Tool,
        external_id: str,
        reason: str,
        detail: str,
        notion_page_id: str | None = None,
    ) -> None:
        """新規レコード作成処理で問題が発生したことを通知する（必須プロパティ不足による
        スキップ、IdMapping登録失敗による孤児ページ発生等）。`notion_page_id`は、Notion
        ページ自体は既に作成されている場合（孤児ページ）にのみ指定される。
        """
        ...

    def notify_update_skipped(
        self,
        *,
        db_key: str,
        source_tool: Tool,
        external_id: str,
        reason: str,
        detail: str,
    ) -> None:
        """既に`IdMapping`が存在するレコードへの通常の更新イベントが、現在値取得の失敗により
        適用されずスキップされたことを通知する。
        """
        ...


class WebhookSlackNotifier:
    """コンフリクト通知（`notify_conflict`）はSLACK_WEBHOOK_URL_ALERT環境変数のIncoming
    Webhookへ、新規レコード作成関連の通知（`notify_new_record_created`/
    `notify_new_record_issue`）は`User.isManager = true`の全ユーザーへのSlack DMへ送る実装
    （クラス名は歴史的経緯によりWebhook前提のままだが、送信手段はメソッドにより異なる。
    モジュールdocstring「通知先について」参照）。

    本番投入時はリトライ・レート制限・Block Kit等によるリッチな整形を検討すること。
    ここでは仕様書05節の通知内容（対象案件 / 変更項目 / 採用データ / 却下データ）を
    満たす最低限のテキスト通知のみを実装する。
    """

    def __init__(self, webhook_url: str | None = None) -> None:
        self._webhook_url = webhook_url

    @property
    def _url(self) -> str | None:
        return self._webhook_url or os.environ.get("SLACK_WEBHOOK_URL_ALERT")

    def _post(self, text: str) -> None:
        """Slack Incoming WebhookへPOSTする。`requests.post()`が送出する例外
        （タイムアウト・接続断・5xx等）は、呼び出し元の同期処理（`Dispatcher`の保護ロジック
        自体を含む）を巻き込んで失敗させないよう、ここで捕捉してログに残すのみとする
        （モジュールdocstring「例外を投げない設計について」参照。3回目最終レビューBLOCKER
        対応、2026-08-25）。Slack通知自体はあくまで副次的な機能であり、本来の同期処理を
        失敗させてはならない（`src/audit_log/recorder.py`の「副次機能は失敗してもメインを
        止めない」方針と同じ考え方）。
        """
        url = self._url
        if not url:
            # Webhook URL未設定時は通知を送らない（ローカル開発・URL未発行段階での動作を妨げない）。
            return
        try:
            requests.post(url, json={"text": text}, timeout=10)
        except Exception:
            logger.warning(
                "WebhookSlackNotifier: failed to post to Slack webhook; continuing without "
                "raising (Slack notification is a secondary feature and must not block the "
                "caller's main processing)",
                exc_info=True,
            )

    def _notify_managers(self, text: str) -> None:
        """`text`を`manager_dm.notify_managers()`経由で`User.isManager = true`の全員へ
        Slack DMする。`manager_dm.notify_managers()`自体が内部で例外を握りつぶす設計だが
        （`SLACK_BOT_TOKEN`未設定・manager解決失敗・DM送信失敗いずれも静かにログのみで
        戻る）、`_post()`と同じ「Notifier自体を絶対に失敗しない実装にする」防御をここでも
        一段重ねる（モジュールdocstring「例外を投げない設計について」参照）。

        `manager_dm`はモジュールの先頭ではなくここで遅延importする:
        `manager_dm` → `src.meeting_sync.slack_approval` →
        `src.sync_engine.clients.notion_lookup` → ... → `src.sync_engine.dispatcher` →
        本モジュール、という循環importが発生するため（`dispatcher.py`が`SlackNotifier`
        Protocolをモジュールレベルでimportしている）。
        """
        from src.notifications import manager_dm

        try:
            manager_dm.notify_managers(text, log_context="WebhookSlackNotifier")
        except Exception:
            logger.warning(
                "WebhookSlackNotifier: failed to notify managers via Slack DM; continuing "
                "without raising (Slack notification is a secondary feature and must not "
                "block the caller's main processing)",
                exc_info=True,
            )

    def notify_conflict(self, rejected: RejectedData) -> None:
        text = (
            "[同期コンフリクト自動解決]\n"
            f"対象案件: {rejected.record_id}\n"
            f"変更項目: {rejected.property_name}\n"
            f"採用データ: {rejected.adopted_value}（採用元: {rejected.adopted_tool.value}）\n"
            f"却下データ: {rejected.rejected_value}（却下元: {rejected.rejected_tool.value}）\n"
            f"発生日時: {rejected.occurred_at.isoformat()}"
        )
        self._post(text)

    def notify_new_record_created(
        self, *, db_key: str, source_tool: Tool, external_id: str, notion_page_id: str
    ) -> None:
        text = (
            "[新規レコード自動作成]\n"
            f"DB: {db_key}（{_db_key_display_name(db_key)}）\n"
            f"作成元: {source_tool.value}（external_id={external_id}）\n"
            f"Notion page ID: {notion_page_id}"
        )
        self._notify_managers(text)

    def notify_new_record_issue(
        self,
        *,
        db_key: str,
        source_tool: Tool,
        external_id: str,
        reason: str,
        detail: str,
        notion_page_id: str | None = None,
    ) -> None:
        lines = [
            "[新規レコード自動作成で問題が発生しました]",
            f"DB: {db_key}（{_db_key_display_name(db_key)}）",
            f"対象: {source_tool.value}（external_id={external_id}）",
            f"理由: {reason}",
            f"詳細: {detail}",
        ]
        action_hint = _ISSUE_REASON_ACTION_HINTS.get(reason)
        if action_hint:
            lines.append(f"対応: {action_hint}")
        if notion_page_id:
            lines.append(f"⚠️ Notion page ID（要確認・孤児ページの可能性あり）: {notion_page_id}")
        self._notify_managers("\n".join(lines))

    def notify_update_skipped(
        self,
        *,
        db_key: str,
        source_tool: Tool,
        external_id: str,
        reason: str,
        detail: str,
    ) -> None:
        lines = [
            "[同期更新イベントが適用されませんでした]",
            f"DB: {db_key}（{_db_key_display_name(db_key)}）",
            f"対象: {source_tool.value}（external_id={external_id}）",
            f"理由: {reason}",
            f"詳細: {detail}",
        ]
        action_hint = _UPDATE_SKIP_REASON_ACTION_HINTS.get(reason)
        if action_hint:
            lines.append(f"対応: {action_hint}")
        self._notify_managers("\n".join(lines))
