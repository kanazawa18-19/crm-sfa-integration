from datetime import date
from src.calendar_sync.tasks import build_task_plan

PROJECT = '26d6f1e2-0000-0000-0000-000000000000'
TODAY = date(2026, 11, 25)


def plan(text, **kwargs):
    return build_task_plan({'次回アクション': text, 'AI推奨アクション日': '2026-11-25',
                            '担当メンバー': ['user'], **kwargs}, PROJECT, today=TODAY)


def test_箇条書きごとの明示日だけを使う():
    tasks = plan('- 提案 2026-11-26\n- 電話 2026年11月27日\n- 回答待ち').tasks
    assert [t.due for t in tasks] == ['2026-11-26', '2026-11-27', None]
    assert len({t.key for t in tasks}) == 3


def test_自然文を推測で細分化しない():
    task = plan('電話し、回答を受け取って提案する。\n社内にも共有する').tasks
    assert len(task) == 1
    assert task[0].due == '2026-11-25'


def test_順序や明示日変更でもキーが変わらない():
    first = plan('- 提案 2026-11-26\n- 電話 2026-11-27').tasks
    second = plan('- 電話 2026-12-03\n- 提案 2026-12-02').tasks
    assert first[0].key == second[1].key
    assert first[1].key == second[0].key


def test_同一本文の別期限は無言で統合しない():
    result = plan('- 電話 2026-11-26\n- 電話 2026-11-27')
    assert not result.tasks
    assert '識別子が重複' in result.holds[0]


def test_明示IDは同一本文の別タスクを区別する():
    tasks = plan('- [id:first] 電話 2026-11-26\n- [id:second] 電話 2026-11-27').tasks
    assert [task.key for task in tasks] == ['id-first', 'id-second']


def test_年省略や曖昧表現を推定しない():
    tasks = plan('- 電話 明日\n- 提案 11/26').tasks
    assert all(task.due is None for task in tasks)


def test_前置き付き箇条書きを一つへ潰さない():
    assert len(plan('以下の対応：\n- 電話 2026-11-26\n- 提案 2026-11-27').tasks) == 2
    result = plan('説明文も作業かもしれない\n- 電話\n- 提案')
    assert not result.tasks and result.holds


def test_不正日付の項目だけを保留する():
    result = plan('- 電話 2026-02-30\n- 提案 2026/12/1')
    assert len(result.tasks) == 1 and result.holds
    assert result.tasks[0].due == '2026-12-01'


def test_単一本文の明示日をAI日空欄の代用にしない():
    task = plan('電話 2026-11-26', AI推奨アクション日=None).tasks[0]
    assert task.due is None


def test_複数候補日は今日以降最短を使う():
    task = plan('- 電話 2026-11-24 または 2026-11-27\n- 共有').tasks[0]
    assert task.due == '2026-11-27'


def test_本文改名は新タスク():
    assert plan('電話').tasks[0].key != plan('提案').tasks[0].key


def test_箇条書きが一件に減っても期限を維持():
    initial = plan('- 電話 2026-11-26\n- 提案 2026-11-27').tasks[0]
    last = plan('- 電話 2026-11-26', AI推奨アクション日='2026-12-31').tasks[0]
    assert (last.key, last.due) == (initial.key, initial.due)


def test_日付に隣接する曜日の変化でキーを変えない():
    first = plan('- 電話 2026-11-26(木)').tasks[0]
    changed = plan('- 電話 2026-11-27（金曜日）').tasks[0]
    assert first.key == changed.key
    assert changed.due == '2026-11-27'


def test_壊れたISO末尾を日付候補にしない():
    task = plan('- 電話 2026-11-26Tbad').tasks[0]
    assert task.due is None
