"""保存量を減らしても、既存の日報・週報入力を変えない。"""
from copy import deepcopy
from datetime import date
from src.reports.durable.snapshot import report_snapshot
from src.api.notion_display import page_to_display_dict, project_page_to_mirror_record
from src.db_schema.registry import get_schema
from src.reports.batch import _build_daily_records, _build_weekly_project_records

class Directory:
    def resolve(self, identifier):
        raise AssertionError('埋込名が失われています')

def field(kind, value):
    return {'type':kind,kind:value}

def test_projection_preserves_all_daily_and_weekly_inputs():
    project={'id':'project-1','created_time':'2026-09-01T00:00:00Z','properties':{
        '案件名':field('title',[{'plain_text':'合成施設'}]),
        '営業ステータス':field('select',{'name':'契約'}),
        '確度':field('select',{'name':'A'}),
        '初期費用':field('number',12345),'月額費用':field('number',6789),
        '担当メンバー':field('people',[{'id':'user','name':'合成担当'}]),
        '次回アクション日':field('date',{'start':'2026-10-01'}),
        '契約日 / 予想契約日':field('date',{'start':'2026-09-28'}),
        '提案サービス':field('multi_select',[{'name':'合成サービス'}]),
        '作成日時':field('created_time','2026-09-01T00:00:00Z'),
        'メモ':field('rich_text',[{'plain_text':'不要な本文'*10000}])}}
    action={'id':'action-1','created_time':'2026-09-28T00:00:00Z','properties':{
        '商談回数・電話回数・メール回数（何回目）':field('title',[{'plain_text':'【電話】1回目'}]),
        '案件名':field('relation',[{'id':'project-1'}]),
        'アクション日':field('date',{'start':'2026-09-28'}),
        '担当営業':field('rollup',{'type':'array','array':[field('people',[{'id':'user','name':'合成担当'}])]}),
        'メモ':field('rich_text',[{'plain_text':'不要な本文'*10000}])}}
    originals=deepcopy([project,action])
    outputs=[]
    for compact in (False,True):
        p=report_snapshot('project',project) if compact else project
        a=report_snapshot('action',action) if compact else action
        ps=[project_page_to_mirror_record(p,Directory())[0]]
        acts=[page_to_display_dict(a,get_schema('action'))[0]]
        outputs.append((_build_daily_records(ps,acts),_build_weekly_project_records(ps,acts)))
    assert outputs[0]==outputs[1]
    daily_actions,daily_projects=outputs[1][0]
    weekly_projects=outputs[1][1]
    assert len(daily_actions)==len(daily_projects)==len(weekly_projects)==1
    assert daily_projects[0].assignee=='合成担当'
    assert daily_projects[0].initial_fee==12345
    assert weekly_projects[0].contract_date==date(2026,9,28)
    assert weekly_projects[0].total_contact_count==1
    assert [project,action]==originals
    assert len(str(report_snapshot('project',project)))<len(str(project))/10
    assert len(str(report_snapshot('action',action)))<len(str(action))/10


def test_disk_capacity_reason_does_not_expose_exception_text():
    from psycopg.errors import DiskFull
    from src.reports.durable.application import safe_failure_reason
    reason=safe_failure_reason('収集ページの保存',DiskFull('private'))
    assert reason.startswith('収集ページの保存でDB保存容量不足。')
    assert 'private' not in reason
