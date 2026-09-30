"""本人DMからのCRM取引先検索。更新操作の範囲が決まるまで読み取り専用。"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs

from src.api.client_360_service import search_clients
from src.sync_engine.webhook_handlers.slack_interaction_webhook import _verify_slack_signature
from src.sync_engine.webhook_handlers._common import unauthorized_response, logger


def _reply(message: str) -> dict[str, Any]:
    import json
    return {'statusCode': 200, 'body': json.dumps({'response_type': 'ephemeral', 'text': message}, ensure_ascii=False)}


def _escape(value: str) -> str:
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def handler(event: Mapping[str, Any], context: object, *, search=search_clients) -> dict[str, Any]:
    body = event.get('body') or ''
    if not _verify_slack_signature(event.get('headers') or {}, body):
        return unauthorized_response()
    form = parse_qs(body)
    user_id = (form.get('user_id') or [''])[0]
    channel_id = (form.get('channel_id') or [''])[0]
    team_id = (form.get('team_id') or [''])[0]
    command = (form.get('command') or [''])[0]
    allowed = {part.strip() for part in os.environ.get('SLACK_CRM_ALLOWED_USER_IDS', '').split(',') if part.strip()}
    expected_team = os.environ.get('SLACK_CRM_ALLOWED_TEAM_ID', '')
    if not expected_team or team_id != expected_team or command != '/crm':
        return _reply('このSlackワークスペースのCRMコマンドは利用できません。')
    if user_id not in allowed or not channel_id.startswith('D'):
        return _reply('この操作は登録済みユーザーのDMから利用してください。')
    query = (form.get('text') or [''])[0].strip()
    if len(query) < 2 or len(query) > 80:
        return _reply('取引先名を2〜80文字で入力してください。例: /crm 取引先名')
    try:
        result = search(query)
        clients = result.get('clients') or []
        if not clients:
            return _reply('該当する取引先はありません。')
        base_url = os.environ.get('DASHBOARD_BASE_URL', '').rstrip('/')
        lines = []
        for client in clients[:10]:
            page_id = str(client.get('notion_page_id') or '')
            name = _escape(str(client.get('取引先名') or '名称なし'))
            if page_id and base_url.startswith('https://'):
                from urllib.parse import quote
                lines.append(f'<{base_url}/clients/{quote(page_id, safe="")}|{name}>（ID: {page_id[:8]}）')
            else:
                lines.append(f'{name}（ID: {page_id[:8]}）')
        suffix = '\nほかにも候補があります。検索語を詳しくしてください。' if result.get('truncated') or len(clients) > 10 else ''
        return _reply('取引先候補:\n' + '\n'.join(lines) + suffix)
    except Exception:
        logger.exception('Slack CRM検索に失敗しました')
        return _reply('検索できませんでした。時間をおいてもう一度お試しください。')
