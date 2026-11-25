"""Notion案件管理DB「AI推奨アクション日」変更を、web-engagement-tool側のGoogle Calendar連携API
へ同期するサービス層。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

from src.infrastructure.http_budget import http_budget, remaining, HttpBudgetExceeded
from src.calendar_sync.notion_user_email import get_notion_user_email
from src.calendar_sync.plan import build_event_plan
from src.calendar_sync.web_engagement_tool_client import WebEngagementToolCalendarClient


def sync_next_action_date_to_calendar(
    properties: Mapping[str, Any],
    notion_page_id: str,
    *,
    calendar_client: WebEngagementToolCalendarClient | None = None,
) -> dict[str, Any] | None:
    """取得済み案件のカレンダー専用5項目を使い、次回アクションを担当メンバーの
    Google Calendarへ同期する。

    以下の場合は同期をスキップし`None`を返す:
    - アクション内容またはAI推奨アクション日が空欄。**「削除された（Noneになった）場合にカレンダー側の
      予定も削除すべきか」は今回のスコープ外とし、単に同期をスキップすることでよい。**
    - `"担当メンバー"`が無い、または空リスト（誰のカレンダーに登録すればよいか不明なため）。
    - 先頭の担当メンバーIDのメールアドレスが解決できない（`get_notion_user_email`が`None`を
      返した場合）。

    `calendar_client`省略時は`WebEngagementToolCalendarClient()`をデフォルト生成する
    （テストでは注入して差し替える）。

    この関数自体は例外を握りつぶさない（呼び出し元＝webhookハンドラ側で「失敗してもWebhook
    全体は失敗させない」という判断を行う設計とする）。
    """
    plan = build_event_plan(properties, notion_page_id)
    if plan is None:
        logger.info("calendar_sync skipped: reason=missing_action_or_date_or_no_action")
        return None

    reps = properties.get("担当メンバー")
    if not reps:
        logger.info("calendar_sync skipped: reason=missing_rep")
        return None

    rep_email = get_notion_user_email(reps[0])
    if rep_email is None:
        logger.info("calendar_sync skipped: reason=unresolved_rep")
        return None

    if calendar_client is None:
        calendar_client = WebEngagementToolCalendarClient()

    return calendar_client.upsert_event(rep_email=rep_email, **plan)


_CALENDAR_HOLD_REASONS = {
    'time_budget': '今回の処理時間が不足しています。書込未着手のタスクは次の再通知で処理してください。',
    'todo_not_verified': 'TODOが未確認です。TODOの保留理由を解消して再通知してください。',
    'task_date_missing': '期限が未入力です。対象アクションに西暦の完全な年月日を入力して再通知してください。箇条書きでない単一文章のみAI推奨アクション日を入力してください。',
    'assignee_missing': '担当メンバーが未入力です。案件に担当者を設定して再通知してください。',
    'calendar_not_configured': 'カレンダー連携が未設定です。連携設定後に再通知してください。',
    'assignee_email_unresolved': '担当者のメールを確認できません。Notionユーザー設定を確認して再通知してください。',
    'rep_not_connected': '担当者のGoogle Calendarが未接続です。本人が接続してから再通知してください。',
    'legacy_event_migration_required': '既存の案件単位予定があります。旧予定の移行方針を確認してから再通知してください。自動転用・削除はしません。',
    'calendar_result_unknown': 'カレンダーの作成・更新結果が不明です。遅延した旧処理の完了を含めて手動照合するまで、TODO・予定の再更新を停止します。',
    'task_sync_failed': 'タスク同期を完了できませんでした。予約・保存済みIDを照合してから再通知してください。',
}


def _has_task_budget(minimum):
    try:
        available = remaining()
        return available is None or available >= minimum
    except HttpBudgetExceeded:
        return False


def _sync_task_locked(task, todo_sync, calendar_client):
    from src.calendar_sync.todo import digest
    key = f'calendar-task:{task.project_id}:{task.key}'
    pending = todo_sync.journal.calendar_get(key)
    if pending and pending['state'] == 'reserved':
        return {'task_id': task.key, 'todo': {'status': 'held', 'reason': 'calendar_result_unknown'},
                'calendar': {'status': 'held', 'reason': 'calendar_result_unknown'}}
    result = {'task_id': task.key, 'todo': todo_sync.sync_locked(task)}
    if result['todo']['status'] != 'verified':
        reason = 'todo_not_verified'
    elif task.due is None:
        reason = 'task_date_missing'
    elif not task.assignees:
        reason = 'assignee_missing'
    elif calendar_client is None:
        reason = 'calendar_not_configured'
    else:
        rep_email = get_notion_user_email(task.assignees[0])
        if not rep_email:
            reason = 'assignee_email_unresolved'
        else:
            request = {'rep_email': rep_email, **task.event()}
            if not _has_task_budget(12):
                reason = 'time_budget'
            elif not todo_sync.journal.calendar_reserve(key, digest(request)):
                reason = 'calendar_result_unknown'
            else:
                try:
                    response = calendar_client.upsert_event(**request)
                    skipped = response.get('skipped')
                    if skipped in {'rep_not_connected', 'legacy_event_migration_required'}:
                        todo_sync.journal.calendar_finish(key, skipped=_CALENDAR_HOLD_REASONS[skipped])
                    elif isinstance(response.get('google_event_id'), str) and response['google_event_id']:
                        todo_sync.journal.calendar_finish(key, event_id=response['google_event_id'])
                    else:
                        raise RuntimeError('カレンダーの成功応答を確認できません')
                    result['calendar'] = response
                    return result
                except Exception:
                    # 相手側で遅れて書込が完了し得る。期限で予約を解除せず次の更新も止める。
                    reason = 'calendar_result_unknown'
    result['calendar'] = {'status': 'held', 'reason': reason}
    return result


@http_budget(120)
def sync_project_actions(properties, notion_page_id, *, todo_sync, calendar_client=None):
    """案件の再開位置を直列化し、同入力の再通知では未着手の続きから進める。"""
    from src.calendar_sync.tasks import build_task_plan
    from uuid import UUID
    notion_page_id = str(UUID(notion_page_id))
    plan = build_task_plan(properties, notion_page_id)
    project_key = f'calendar-task-plan:{notion_page_id}'
    with todo_sync.journal.lock(project_key):
        from datetime import datetime, timezone
        from src.calendar_sync.plan import CALENDAR_FIELDS, SOURCE_VERSION_KEY
        from src.calendar_sync.todo import digest
        try:
            source_at = datetime.fromisoformat(properties[SOURCE_VERSION_KEY].replace("Z", "+00:00"))
            if source_at.tzinfo is None:
                raise ValueError('原本時刻にタイムゾーンがありません')
        except (KeyError, AttributeError, TypeError, ValueError):
            reason = '原本の最終編集時刻を確認できません。案件を再取得してから再通知してください。'
            todo_sync.journal.hold(project_key, reason + ' 案件: https://www.notion.so/' + notion_page_id.replace('-', ''))
            return {'tasks': [], 'holds': [reason], 'reason': 'source_version_missing'}
        source_hash = digest({name: properties.get(name) for name in CALENDAR_FIELDS})
        admission = todo_sync.journal.accept_source(project_key, source_at.astimezone(timezone.utc).isoformat(), source_hash)
        if admission == 'stale':
            return {'tasks': [], 'holds': [], 'skipped': 'stale_source_version'}
        if admission == 'conflict':
            reason = '同じ原本時刻で内容が異なります。案件の最新値を再取得してから再通知してください。'
            todo_sync.journal.hold(project_key, reason + ' 案件: https://www.notion.so/' + notion_page_id.replace('-', ''))
            return {'tasks': [], 'holds': [reason], 'reason': 'source_version_conflict'}
        return _sync_project_actions_locked(plan, notion_page_id, project_key, todo_sync, calendar_client)


def _sync_project_actions_locked(plan, notion_page_id, project_key, todo_sync, calendar_client):
    from dataclasses import asdict
    from src.calendar_sync.todo import digest
    project_url = f'https://www.notion.so/{notion_page_id.replace("-", "")}'
    plan_hash = digest({'tasks': [asdict(task) for task in plan.tasks], 'calendar_configured': calendar_client is not None})
    start = todo_sync.journal.batch_position(project_key, plan_hash)
    if not isinstance(start, int) or start < 0 or start >= len(plan.tasks):
        start = 0
    results = []
    next_index = start
    deferred = False
    todo_sync.journal.hold(project_key, '案件アクション同期の処理中です。中断時は再通知で続きから再開します。 案件: ' + project_url)
    for index, task in enumerate(plan.tasks[start:], start):
        key = f'calendar-task:{task.project_id}:{task.key}'
        with todo_sync.journal.lock(key):
            try:
                if _has_task_budget(35):
                    result = _sync_task_locked(task, todo_sync, calendar_client)
                else:
                    deferred = True
                    result = {'task_id': task.key, 'todo': {'status': 'held', 'reason': 'time_budget'},
                              'calendar': {'status': 'held', 'reason': 'time_budget'}}
            except Exception:
                result = {'task_id': task.key, 'todo': {'status': 'held', 'reason': 'task_sync_failed'},
                          'calendar': {'status': 'held', 'reason': 'task_sync_failed'}}
            calendar = result['calendar']
            event_key = f'calendar-event:{task.project_id}:{task.key}'
            if calendar.get('status') == 'held' or calendar.get('skipped'):
                code = calendar.get('reason') or calendar.get('skipped')
                message = _CALENDAR_HOLD_REASONS.get(code, '予定が未反映です。保存状態を確認してください。')
                calendar['message'] = message
                todo_sync.journal.hold(event_key, message + ' 案件: ' + project_url)
            else:
                todo_sync.journal.clear(event_key)
            results.append(result)
            if deferred:
                break
            next_index = index + 1
            todo_sync.journal.batch_advance(project_key, plan_hash, next_index)
    if deferred:
        todo_sync.journal.hold(project_key, f'残り{len(plan.tasks) - next_index}件は時間不足で未着手です。再通知で続きから再開します。 案件: ' + project_url)
    else:
        todo_sync.journal.batch_advance(project_key, plan_hash, 0)
        if plan.holds:
            todo_sync.journal.hold(project_key, ' / '.join(plan.holds) + ' 案件: ' + project_url)
        else:
            todo_sync.journal.clear(project_key)
    logger.info('案件タスク同期: tasks=%d plan_holds=%d deferred=%s', len(results), len(plan.holds), deferred)
    return {'tasks': results, 'holds': list(plan.holds), 'resume_from': start,
            'remaining_count': len(plan.tasks) - next_index if deferred else 0}


# 新経路だけが原本版を必須にする。旧単一予定の呼出契約は維持する。
sync_project_actions.requires_source_version = True
