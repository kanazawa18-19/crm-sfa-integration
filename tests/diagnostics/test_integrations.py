"""外部連携の疎通診断のテスト（2026-08-31）。

固定したいのは4点。
1. **未設定と失敗を混ぜない。**未設定を異常として通知すると誤報になり、
   誤報を鳴らし続けると本物の通知も無視されるようになる。
2. **1つ落ちても残りを続ける。**「全部見る」のが目的なので、最初の失敗で止まると
   2つ目以降の状態が分からなくなる。
3. **スプレッドシートはシート名の実在まで見る。**認証が通ることと書き込み先があることは別。
4. **advisory lockは必ず解放する。**握ったままにすると本番の夜間バッチを止める。
"""

from __future__ import annotations

import pytest

from src.diagnostics import integrations
from src.diagnostics.integrations import (
    FAILED,
    NOT_CONFIGURED,
    OK,
    ProbeResult,
    run_integration_diagnostics,
)


def test_未設定と失敗を別のキーに分ける() -> None:
    """`failed`には対処が要るものだけが入り、未設定は含まれない。"""
    probes = (
        ("a", lambda: ProbeResult("a", OK)),
        ("b", lambda: ProbeResult("b", NOT_CONFIGURED, detail="環境変数が無い")),
        ("c", lambda: ProbeResult("c", FAILED, detail="繋がらない")),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(integrations, "PROBES", probes)
        report = run_integration_diagnostics()

    assert report["ok"] == ["a"]
    assert report["not_configured"] == ["b"]
    assert report["failed"] == ["c"]


def test_1つの診断が例外を投げても他の診断は続行する() -> None:
    """診断は「全部見る」のが目的なので、途中の例外で打ち切らない。"""

    def 落ちる() -> ProbeResult:
        raise RuntimeError("接続できません")

    probes = (
        ("落ちる方", 落ちる),
        ("生きてる方", lambda: ProbeResult("生きてる方", OK)),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(integrations, "PROBES", probes)
        report = run_integration_diagnostics()

    assert report["failed"] == ["落ちる方"]
    assert report["ok"] == ["生きてる方"]
    # 例外の種類と本文が結果に残ること（ログを見に行かなくても原因が分かるように）。
    落ちた = next(r for r in report["results"] if r["name"] == "落ちる方")
    assert "RuntimeError" in 落ちた["detail"]
    assert "接続できません" in 落ちた["detail"]


def test_onlyで診断対象を絞れる() -> None:
    probes = (
        ("a", lambda: ProbeResult("a", OK)),
        ("b", lambda: ProbeResult("b", OK)),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(integrations, "PROBES", probes)
        report = run_integration_diagnostics(only=("b",))

    assert [r["name"] for r in report["results"]] == ["b"]


def test_全ての診断結果に所要時間が入る() -> None:
    """`rc=0`でも想定より遅ければどこかで空回りしている。所要時間を必ず残す。"""
    probes = (("a", lambda: ProbeResult("a", OK)),)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(integrations, "PROBES", probes)
        report = run_integration_diagnostics()

    assert "elapsed_ms" in report["results"][0]
    assert report["results"][0]["elapsed_ms"] >= 0


class _シート名を返すだけのクライアント:
    def __init__(self, names: list[str], row_counts: dict[str, int] | None = None) -> None:
        self._names = names
        self._row_counts = row_counts or {}

    def list_sheet_names(self) -> tuple[str, list[str]]:
        return "テスト用スプレッドシート", self._names

    def count_rows(self, sheets: list[str]) -> dict[str, int]:
        # 指定が無いシートは「ヘッダ＋データ2件」の想定にする。
        return {name: self._row_counts.get(name, 3) for name in sheets}


def _スプレッドシート診断(
    names: list[str],
    monkeypatch: pytest.MonkeyPatch,
    row_counts: dict[str, int] | None = None,
) -> ProbeResult:
    monkeypatch.setattr(
        "src.sync_engine.clients.spreadsheet_client.HttpSpreadsheetClient",
        lambda **kwargs: _シート名を返すだけのクライアント(names, row_counts),
    )
    return integrations.probe_spreadsheet()


def test_同期先のシートが全て実在すればok(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.db_schema.registry import ALL_SCHEMAS

    全シート名 = [schema.spreadsheet_sheet_name for schema in ALL_SCHEMAS]
    result = _スプレッドシート診断(全シート名 + ["関係ないシート"], monkeypatch)

    assert result.status == OK


def test_同期先のシートが1枚でも欠けていれば失敗として報告する(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """認証が通っていてもシート名が変わっていれば同期は静かに失敗し続ける。
    そこを「ok」と言わないことがこの診断の存在意義。"""
    from src.db_schema.registry import ALL_SCHEMAS

    全シート名 = [schema.spreadsheet_sheet_name for schema in ALL_SCHEMAS]
    欠けた = 全シート名[1:]
    result = _スプレッドシート診断(欠けた, monkeypatch)

    assert result.status == FAILED
    assert 全シート名[0] in result.detail


def test_スプレッドシートの認証情報が無ければ未設定として扱う(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def 構築時に落ちる(**kwargs: object) -> None:
        raise ValueError("SPREADSHEET_ID environment variable ... is required but not set")

    monkeypatch.setattr(
        "src.sync_engine.clients.spreadsheet_client.HttpSpreadsheetClient", 構築時に落ちる
    )
    result = integrations.probe_spreadsheet()

    assert result.status == NOT_CONFIGURED


def test_シートは在るがデータが0件なら失敗として報告する(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-08-31に実際に起きた状態。認証も通りシートも在るのに、全シートが
    ヘッダ1行のままだった。到達確認だけでは「ok」と言ってしまうため、行数まで数える。"""
    from src.db_schema.registry import ALL_SCHEMAS

    全シート名 = [schema.spreadsheet_sheet_name for schema in ALL_SCHEMAS]
    ヘッダのみ = {name: 1 for name in 全シート名}
    result = _スプレッドシート診断(全シート名, monkeypatch, row_counts=ヘッダのみ)

    assert result.status == FAILED
    assert "データが1件も無いシート" in result.detail
    assert result.extra["row_counts"] == ヘッダのみ


# --- 行の新規作成フラグの実効値 -------------------------------------------------------------
#
# Vercelでは`SPREADSHEET_ROW_CREATION_*`がSensitive扱いで**現在値を読み戻せない**。
# 値が分からないまま上書きすると、旧値のdb_keyを黙って無効化しうる。
# ここで固定したいのは3点。
# 1. **実行中のプロセスがどう解釈しているか**を返すこと（本番の判定と同じ経路を通る）。
# 2. **環境変数に書かれていた値そのものは、形に関わらず1つも返さないこと。**
#    「db_keyらしい形なら安全」は成立しない（鍵はその条件を素通りする）。
# 3. **綴り違いを「意図したOFF」と混ぜないこと。** 混ぜると設定ミスを正常として見送る。


def test_フラグがOFFなら未設定として返す(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定OFFは意図した状態なので、異常（failed）にはしない。"""
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_ENABLED", "false")
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", "client_master")

    result = integrations.probe_spreadsheet_row_creation()

    assert result.status == NOT_CONFIGURED
    assert result.extra["flag_enabled"] is False
    assert result.extra["enabled_db_keys"] == []


def test_フラグ未設定でも保存されているdb_keyは読める(monkeypatch: pytest.MonkeyPatch) -> None:
    """**上書き前に旧値を確かめる**のがこの診断の一番の用途。OFFの間も読めないと困る。"""
    monkeypatch.delenv("SPREADSHEET_ROW_CREATION_ENABLED", raising=False)
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", "client_master,product")

    result = integrations.probe_spreadsheet_row_creation()

    assert result.status == NOT_CONFIGURED
    assert result.extra["flag_state"] == "unset"
    assert result.extra["configured_db_keys"] == ["client_master", "product"]
    # 実際には1件も書けないので、有効なdb_keyは空のまま。
    assert result.extra["enabled_db_keys"] == []


def test_フラグの綴り違いは意図したOFFと分けて失敗にする(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TURE`は「意図的に無効化」ではなく設定ミス。not_configuredに混ぜると見送ってしまう。"""
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_ENABLED", "TURE")
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", "client_master")

    result = integrations.probe_spreadsheet_row_creation()

    assert result.status == FAILED
    assert result.extra["flag_state"] == "invalid"
    # 本番の挙動は今までどおりOFF側に倒れる（安全側）。
    assert result.extra["enabled_db_keys"] == []


def test_大文字のTRUEや前後の空白は今までどおり有効(monkeypatch: pytest.MonkeyPatch) -> None:
    """既存の本番挙動を変えていないことの確認。"""
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_ENABLED", "  TRUE  ")
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", " client_master , product ")

    result = integrations.probe_spreadsheet_row_creation()

    assert result.status == OK
    assert result.extra["enabled_db_keys"] == ["client_master", "product"]
    assert result.extra["per_db"]["chain"] is False


def test_フラグだけ立っていてdb_keyが空なら失敗として報告する(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1件も書けない状態は「有効にしたつもり」との食い違いなので通知したい。"""
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_ENABLED", "true")
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", "")

    result = integrations.probe_spreadsheet_row_creation()

    assert result.status == FAILED
    assert "SPREADSHEET_ROW_CREATION_DB_KEYS" in result.detail


def test_ワイルドカードなら全db_keyが有効になる(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_ENABLED", "true")
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", "*")

    result = integrations.probe_spreadsheet_row_creation()

    assert result.status == OK
    assert result.extra["wildcard"] is True
    assert all(result.extra["per_db"].values())
    # 「今後追加されるDBも自動で対象になる」ことは黙っていないで警告に出す。
    assert any("追加されるDB" in w for w in result.extra["warnings"])


@pytest.mark.parametrize(
    "db_keys",
    [
        "client_master,prodcut",
        "client_master,abcdef0123456789abcdef0123456789",
        "client_master,secret-key-123456",
        "*,abcdef0123456789abcdef0123456789",
        "client_master;貼り間違えた 何かの値",
    ],
)
def test_環境変数に書かれた未知の値は形に関わらず1つも返さない(
    monkeypatch: pytest.MonkeyPatch, db_keys: str
) -> None:
    """**「db_keyらしい形なら安全」は成立しない。** 鍵はその条件を素通りする。

    2026-09-02、ChatGPTがBLOCKERとして指摘し、Geminiも同じ箇所を独立に指摘した。
    正規表現を厳しくするのではなく、**未知の値を返さないこと自体**を防御線にする。
    """
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_ENABLED", "true")
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", db_keys)

    result = integrations.probe_spreadsheet_row_creation()
    見えている全文 = str(result.as_dict())

    assert result.extra["unknown_db_keys_count"] >= 1
    for 未知の値 in ("prodcut", "abcdef0123456789", "secret", "貼り間違えた"):
        assert 未知の値 not in 見えている全文
    # 既知のdb_keyは伏せない（伏せると上書き前の確認ができなくなる）。
    assert "client_master" in 見えている全文 or result.extra["wildcard"]


def test_未知の値があってもstatusは実効性だけで決める(monkeypatch: pytest.MonkeyPatch) -> None:
    """statusに「設定の綺麗さ」を混ぜない。

    `client_master`は現に書けるのに、綴り違いが1つ混じっただけで`failed`にすると、
    ダッシュボードが「連携が落ちた」と鳴る。実害の無いものを異常と呼ぶと誤報になり、
    誤報を鳴らし続けると本物の通知も無視されるようになる。
    気付く手段は`warnings`に残すので失わない。
    """
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_ENABLED", "true")
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", "client_master,prodcut")

    result = integrations.probe_spreadsheet_row_creation()

    assert result.status == OK
    assert result.extra["enabled_db_keys"] == ["client_master"]
    assert result.extra["unknown_db_keys_count"] == 1
    assert any("当たらない指定" in w for w in result.extra["warnings"])
    # 警告はdetailにも出す（statusとdetailだけ見る人が気付けるように）。
    assert "当たらない指定" in result.detail


def test_書けると言い切らない(monkeypatch: pytest.MonkeyPatch) -> None:
    """この診断が見ているのは設定だけ。認証やシートの実在は`spreadsheet`側の仕事。"""
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_ENABLED", "true")
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", "client_master")

    result = integrations.probe_spreadsheet_row_creation()

    assert result.detail.startswith("設定上、")


def test_extraのキーは状況によらず同じ顔ぶれになる(monkeypatch: pytest.MonkeyPatch) -> None:
    """読む側が毎回キーの有無を確かめずに済むようにする。"""
    期待 = {
        "flag_state",
        "flag_enabled",
        "configured_db_keys",
        "unknown_db_keys_count",
        "wildcard",
        "per_db",
        "enabled_db_keys",
        "warnings",
    }
    for 有効, キー一覧 in (("true", "client_master"), ("false", ""), ("TURE", "*,prodcut")):
        monkeypatch.setenv("SPREADSHEET_ROW_CREATION_ENABLED", 有効)
        monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", キー一覧)
        assert set(integrations.probe_spreadsheet_row_creation().extra) == 期待


def test_診断が見ているdb_keyは実際の同期対象と同じ集合(monkeypatch: pytest.MonkeyPatch) -> None:
    """**`ALL_SCHEMAS`に無いdb_keyが実行時に渡されない**ことを前提にしている。

    その前提が崩れると、「診断では未知のキー、本番のガードでは許可」という
    一番まずいズレが起きる（2026-09-02、ChatGPT指摘）。本番の配線が
    `ALL_SCHEMAS`から組まれていることをここで固定する。
    """
    from src.db_schema.registry import ALL_SCHEMAS, SCHEMAS_BY_KEY

    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_ENABLED", "true")
    monkeypatch.setenv("SPREADSHEET_ROW_CREATION_DB_KEYS", "*")

    result = integrations.probe_spreadsheet_row_creation()

    assert set(result.extra["per_db"]) == set(SCHEMAS_BY_KEY)
    assert set(result.extra["per_db"]) == {schema.key for schema in ALL_SCHEMAS}


def test_本物のPROBESを差し替えずに実行できる() -> None:
    """PROBESへの結線ミス（登録漏れ・タプルの書き方ミス・import時エラー）を拾う。"""
    report = run_integration_diagnostics(only=("spreadsheet_row_creation",))

    assert [r["name"] for r in report["results"]] == ["spreadsheet_row_creation"]
    assert "spreadsheet_row_creation" in {name for name, _ in integrations.PROBES}
