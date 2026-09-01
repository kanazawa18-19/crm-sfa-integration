"""全DBを順に流すバックフィルドライバ（2026-09-01）。

このドライバは「2026-08-31→09-01の実行でMacがスリープしてDNSが引けなくなり、
取引先マスターが8,482件失敗して後続2つは0秒で空振りした」という実害を受けて書かれた。
**rc=0を信用しない・失敗したら流し直す**という運用の作法そのものなので、
分岐ロジックにはテストを付ける（QAレビューWARN対応）。

ネットワーク待機とcaffeinateの実起動はE2E寄りなのでテストしない。
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts import backfill_spreadsheet_all as driver


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """待ち時間とネットワーク確認を潰す（テストを実時間で待たせない）。"""
    monkeypatch.setattr(driver.time, "sleep", lambda _s: None)
    monkeypatch.setattr(driver, "_wait_for_network", lambda: True)


def _record_calls(monkeypatch: pytest.MonkeyPatch, rcs: list[int]) -> list[list[str]]:
    """`subprocess.call`を差し替え、`rcs`を順に返す。実行されたコマンドを記録して返す。"""
    calls: list[list[str]] = []
    queue = list(rcs)

    def _call(command: list[str], **kwargs: Any) -> int:
        calls.append(command)
        return queue.pop(0) if queue else 0

    monkeypatch.setattr(driver.subprocess, "call", _call)
    return calls


# --- _run_one: 流し直しの条件 ---------------------------------------------------------


def test_stops_immediately_when_the_run_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_calls(monkeypatch, [0])

    rc, attempts = driver._run_one("product", apply=True, env={})

    assert (rc, attempts) == (0, 1)
    assert len(calls) == 1, "成功したのに流し直している"


def test_retries_up_to_the_limit_and_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """**失敗したら流し直す。** ただし無限には粘らない。"""
    calls = _record_calls(monkeypatch, [1] * driver._MAX_ATTEMPTS)

    rc, attempts = driver._run_one("client_master", apply=True, env={})

    assert rc == 1
    assert attempts == driver._MAX_ATTEMPTS
    assert len(calls) == driver._MAX_ATTEMPTS


def test_succeeds_on_a_later_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """一晩流すとネットワークが落ちる。2回目で通るのが想定どおりの姿。"""
    calls = _record_calls(monkeypatch, [1, 1, 0])

    rc, attempts = driver._run_one("contact", apply=True, env={})

    assert (rc, attempts) == (0, 3)
    assert len(calls) == 3


def test_does_not_retry_a_failure_that_rerunning_cannot_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「対象がちょうど1万件」は人が数え直すまで何度流しても同じ。粘らない。"""
    calls = _record_calls(monkeypatch, [driver._RC_DO_NOT_RETRY])

    rc, attempts = driver._run_one("project", apply=True, env={})

    assert rc == driver._RC_DO_NOT_RETRY
    assert attempts == 1
    assert len(calls) == 1, "流し直しても直らない失敗でリトライを回している"


def test_dry_run_does_not_pass_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_calls(monkeypatch, [0])

    driver._run_one("product", apply=False, env={})

    assert "--apply" not in calls[0], "試算のつもりで書き込もうとしている"


def test_apply_passes_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_calls(monkeypatch, [0])

    driver._run_one("product", apply=True, env={})

    assert "--apply" in calls[0]


# --- main: 全体の進み方と終了コード ---------------------------------------------------


def test_runs_every_db_in_order_and_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(driver, "_load_env", lambda: {})
    monkeypatch.setattr(
        driver, "_run_one", lambda db_key, **kw: (seen.append(db_key), (0, 1))[1]
    )

    rc = driver.main(["--dry-run"])

    assert rc == 0
    assert seen == list(driver.DEFAULT_DB_KEYS), "既定の順（件数の少ない順）で回っていない"


def test_one_failed_db_does_not_stop_the_rest_but_fails_overall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**1つ落ちても後続は流す。ただし全体は失敗として返す。**

    前回はここが無く、取引先マスターが落ちた時点で後続2つが空振りした。
    """
    seen: list[str] = []
    monkeypatch.setattr(driver, "_load_env", lambda: {})

    def _run_one(db_key: str, **kwargs: Any) -> tuple[int, int]:
        seen.append(db_key)
        return (1, 5) if db_key == "client_master" else (0, 1)

    monkeypatch.setattr(driver, "_run_one", _run_one)

    rc = driver.main(["--dry-run"])

    assert rc == 1, "失敗が残っているのに成功として返している"
    assert seen == list(driver.DEFAULT_DB_KEYS), "失敗した時点で後続をやめている"


def test_unknown_db_key_is_rejected_before_doing_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        driver, "_load_env", lambda: pytest.fail("知らないdb_keyなのに実行しようとした")
    )

    assert driver.main(["--dry-run", "--db-keys", "product", "nonexistent"]) == 2


def test_apply_and_dry_run_are_both_required_and_mutually_exclusive() -> None:
    """うっかり素で流して書き込む／何もしないを、構造的に防いでいること。"""
    with pytest.raises(SystemExit):
        driver.main([])
    with pytest.raises(SystemExit):
        driver.main(["--apply", "--dry-run"])


# --- _load_env: 認証情報の扱い ---------------------------------------------------------


def test_shell_exported_values_win_over_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """**既にexportされている値を`config/.env`で上書きしない**（`setdefault`の意図）。

    ここが逆転すると、シェルで指定したつもりの向き先が黙って別の環境に化ける。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        'NOTION_API_KEY="from-file"\nSPREADSHEET_ID="sheet-from-file"\n', encoding="utf-8"
    )
    monkeypatch.setattr(driver.os.path, "exists", lambda p: True)
    monkeypatch.setattr(driver.os.path, "join", lambda *parts: str(env_file))
    monkeypatch.setenv("NOTION_API_KEY", "from-shell")

    env = driver._load_env()

    assert env["NOTION_API_KEY"] == "from-shell"
    assert env["SPREADSHEET_ID"] == "sheet-from-file"
    assert env["SYNC_ID_MAPPING_NOTION_API_KEY"] == "from-shell"
    assert env["SYNC_ID_MAPPING_BACKEND"] == "notion"


def test_missing_credentials_stop_the_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """認証情報が無いまま流し始めない（全DBぶんリトライを空回りさせない）。"""
    env_file = tmp_path / ".env"
    env_file.write_text("SPREADSHEET_ID=\"only-this\"\n", encoding="utf-8")
    monkeypatch.setattr(driver.os.path, "exists", lambda p: True)
    monkeypatch.setattr(driver.os.path, "join", lambda *parts: str(env_file))
    monkeypatch.delenv("NOTION_API_KEY", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        driver._load_env()

    assert excinfo.value.code == 2


# --- caffeinate の掛け直し -------------------------------------------------------------


def test_apply_wraps_itself_in_caffeinate_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """一晩流すのでスリープさせない。ただし**二重ラップしない**こと。"""
    monkeypatch.setattr(driver.sys, "platform", "darwin")
    monkeypatch.delenv("_BACKFILL_UNDER_CAFFEINATE", raising=False)
    monkeypatch.setattr(
        driver, "_load_env", lambda: pytest.fail("caffeinateで包む前に本体が走っている")
    )
    wrapped: list[list[str]] = []
    monkeypatch.setattr(
        driver.subprocess, "call", lambda cmd, **kw: (wrapped.append(cmd), 0)[1]
    )

    rc = driver.main(["--apply", "--db-keys", "product"])

    assert rc == 0
    assert wrapped and wrapped[0][0] == "/usr/bin/caffeinate"


def test_does_not_wrap_again_once_already_under_caffeinate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """再exec のガードが効いていること（効かないと無限に自分を起動し続ける）。"""
    monkeypatch.setattr(driver.sys, "platform", "darwin")
    monkeypatch.setenv("_BACKFILL_UNDER_CAFFEINATE", "1")
    monkeypatch.setattr(driver, "_load_env", lambda: {})
    monkeypatch.setattr(driver, "_run_one", lambda db_key, **kw: (0, 1))
    monkeypatch.setattr(
        driver.subprocess, "call", lambda cmd, **kw: pytest.fail("二重にcaffeinateで包んでいる")
    )

    assert driver.main(["--apply", "--db-keys", "product"]) == 0


def test_no_caffeinate_flag_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver.sys, "platform", "darwin")
    monkeypatch.delenv("_BACKFILL_UNDER_CAFFEINATE", raising=False)
    monkeypatch.setattr(driver, "_load_env", lambda: {})
    monkeypatch.setattr(driver, "_run_one", lambda db_key, **kw: (0, 1))
    monkeypatch.setattr(
        driver.subprocess, "call", lambda cmd, **kw: pytest.fail("--no-caffeinate が効いていない")
    )

    assert driver.main(["--apply", "--no-caffeinate", "--db-keys", "product"]) == 0
