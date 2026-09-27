"""行移動や空の同期キーを未登録の新規と混同しない。"""
from scripts.inventory_unmapped_hub_records import classify


def test_inventory_separates_identity_ambiguity():
    mappings=[{'notion_key':'AA-BB','spreadsheet_row':2,'kintone_id':None,'zoho_id':'z'},
              {'notion_key':'ccdd','spreadsheet_row':3,'kintone_id':None,'zoho_id':'y'}]
    pages=[{'id':'aabb'},{'id':'ccdd'},{'id':'eeff'}]
    cases=[
        ([{'row':2,'key':'aabb'}],'mapped'),
        ([{'row':2,'key':''}],'mapped_row_without_sync_key'),
        ([{'row':2,'key':'ccdd'}],'row_mapping_mismatch'),
        ([{'row':4,'key':'aabb'}],'mapped_key_at_other_row'),
        ([{'row':4,'key':'eeff'}],'unmapped'),
        ([{'row':4,'key':'missing'}],'sync_key_without_page'),
        ([{'row':4,'key':'aabb'},{'row':5,'key':'AA-BB'}],'duplicate_sync_key'),
    ]
    for rows,expected in cases:
        result=classify(pages,mappings,rows)
        assert result['notion_unmapped']==1
        assert result['sheet_classes']=={expected:len(rows)}
        assert len(result['sheet_review_rows'])==(0 if expected=='mapped' else len(rows))


def test_report_requires_six_fresh_databases():
    from datetime import datetime, timezone
    import pytest
    from scripts.report_hub_inventory import render_report
    from src.db_schema.registry import ALL_SCHEMAS
    now=datetime(2026,9,27,10,tzinfo=timezone.utc)
    reports=[dict(db=s.key,observed_at=now.isoformat(),notion_unmapped=0,notion_total=1,
                  sheet_nonempty_rows=1,sheet_classes={'mapped':1},notion_unmapped_pages=[],sheet_review_rows=[]) for s in ALL_SCHEMAS]
    assert '自動作成は行いません' in render_report(reports,now)
    with pytest.raises(ValueError):render_report(reports[:-1],now)
    reports[0]['observed_at']='2026-09-26T10:00:00+00:00'
    with pytest.raises(ValueError):render_report(reports,now)


def test_duplicate_mapping_is_not_hidden():
    mappings=[{'notion_key':'a','spreadsheet_row':2},{'notion_key':'b','spreadsheet_row':2}]
    result=classify([{'id':'a'},{'id':'b'}],mappings,[{'row':2,'key':'b'}])
    assert result['sheet_classes']=={'duplicate_mapping':1}
    assert result['duplicate_mapping_rows']==1
