"""大規模ZohoをID順に読み、時間切れでも未照合部分を残して再開する。"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import re

from src.hub_creation.domain import CreationHeld, identity_hash
from src.infrastructure.http_budget import HttpBudgetExceeded, http_budget, remaining
from src.record_merge.creation_candidates import creation_context, hold_duplicate
from src.record_merge.domain import digest
from src.sync_engine.clients._http import ApiError, raise_for_error

PENDING = '外部の重複照合を分割処理中です。続きは定期処理で確認します'
RESUME = '問題を解消した後、登録元を再通知して再開してください'
MAX_CATCHUP_ROUNDS = 3


def utc_second():
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(value):
    return value.isoformat(timespec='seconds')


class ZohoDuplicateScan:
    def __init__(self, client, journal, *, slice_seconds=60):
        self.client, self.journal = client, journal
        self.slice_seconds = slice_seconds

    def exists(self):
        context = creation_context()
        return context is not None and self.journal.get(context['sourceKey']) is not None

    def defer(self):
        """通常一覧の時間切れでも、次回を先頭10ページに固定しない。"""
        context = creation_context()
        if context is None:
            return
        fingerprint = digest(context)
        row = self.journal.get(context['sourceKey'])
        checkpoint = (row['checkpoint'] if row and row['inputHash'] == fingerprint else
                      {'phase': 'full', 'last_id': '0', 'since': stamp(utc_second() - timedelta(seconds=1))})
        self.journal.save(context, fingerprint, checkpoint)

    def check(self, db_key, module, fields, matches):
        context = creation_context()
        if context is None or context['dbKey'] != db_key:
            raise CreationHeld('大規模照合の登録元を確認できません')
        fingerprint = digest(context)
        row = self.journal.get(context['sourceKey'])
        now = utc_second()
        if row is None or row['inputHash'] != fingerprint:
            checkpoint = {'phase': 'full', 'last_id': '0', 'since': stamp(now - timedelta(seconds=1))}
        else:
            checkpoint = deepcopy(row['checkpoint'])
            if row['state'] == 'held':
                checkpoint['catchup_rounds'] = 0
                if checkpoint['phase'] == 'delta':
                    checkpoint.update(last_id='0', until=stamp(now))
            if checkpoint['phase'] == 'verified':
                checkpoint = {'phase': 'delta', 'last_id': '0', 'since': checkpoint['since'],
                              'until': stamp(now), 'catchup_rounds': 0}
        self.journal.save(context, fingerprint, checkpoint)
        try:
            with http_budget(self.slice_seconds):
                while True:
                    remaining()
                    records, more = self._page(module, fields, checkpoint)
                    prior = int(checkpoint['last_id'])
                    for record in records:
                        identifier = record.get('id') if isinstance(record, dict) else None
                        if (not isinstance(identifier, str) or not identifier.isascii() or not identifier.isdigit()
                                or int(identifier) <= prior):
                            raise CreationHeld('外部の照合順序を確認できません')
                        prior = int(identifier)
                        if matches(record):
                            hold_duplicate('zoho', db_key, identifier, record,
                                '外部に名前またはメールが一致する候補があります。比較してください',
                                fetch=lambda identifier=identifier: self.client.get_record(module, identifier))
                    # 候補確認・応答検証が終わるまでページの先へしおりを進めない。
                    if more and not records:
                        raise CreationHeld('外部の照合ページが欠けています')
                    checkpoint['last_id'] = str(prior)
                    if not more:
                        finished = utc_second()
                        if checkpoint['phase'] == 'delta':
                            until = datetime.fromisoformat(checkpoint['until'])
                            if 0 <= (finished - until).total_seconds() <= 5:
                                checkpoint = {'phase': 'verified', 'last_id': '0',
                                              'since': stamp(until - timedelta(seconds=1))}
                                self.journal.save(context, fingerprint, checkpoint)
                                return
                            checkpoint['catchup_rounds'] = checkpoint.get('catchup_rounds', 0) + 1
                            if checkpoint['catchup_rounds'] >= MAX_CATCHUP_ROUNDS:
                                raise CreationHeld('変更分の照合を3巡しましたが、毎回5秒以内に追いつけません。'
                                                   'Zohoの応答速度または更新頻度を確認してください')
                            checkpoint['since'] = stamp(until - timedelta(seconds=1))
                        checkpoint.update(phase='delta', last_id='0', until=stamp(finished))
                    self.journal.save(context, fingerprint, checkpoint)
        except HttpBudgetExceeded:
            raise CreationHeld(PENDING) from None
        except CreationHeld as exc:
            reason = str(exc) + '。' + RESUME
            self.journal.save(context, fingerprint, checkpoint, state='held', reason=reason)
            raise CreationHeld(reason) from None
        except ApiError as exc:
            reason = ('ZohoのCOQL読取権限または認証を確認してください' if exc.status_code in (401,403)
                      else '外部の重複照合で通信の確認が必要です')
            reason += '。' + RESUME
            self.journal.save(context, fingerprint, checkpoint, state='held', reason=reason)
            raise CreationHeld(reason) from None
        except Exception:
            reason = '外部の重複照合を確認できません。未確認のまま作成しません。' + RESUME
            self.journal.save(context, fingerprint, checkpoint, state='held', reason=reason)
            raise CreationHeld(reason) from None

    def _page(self, module, fields, checkpoint):
        # モジュール・項目はスキーマ由来のAPI名だけ。ユーザーの名前をクエリに入れない。
        if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', value) for value in (module, *fields)):
            raise CreationHeld('外部の照合項目を確認できません')
        last = checkpoint['last_id']
        if not isinstance(last, str) or not last.isascii() or not last.isdigit():
            raise CreationHeld('外部の照合位置を確認できません')
        condition = f'id > {last}'
        if checkpoint['phase'] == 'delta':
            since = stamp(datetime.fromisoformat(checkpoint['since']))
            until = stamp(datetime.fromisoformat(checkpoint['until']))
            condition = f"((id > {last}) and ((Modified_Time >= '{since}') and (Modified_Time <= '{until}')))"
        query = f"select id,{','.join(fields)} from {module} where {condition} order by id asc limit 0,2000"
        base = re.sub(r'/crm/v\d+/?$', '/crm/v8', self.client._api_base_url)
        if base == self.client._api_base_url and not base.endswith('/crm/v8'):
            raise CreationHeld('Zohoの照合接続先を確認できません')
        response = self.client.request('POST', base + '/coql', json_body={'select_query': query}, idempotent=True)
        if response.status_code == 204:
            return [], False
        raise_for_error(response, ApiError)
        body = response.json()
        records, info = body.get('data'), body.get('info')
        if (not isinstance(records, list) or len(records) > 2000 or not isinstance(info, dict)
                or type(info.get('more_records')) is not bool):
            raise CreationHeld('外部の照合ページを確認できません')
        return records, info['more_records']


def title_matcher(db_key, field, title):
    expected = identity_hash(db_key, title)
    def matches(record):
        if not isinstance(record.get(field), str):
            raise CreationHeld('外部の名前を確認できません')
        return identity_hash(db_key, record[field]) == expected
    return matches


def contact_matcher(properties):
    surname, given = (properties.get('姓') or '').strip(), (properties.get('名') or '').strip()
    email = properties.get('メールアドレス')
    def matches(record):
        if any(record.get(field) is not None and not isinstance(record[field], str)
               for field in ('Last_Name', 'First_Name', 'Email')) or not isinstance(record.get('Last_Name'), str):
            raise CreationHeld('外部の連絡先を確認できません')
        return (((record.get('Last_Name') or '').strip() == surname and (record.get('First_Name') or '').strip() == given)
                or bool(email and (record.get('Email') or '').casefold() == email.casefold()))
    return matches
