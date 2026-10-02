"""GASからのGmailメタデータを既存CRM判定へ渡す。"""
from __future__ import annotations

import hmac
import json
import os
from collections.abc import Mapping
from typing import Any

from src.gmail_sync import db, gmail_client, sync
from src.gmail_sync.matcher import find_contact_page_id
from src.sync_engine.webhook_handlers._common import (
    bad_request_response, get_header, internal_error_response, logger, unauthorized_response,
)


def _authorized(headers: Mapping[str, str]) -> bool:
    expected = os.environ.get('GMAIL_INGEST_WEBHOOK_SECRET')
    actual = get_header(headers, 'X-Webhook-Secret')
    return bool(expected and actual and hmac.compare_digest(expected, actual))


def handler(event: Mapping[str, Any], context: object, *, contact_client=None) -> dict[str, Any]:
    if not _authorized(event.get('headers') or {}):
        return unauthorized_response()
    rep_email = os.environ.get('GMAIL_INGEST_REP_EMAIL', '').strip().lower()
    if not rep_email:
        return internal_error_response()
    try:
        body = json.loads(event.get('body') or '')
        if not isinstance(body, dict) or body.get('rep_email', '').lower() != rep_email:
            raise ValueError('担当者が一致しません')
        messages = body.get('messages')
        if not isinstance(messages, list) or len(messages) > 50:
            raise ValueError('messagesは50件以下の配列にしてください')
        dry_run = body.get('dry_run')
        if not isinstance(dry_run, bool):
            raise ValueError('dry_runを指定してください')
        parsed = []
        for item in messages:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ('id', 'from', 'to')):
                raise ValueError('メールの必須項目がありません')
            if not item['id'] or len(item['id']) > 100:
                raise ValueError('メールIDが不正です')
            # 一斉送信の宛先を切り捨てず受理しつつ、入力サイズは制限する。
            limits = {'from': 1000, 'to': 65536, 'subject': 1000, 'snippet': 500,
                      'date_header': 200, 'thread_id': 100, 'internal_date_ms': 20}
            if any(item.get(key) is not None and (
                not isinstance(item[key], str) or len(item[key]) > limit
            ) for key, limit in limits.items()):
                raise ValueError('メール項目の型または長さが不正です')
            stamp = item.get('internal_date_ms')
            if stamp is not None and (not stamp.isdecimal() or int(stamp) <= 0):
                raise ValueError('メール時刻が不正です')
            parsed.append(gmail_client.GmailMessage(
                id=item['id'], from_header=item['from'], to_header=item['to'],
                subject=item.get('subject'), date_header=item.get('date_header'),
                snippet=item.get('snippet'), thread_id=item.get('thread_id'),
                internal_date_ms=item.get('internal_date_ms'),
            ))
    except (ValueError, TypeError, AttributeError) as exc:
        return bad_request_response(str(exc))
    try:
        client = contact_client or sync._default_contact_client()
        cache: dict[str, str | None] = {}

        def resolve(address: str) -> str | None:
            if address not in cache:
                cache[address] = find_contact_page_id(client, address)
            return cache[address]

        results = []
        for message in parsed:
            try:
                status = _process_one(message, rep_email, client, resolve, dry_run)
            except Exception:
                logger.exception('Gmail後処理に失敗しました: message_id=%s', message.id)
                status = 'effect_failed'
            results.append({'id': message.id, 'status': status})
        return {'statusCode': 200, 'body': json.dumps({
            'received': len(parsed), 'dry_run': dry_run,
            'inserted': sum(r['status'] == 'inserted' for r in results),
            'would_insert': sum(r['status'] == 'would_insert' for r in results),
            'results': results,
        })}
    except Exception:
        logger.exception('Gmail GAS取り込みに失敗しました')
        return internal_error_response()


def _process_one(message, rep_email, client, resolve, dry_run: bool) -> str:
    exists = db.email_log_exists(message.id)
    effect_before = db.gmail_ingest_effect_status(message.id) if exists and not dry_run else None
    if dry_run:
        if exists:
            return 'existing'
        classified = sync.classify_message(
            message, rep_email=rep_email, internal_domains=sync.internal_domains_from_env(),
            resolve_contact=resolve,
        )
        return 'would_insert' if classified else 'unmatched'
    if exists and effect_before is None:
        return 'existing'
    if exists and effect_before and all(effect_before.values()):
        return 'existing'
    inserted = sync.record_message(
        message, rep_email, client, internal_domains=sync.internal_domains_from_env(),
        atomic_insert=True, resolve_contact=resolve,
    )
    if inserted:
        return 'inserted'
    if effect_before:
        return 'postprocess_resumed'
    return 'existing' if db.email_log_exists(message.id) else 'unmatched'
