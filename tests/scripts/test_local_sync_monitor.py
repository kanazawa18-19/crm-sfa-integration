"""週次は必ず新しい全件調査を行い、プレビューだけ既存結果を使う。"""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts import run_local_sync_monitor as mod


@pytest.mark.parametrize('preview',[False,True])
def test_inventory_refreshes_unless_preview(tmp_path, monkeypatch, preview):
    state=tmp_path/'state';state.mkdir()
    previous=tmp_path/'previous'
    (state/'inventory.json').write_text(json.dumps({'directory':str(previous)}))
    monkeypatch.setattr(mod,'STATE',state);monkeypatch.setattr(mod,'ROOT',tmp_path)
    monkeypatch.setattr(mod.sys,'argv',['monitor','inventory']+(['--preview'] if preview else []))
    scans=[];reports=[];sent=[]
    monkeypatch.setattr(mod.subprocess,'run',lambda args,**kwargs: (scans.append((args,kwargs)) or SimpleNamespace(returncode=0)))
    monkeypatch.setattr(mod,'inventory_report',lambda directory: (reports.append(directory) or '点検結果'))
    monkeypatch.setattr(mod,'keychain',lambda name:'test-key')
    monkeypatch.setattr(mod,'send',sent.append)
    mod.main()
    if preview:
        assert scans==[] and reports==[previous] and sent==[]
    else:
        assert len(scans)==1 and scans[0][1]['timeout']==9900
        assert reports[0] != previous and 'weekly_inventory' in str(reports[0])
        assert sent==['点検結果']
