"""原本・内容・配送予約を分け、結果不明の追記は自動で繰り返さない。"""
from copy import deepcopy
from datetime import datetime, timezone
import os
import time

from src.attachment_sync.domain import Source, Held, MAX_BYTES, content_key, filename
from src.attachment_sync.repository import Repository
from src.contact_name_job.repository import record_lock


def process(repo, gateway, row, lock=record_lock):
    source = None
    try:
        source = Source(**row['data'])
        if source.size > MAX_BYTES:raise Held('添付は10MBを超えています')
        with gateway.source_guard(source) as source_owned, lock(source.db, source.record) as owned:
            def check():
                repo.check()
                source_owned()
                owned()
            check()
            data = gateway.validate_source(source)
            key, digest = content_key(source, data)
            if source.tool=='kintone' and (mirror:=repo.find_mirror(source,digest)):
                repo.mark_mirror(source,mirror)
                return 'mirrored'
            if row.get('assetId') and row['assetId'] != key:
                raise Held('受付後に添付内容が変わりました')
            asset = repo.asset(key)
            if not asset:
                asset = {'id': key, 'db':source.db, 'record':source.record, 'sha256': digest, 'name': filename(source.name),
                         'drive_id': gateway.reserve_drive_id(), 'phase': 'reserved', 'receipts': {}}
                repo.save_asset(key, asset)
            repo.finish(source.key, 'retry', key)
            allow_create = asset['phase'] == 'reserved'
            def before_create():
                asset['phase'] = 'writing'
                repo.save_asset(key, asset)
            asset['url'] = gateway.archive(asset, data, check, allow_create=allow_create,before_create=before_create)
            asset.update(phase='archived', archived=True)
            repo.save_asset(key, asset)
            for target in ('notion', 'zoho', 'kintone'):
                receipt = deepcopy(asset['receipts'].get(target, {}))
                def checkpoint(value):
                    asset['receipts'][target] = value
                    repo.save_asset(key, asset)
                # 各配送時にも原本内容を再照合する。メールの訂正は行ロックで待たせる。
                if content_key(source, gateway.validate_source(source))[0] != key:
                    raise Held('配送中に添付内容が変わりました')
                result = gateway.deliver(source, asset, target, receipt, checkpoint, check)
                # 予約時に保存した宛先を確定結果にも残す。
                result = {**asset['receipts'].get(target, {}), **result}
                checkpoint(result)
                if result['state'] == 'verified' and target != 'notion':
                    category = '添付ファイル'
                    field = gateway.config[source.db]['kintone_field'] if target == 'kintone' else ''
                    mirror = Source(target, source.db, source.record, category, result['record'],
                                    result['id'], asset['name'], 0, field=field)
                    repo.mark_mirror(mirror, key)
            repo.finish(source.key, 'verified', key)
            return 'verified'
    except Held as exc:
        repo.finish(source.key if source else row['id'], 'held', reason=str(exc))
        return 'held'
    except Exception as exc:
        # 外部応答・URL・個人情報は保存しない。予約はそのまま保持する。
        repo.finish(source.key if source else row['id'], 'retry' if source else 'held', reason=type(exc).__name__)
        return 'retry'


def discover_one(repo, gateway):
    progress = repo.state('progress')
    db = 'action' if progress.get('next_db') == 'action' else 'project'
    event = repo.next_event()
    if event:
        event_db, record = event['data']['db'], event['data']['record']
        try:
            for source in gateway.sources(event_db, record):
                repo.add(source)
            repo.put_state('error:'+event_db+':'+record,{})
        except Exception as exc:
            repo.put_state('error:'+event_db+':'+record,{'reason': str(exc) if isinstance(exc,Held) else type(exc).__name__})
        repo.complete_event(event)
    state = repo.state('scan:' + db)
    if not state.get('pending'):
        try:
            records, after = gateway.discover(db, state.get('after', {}))
            state = {'pending': records, 'after': after}
            repo.put_state('scan:' + db, state)
            repo.put_state('error:scan:'+db,{})
        except Exception as exc:
            repo.put_state('error:scan:'+db,{'reason':type(exc).__name__})
            state={**state,'pending':[]}
    if state['pending']:
        record = state['pending'][0]
        error_key = 'error:' + db + ':' + record
        try:
            for source in gateway.sources(db, record):
                repo.add(source)
            repo.put_state(error_key, {})
        except Exception as exc:
            # 一件の不備で全体を止めず、次巡回で再読。失敗を件数とIDで表示する。
            repo.put_state(error_key, {'reason': str(exc) if isinstance(exc, Held) else type(exc).__name__})
        state['pending'] = state['pending'][1:]
        repo.put_state('scan:' + db, state)
    progress['next_db'] = 'project' if db == 'action' else 'action'
    repo.put_state('progress', progress)
    mail_cursor = repo.state('scan:mail')
    rows = repo.mail_batch(mail_cursor)
    if rows:
        row = rows[0]
        error_key = 'error:mail:' + row['id']
        try:
            for source in gateway.mail_sources(row):
                repo.add(source)
            repo.put_state(error_key, {})
        except Exception as exc:
            repo.put_state(error_key, {'reason': str(exc) if isinstance(exc, Held) else type(exc).__name__})
        repo.put_state('scan:mail', {'at': row['updatedAt'].isoformat(), 'id': row['id'],
                                   'owner': row['ownerEmail'], 'message': row['gmailMessageId']})
    else:
        repo.put_state('scan:mail', {})


def run(*, dry_run=False):
    if not dry_run and os.environ.get('ATTACHMENT_SYNC_ENABLED') != 'true':
        return {'status': 'paused'}
    from src.attachment_sync.gateway import Gateway
    from src.attachment_sync.http import budget
    with budget(), Repository.locked() as repo:
        if repo is None:
            return {'status': 'busy'}
        gateway = Gateway()
        if dry_run:
            # 外部書込み・キュー変更なし。設定と最初の一件だけを試算する。
            gateway.preflight()
            preview = {}
            for db in ('project', 'action'):
                ids, _ = gateway.discover(db, {})
                preview[db] = len(gateway.sources(db, ids[0])) if ids else 0
            return {'status': 'preview', 'sample_attachment_counts': preview}
        gateway.preflight()
        started = time.monotonic()
        row = repo.pending()
        result = process(repo, gateway, row) if row else 'idle'
        if time.monotonic() - started < 120:
            discover_one(repo, gateway)
        progress = repo.state('progress')
        progress['last_run_at'] = datetime.now(timezone.utc).isoformat()
        progress['last_result'] = result
        repo.put_state('progress', progress)
        return {'status': result, **repo.status(), 'discovery_errors': repo.errors()}
