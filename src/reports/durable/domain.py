"""ページ収集のしおりと配信可否。外部APIやDBに依存しない。"""
from datetime import datetime


class ReportHeld(ValueError):
    pass


def query_body(state, cutoff):
    filters = [{'timestamp': 'created_time', 'created_time': {'on_or_before': cutoff}}]
    if state.get('watermark'):
        filters.append({'timestamp': 'created_time', 'created_time': {'on_or_after': state['watermark']}})
    body = {'page_size': 100, 'sorts': [{'timestamp': 'created_time', 'direction': 'ascending'}],
            'filter': filters[0] if len(filters) == 1 else {'and': filters}}
    if state.get('cursor'): body['start_cursor'] = state['cursor']
    return body


def accept_page(state, response):
    """不完全応答を完了扱いせず、境界を重ねて取得する。IDの重複は保存側で除く。"""
    pages = response.get('results')
    if not isinstance(pages, list) or type(response.get('has_more')) is not bool:
        raise ReportHeld('収集応答の形式を確認できません')
    status = response.get('request_status')
    if status is not None and (not isinstance(status, dict) or status.get('type') not in {None, 'complete', 'incomplete'}):
        raise ReportHeld('収集の完了状態を確認できません')
    watermark = state.get('watermark')
    newest = state.get('newest') or watermark
    previous_time = datetime.fromisoformat(newest.replace('Z', '+00:00')) if newest else None
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get('id'), str) or not page['id']:
            raise ReportHeld('収集したページIDが不正です')
        try: created = datetime.fromisoformat(page['created_time'].replace('Z', '+00:00'))
        except (KeyError, ValueError, TypeError, AttributeError): raise ReportHeld('作成日時を確認できません') from None
        if created.tzinfo is None or (previous_time and created < previous_time):
            raise ReportHeld('収集順序が変わりました')
        previous_time, newest = created, page['created_time']
    count = state.get('roundCount', 0) + len(pages)
    if response['has_more']:
        cursor = response.get('next_cursor')
        if not isinstance(cursor, str) or not cursor or cursor == state.get('cursor'):
            raise ReportHeld('収集の続き位置を確認できません')
        return {'watermark': watermark, 'newest': newest, 'cursor': cursor, 'roundCount': count}, False
    truncated = (status or {}).get('type') == 'incomplete' or ('request_status' not in response and count >= 9000)
    if truncated:
        if not newest or newest == watermark:
            raise ReportHeld('収集の続き位置が進みません。全件確認まで送信しません')
        return {'watermark': newest, 'newest': newest, 'roundCount': 0}, False
    return {'watermark': watermark, 'newest': newest, 'roundCount': count}, True


def delivery_action(existing, destination_hash):
    if existing is None: return 'reserve'
    if existing['destinationHash'] != destination_hash:
        raise ReportHeld('配信先が変更されています。既存の送達記録を確認してください')
    if existing['state'] == 'delivered': return 'skip'
    raise ReportHeld('送信結果が未確定です。実物を確認するまで自動再送しません')
