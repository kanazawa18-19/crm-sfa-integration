"""統合を1手順ずつ進め、再開時に現在値を照合する。永続化とAPIは注入する。"""
from __future__ import annotations

from src.record_merge.domain import MergeHeld, digest, resumable_value


class MergeService:
    def __init__(self, journal, gateway, locks):
        self.journal, self.gateway, self.locks = journal, gateway, locks

    def execute(self, operation_id, actor_id, *, read_only=False):
        """外部書込みの前に予約し、結果不明の本文追加は自動再送しない。"""
        job = self.journal.get_authorized(operation_id, actor_id)
        with self.locks(job):
            job = self.journal.get_authorized(operation_id, actor_id)
            if job['state'] == 'done':
                return job
            if job['state'] not in {'approved', 'running', 'held'}:
                raise MergeHeld('比較内容の承認が必要です')
            try:
                # 手順を増減させず、承認済みの元・先・子だけを扱う。
                self.gateway.verify_identity(job)
                if read_only:
                    # 停止中は外部へ書かず、全手順の実値が既に完了した場合だけ記録を回収する。
                    for step in job['steps']:
                        self.gateway.verify_completed(step)
                    self.gateway.verify_archive_ready(job)
                    for index in range(len(job['steps'])):
                        self.journal.complete_step(operation_id, index, actor_id, None)
                    self.journal.finish_with_aliases(operation_id, actor_id)
                    return self.journal.get_authorized(operation_id, actor_id)
                for index, step in enumerate(job['steps']):
                    status = self.journal.step_state(operation_id, index)
                    if status == 'done':
                        self.gateway.verify_completed(step)
                        continue
                    if step['kind'] == 'append_body':
                        if status == 'reserved':
                            try:
                                self.gateway.verify_completed(step)
                            except Exception:
                                raise MergeHeld('本文追加の結果が不明です。二重追加を避けて確認を待っています') from None
                            self.journal.complete_step(operation_id, index, actor_id, None)
                            continue
                        self.gateway.verify_before(step)
                        self.journal.reserve_step(operation_id, index, actor_id)
                        receipt = self.gateway.append_body(step)
                        self.gateway.verify_receipt(step, receipt)
                        self.journal.complete_step(operation_id, index, actor_id, receipt)
                        continue
                    current = self.gateway.read(step)
                    before = step['before']
                    if step['kind'] == 'relation' and 'intermediate' in step and set(current) == set(step['intermediate']):
                        # 承認済みの先への関連追加で、Notionが自動生成する中間状態。
                        before = current
                    write = resumable_value(current, before, step['desired'])
                    if write:
                        if step['kind'] == 'archive':
                            self.gateway.verify_archive_ready(job)
                        self.journal.reserve_step(operation_id, index, actor_id)
                        # readbackが失敗しても次回はdesiredとの一致から再開できる。
                        self.gateway.write(step)
                    if self.gateway.read(step) != step['desired']:
                        raise MergeHeld('統合結果の実値を確認できません')
                    self.journal.complete_step(operation_id, index, actor_id, None)
                self.gateway.verify_archive_ready(job)
                self.journal.finish_with_aliases(operation_id, actor_id)
            except Exception as exc:
                # 外部エラー本文に認証情報・顧客情報が含まれ得るので保存しない。
                reason = str(exc) if isinstance(exc, MergeHeld) else '外部処理または履歴保存を確認できません'
                self.journal.hold(operation_id, actor_id, reason)
                if isinstance(exc, MergeHeld):
                    raise
                raise MergeHeld(reason) from None
            return self.journal.get_authorized(operation_id, actor_id)


def validate_plan(snapshot, steps):
    """アーカイブを最後に限定し、対象外の任意書込みを禁止する。"""
    source, target = snapshot['sourceId'], snapshot['targetId']
    if source == target or not source or not target:
        raise MergeHeld('異なる2件を選んでください')
    if not steps or steps[-1]['kind'] != 'archive':
        raise MergeHeld('元を残すためアーカイブは最後に行ってください')
    allowed = {(snapshot['dbKey'], source), (snapshot['dbKey'], target)}
    allowed.update((child['dbKey'], child['id']) for child in snapshot['children'])
    for index, step in enumerate(steps):
        if (step['dbKey'], step['id']) not in allowed:
            raise MergeHeld('比較していない対象へは書き込めません')
        if step['kind'] not in {'properties', 'relation', 'append_body', 'archive'}:
            raise MergeHeld('未対応の統合手順です')
        if step['kind'] == 'archive' and (index != len(steps)-1 or step['id'] != source):
            raise MergeHeld('統合元以外はアーカイブできません')
        if step['kind'] in {'properties', 'append_body'} and step['id'] != target:
            raise MergeHeld('統合先以外の内容は変更できません')
    return digest({'snapshot': snapshot, 'steps': steps})
