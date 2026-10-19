"""保存前後の確認と、途切れた配送の再開。"""
import time
from contextlib import ExitStack
from src.audit_log.actor_context import set_actor
from src.contact_name_job.repository import record_lock
from src.contact_affiliation_job.domain import decide
from src.contact_affiliation_job.repository import Repository
from src.contact_affiliation_job.gateway import JobGateway
from src.email_review.domain import normalized, Conflict


class DecisionHold(ValueError):
    """既知の判断保留。通信障害とは分け、他の対象を継続する。"""



def process(item, repository, conn, gateway, dry_run):
    key = item['notionId']
    plan = item['decision']
    resuming = item['state'] in ('writing', 'delivery_pending')
    contact = gateway.live('contact', key)
    # 配送待ちは一度保存実読を終えた状態。空への変更は人の解除として尊重する。
    if item['state'] == 'delivery_pending' and not contact.get('取引先マスター'):
        repository.save(conn, key, 'released', {**plan, 'reason': '保存確認後に所属が解除されました。自動では戻しません'})
        return 'released'
    if not resuming:
        if contact.get('取引先マスター'):
            repository.save(conn, key, 'existing', {'reason': '既存所属あり'})
            return 'existing'
        if not contact.get('メールアドレス') or contact.get('人事異動フラグ'):
            plan = decide(contact, [], [])
        else:
            rows, masters = gateway.collect(key, contact['メールアドレス'].strip().casefold(), repository)
            plan = decide(contact, rows, masters)
        if plan['state'] == 'ready' and dry_run:
            company = gateway.live('client_master', plan['company_id'])
            fresh = gateway.live('contact', key)
            if normalized(company.get('取引先名') or '') != normalized(plan['company_name']):
                raise DecisionHold('取引先名が変わりました')
            if (str(fresh.get('メールアドレス') or '').strip().casefold() != plan['email']
                    or fresh.get('取引先マスター') or fresh.get('人事異動フラグ')):
                raise DecisionHold('本人または所属情報が変わりました')
            if {m['id'] for m in gateway.contacts([plan['email']])} != {key}:
                raise DecisionHold('同じメールの連絡先が一意ではありません')
        if plan['state'] != 'ready' or dry_run:
            state = 'candidate' if plan['state'] == 'ready' else 'held'
            repository.save(conn, key, state, plan)
            return state
    if dry_run:
        repository.save(conn, key, item['state'], plan)
        return item['state']  # 設定変更中は延期し、同じ行をループしない。
    company_id = plan['company_id']
    email = plan['email']
    patch = {'取引先マスター': [company_id]}
    with set_actor('manual', '本人承認：独立した2原本による連絡先の所属補完'):
        with ExitStack() as stack:
            checks = [stack.enter_context(record_lock(kind, identifier)) for kind, identifier in
                      sorted({('contact', key), ('contact', 'email:' + email), ('client_master', company_id)})]
            def check():
                for verify in checks:
                    verify()
                conn.execute('SELECT 1')
            current = gateway.live('contact', key)
            company = gateway.live('client_master', company_id)
            if normalized(company.get('取引先名') or '') != normalized(plan['company_name']):
                raise DecisionHold('取引先名が変わりました')
            if str(current.get('メールアドレス') or '').strip().casefold() != email or current.get('人事異動フラグ'):
                raise DecisionHold('本人または異動情報が変わりました')
            matches = gateway.contacts([email])
            if {m['id'] for m in matches} != {key}:
                raise DecisionHold('同じメールの連絡先が一意ではありません')
            affiliation = current.get('取引先マスター') or []
            if affiliation and (not resuming or set(affiliation) != {company_id}):
                repository.save(conn, key, 'held', {**plan, 'reason': '既存所属が変わりました'})
                return 'held'
            if not affiliation and item['state'] == 'delivery_pending':
                repository.save(conn, key, 'released', {**plan, 'reason': '配送再開前に所属が解除されました。自動では戻しません'})
                return 'released'
            if not affiliation:
                # 遅延・再開中に追加された矛盾も書込み直前に確認する。
                rows, masters = gateway.collect(key, email, repository)
                renewed = decide(current, rows, masters)
                if renewed.get('company_id') != company_id or renewed['state'] != 'ready':
                    repository.save(conn, key, 'held', renewed)
                    return 'held'
                plan = renewed
                repository.save(conn, key, 'writing', plan)
                client = gateway.client('contact')
                def before_write():
                    check()
                    fresh = gateway.live('contact', key)
                    if (fresh.get('取引先マスター') or fresh.get('メールアドレス') != current.get('メールアドレス')
                            or fresh.get('人事異動フラグ') or fresh.get('_last_edited_time') != current.get('_last_edited_time')):
                        raise DecisionHold('書込み直前に連絡先が変わりました')
                    gateway.live('client_master', company_id)
                client.before_write = before_write
                check()
                # HTTP再試行は無効。未知の結果はwritingから実読で回復する。
                client.update_page(key, patch)
            saved = gateway.live('contact', key)
            if set(saved.get('取引先マスター') or []) != {company_id} or str(saved.get('メールアドレス') or '').strip().casefold() != email:
                raise DecisionHold('保存後の所属・本人確認が一致しません')
            check()
            repository.save(conn, key, 'delivery_pending', plan)
        # dispatcherも同じrecord_lockを取るため、原本ロックを解放してから配送する。
        delivered = gateway.dispatch('contact', key, patch, expected_email=email)
        state = 'verified' if delivered == 'delivered' else 'delivery_pending'
        repository.save(conn, key, state, plan)
        return state


def run(*, repository=None, gateway=None, clock=time.monotonic, budget=40, discover_only=False):
    repository = repository or Repository()
    counts = {}
    with repository.locked() as conn:
        if conn is None:
            return {'status': 'busy'}
        config = repository.config(conn)
        if not config['enabled']:
            return {'status': 'paused'}
        conn.execute('UPDATE "ContactAffiliationJob" SET "lastRunAt"=now(),"lastError"=NULL WHERE id=\'main\'')
        gateway = gateway or JobGateway()
        started = clock()
        try:
            prioritized = repository.prioritize_mail(conn)
            pages, cursor = gateway.discover(config['discovery'])
            repository.discovery(conn, pages, cursor)
            while not discover_only and clock() - started < budget:
                item = repository.next(conn)
                if not item:
                    break
                try:
                    state = process(item, repository, conn, gateway, config['dryRun'])
                except (DecisionHold, Conflict) as exc:
                    latest = conn.execute('SELECT state,decision FROM "ContactAffiliationItem" WHERE "notionId"=%s', (item['notionId'],)).fetchone()
                    state = latest['state'] if latest['state'] in ('writing', 'delivery_pending') else 'held'
                    repository.save(conn, item['notionId'], state, {**latest['decision'], 'reason': str(exc)})
                except Exception as exc:
                    # writing/delivery_pendingを失敗で上書きしない。外部成功後の切断を回復できる。
                    conn.execute('''UPDATE "ContactAffiliationItem" SET "errorType"=%s,
                        "nextAttemptAt"=now()+interval '1 hour' WHERE "notionId"=%s''',
                                 (type(exc).__name__, item['notionId']))
                    raise
                counts[state] = counts.get(state, 0) + 1
            from psycopg.types.json import Jsonb
            duration_ms = round((clock() - started) * 1000)
            metrics = {'last_duration_ms': duration_ms, 'last_discovered': len(pages)}
            if not discover_only:
                metrics.update(last_evaluated=sum(counts.values()),
                    evaluated_per_minute=round(sum(counts.values()) * 60000 / max(duration_ms, 1), 2))
            conn.execute('UPDATE "ContactAffiliationJob" SET "lastSuccessAt"=now(),metrics=metrics || %s WHERE id=\'main\'', (Jsonb(metrics),))
        except Exception as exc:
            conn.execute('UPDATE "ContactAffiliationJob" SET "lastError"=%s WHERE id=\'main\'', (type(exc).__name__,))
            raise
    return {'status': 'ok', 'dry_run': config['dryRun'], 'counts': counts,
            'discovered': len(pages), 'discovery_complete': not cursor, 'prioritized': prioritized, 'duration_ms': round((clock() - started) * 1000)}
