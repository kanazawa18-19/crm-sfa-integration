"""観測の欠測・途絶・滞留を判定する。再送や予約解除は行わない。"""
import math


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def assess(observation, *, now, last_worker_success, freshness_seconds, waiting_seconds, processing_seconds):
    limits = (freshness_seconds, waiting_seconds, processing_seconds)
    if not _number(now) or any(not _number(n) or n <= 0 for n in limits):
        raise ValueError('現在時刻と正の監視上限を指定してください')
    alerts = []
    def fresh(value):
        return _number(value) and 0 <= now - value <= freshness_seconds
    if not isinstance(observation, dict) or observation.get('complete') is not True:
        alerts.append('observation_missing_or_incomplete')
        observation = {}
    if not fresh(observation.get('scan_finished_at')):
        alerts.append('observation_stale_or_invalid')
    started, finished = observation.get('scan_started_at'), observation.get('scan_finished_at')
    if not fresh(started) or not _number(finished) or (_number(started) and finished < started):
        alerts.append('scan_time_invalid')
    if not fresh(last_worker_success):
        alerts.append('worker_success_missing_or_stale')
    counts = observation.get('counts') or {}
    if not isinstance(counts, dict):
        counts = {}
    for state, limit, age_key, invalid_key in (
        ('waiting', waiting_seconds, 'oldest_waiting_age_seconds', 'invalid_waiting_timestamp_count'),
        ('processing', processing_seconds, 'oldest_processing_age_seconds', 'invalid_processing_timestamp_count'),
    ):
        parts = [counts.get('pending'), counts.get('retry')] if state == 'waiting' else [counts.get('processing')]
        if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in parts):
            alerts.append(state + '_count_missing')
            continue
        count = sum(parts)
        invalid, age = observation.get(invalid_key), observation.get(age_key)
        if type(invalid) is not int or invalid != 0 or (count and (not _number(age) or age < 0)):
            alerts.append(state + '_age_unknown')
        elif count and _number(started) and age + now - started > limit:
            alerts.append(state + '_overdue')
    for key in ('unknown_state_count',):
        if type(observation.get(key)) is not int or observation.get(key) != 0:
            alerts.append(key)
    attention = counts.get('needs_attention')
    if not isinstance(attention, int) or isinstance(attention, bool) or attention < 0:
        alerts.append('attention_count_missing')
    elif attention:
        alerts.append('needs_attention')
    return {'healthy': not alerts, 'alerts': alerts, 'automatic_recovery': False}
