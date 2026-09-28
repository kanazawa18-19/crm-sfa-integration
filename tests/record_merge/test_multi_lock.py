from unittest.mock import Mock
import pytest
from src.sync_engine.record_sync_lock import acquire_record_sync_locks, RecordSyncBusy, lock_key


def test_many_locks_share_one_connection_and_release_all_on_partial_failure(monkeypatch):
    import src.sync_engine.record_sync_lock as module
    cursor=Mock(); cursor.fetchone.side_effect=[{'locked':True}, {'locked':False}]
    cursor.__enter__=Mock(return_value=cursor); cursor.__exit__=Mock(return_value=False)
    conn=Mock(); conn.cursor.return_value=cursor
    factory=Mock(return_value=conn); monkeypatch.setattr(module,'_connect_direct',factory)
    with pytest.raises(RecordSyncBusy):
        with acquire_record_sync_locks(None, [('project','z'),('action','a'),('action','a')]):
            pytest.fail('競合時には処理しない')
    factory.assert_called_once(); conn.close.assert_called_once()
    assert cursor.execute.call_args_list[0].args[1] == (lock_key('action','a'),)
    assert cursor.execute.call_args_list[1].args[1] == (lock_key('project','z'),)
