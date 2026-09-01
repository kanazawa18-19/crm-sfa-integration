"""スプレッドシートのバックフィルを、全DBぶん順番に流すドライバ（2026-09-01）。

`scripts/backfill_spreadsheet_rows.py` は1DBぶんしか処理しない。全部を一晩で流すには
「順番に回す」「落ちたら流し直す」「ネットワークの復帰を待つ」の3つが要る。
前回はこれをその場限りのスクリプトでやってしまい、次のセッションで残っていなかった。
**一晩流す処理の運び方そのものが資産なので、リポジトリに置く。**

■ なぜやり直しなのか（2026-09-01）

前回のバックフィルは**古い1万件のリスト**で動いていた。IDマッピングDBの全件取得が
Notionの「1クエリ1万件」の壁で静かに打ち切られており（`has_more: false` を返すので
気づけない）、`db_key=client_master` は10,000件で頭打ちになっていた。実際は34,233件ある。
`src/sync_engine/clients/_notion_paging.py` で壁を越えたので、**対象件数が変わる**。

■ 一晩流す処理は「落ちる前提」で組む

2026-08-31→09-01 の実行は、Macがスリープして `api.notion.com` が引けなくなり、
取引先マスターが8,482件失敗、後続2つは0秒で落ちた（`Failed to resolve` が11,346回）。
**rc≠0 は正しく出ていたが、流し直しが無かったので後続が丸ごと空振りした。**

- `caffeinate -is` の下で動かす（このスクリプトが自分で掛け直す）
- 各DBは最大5回まで流し直す。バックフィル自体が冪等なので重複しない
- 流す前に名前解決の復帰を待つ（待たずに始めるとリトライ回数を無駄に使い切る）

■ 使い方

    # まず試算（書き込まない）。対象件数が1万で頭打ちになっていないかをここで見る
    .venv/bin/python scripts/backfill_spreadsheet_all.py --dry-run

    # 実行（全DB・件数の少ない順）
    .venv/bin/python scripts/backfill_spreadsheet_all.py --apply

    # 特定のDBだけ
    .venv/bin/python scripts/backfill_spreadsheet_all.py --apply --db-keys client_master project

**終わったらシートの実件数を必ず数えること。** スクリプトの「完了」表示だけを信用しない
（バックフィル後に実件数を目視確認する、は`~/notes`にも書いてある運用ルール）。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 件数の少ない順。途中で止まっても、埋まったところまでは残る（冪等）。
DEFAULT_DB_KEYS = ("product", "chain", "contact", "client_master", "project", "action")

#: 1DBあたりの流し直しの上限。
_MAX_ATTEMPTS = 5

#: 流し直しの前に置く間隔（秒）。
_RETRY_WAIT_SECONDS = 60

#: 名前解決の復帰を待つ上限（秒）と、確認の間隔（秒）。
_NETWORK_WAIT_LIMIT_SECONDS = 3600
_NETWORK_POLL_SECONDS = 30

#: このバックフィルが到達できないと話にならないホスト。
_REQUIRED_HOSTS = ("api.notion.com", "sheets.googleapis.com")


def _load_env() -> dict[str, str]:
    """`config/.env` を読み、サブプロセスに渡す環境を組み立てる。

    既にシェルで export されている値を優先する（`setdefault`）。
    `config/.env` は `.gitignore` 済みで、リポジトリにもVaultにも入らない。
    """
    env = dict(os.environ)
    env_path = os.path.join(REPO, "config", ".env")
    if not os.path.exists(env_path):
        print(f"エラー: {env_path} がありません（認証情報の置き場所）", file=sys.stderr)
        raise SystemExit(2)
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip().strip('"'))

    missing = [k for k in ("NOTION_API_KEY", "SPREADSHEET_ID") if not env.get(k)]
    if missing:
        print(f"エラー: 環境変数が足りません: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)

    env["SYNC_ID_MAPPING_BACKEND"] = "notion"
    env["SYNC_ID_MAPPING_NOTION_API_KEY"] = env["NOTION_API_KEY"]
    # サブプロセスからも `src` を解決できるようにする（cwdだけでは足りない）。
    env["PYTHONPATH"] = REPO
    env["SPREADSHEET_ROW_CREATION_ENABLED"] = "true"
    return env


def _wait_for_network() -> bool:
    """必要なホストが引けるようになるまで待つ。復帰したらTrue、諦めたらFalse。"""
    waited = 0
    announced = False
    while waited <= _NETWORK_WAIT_LIMIT_SECONDS:
        try:
            for host in _REQUIRED_HOSTS:
                socket.getaddrinfo(host, 443)
            if announced:
                print(f"ネットワークが復帰した（{waited}秒待った）", flush=True)
            return True
        except OSError:
            if not announced:
                print("ネットワークが落ちている。復帰を待つ", flush=True)
                announced = True
            time.sleep(_NETWORK_POLL_SECONDS)
            waited += _NETWORK_POLL_SECONDS
    print(
        f"ネットワークが{_NETWORK_WAIT_LIMIT_SECONDS}秒待っても復帰しなかった。"
        "このまま試すが、失敗する可能性が高い",
        flush=True,
    )
    return False


def _run_one(db_key: str, *, apply: bool, env: dict[str, str]) -> tuple[int, int]:
    """1DBを最大`_MAX_ATTEMPTS`回まで流す。`(最後のrc, 試行回数)`を返す。"""
    env = dict(env)
    env["SPREADSHEET_ROW_CREATION_DB_KEYS"] = db_key
    command = [
        os.path.join(REPO, ".venv", "bin", "python"),
        os.path.join("scripts", "backfill_spreadsheet_rows.py"),
        "--db-key",
        db_key,
    ]
    if apply:
        command.append("--apply")

    rc = 1
    attempt = 0
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        _wait_for_network()
        print(f"===== {db_key} 開始（{attempt}回目）=====", flush=True)
        started = time.monotonic()
        rc = subprocess.call(command, cwd=REPO, env=env)
        elapsed = int(time.monotonic() - started)
        print(f"===== {db_key} 終了 rc={rc} 所要={elapsed}秒 =====", flush=True)
        if rc == 0:
            break
        if attempt < _MAX_ATTEMPTS:
            print(f"{db_key} に失敗があった。{_RETRY_WAIT_SECONDS}秒待って流し直す", flush=True)
            time.sleep(_RETRY_WAIT_SECONDS)
    return rc, attempt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-keys",
        nargs="+",
        default=list(DEFAULT_DB_KEYS),
        help=f"対象のDB（既定: {' '.join(DEFAULT_DB_KEYS)}）",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true", help="実際に書き込む")
    group.add_argument("--dry-run", action="store_true", help="試算のみ（書き込まない）")
    parser.add_argument(
        "--no-caffeinate",
        action="store_true",
        help="caffeinate を掛け直さない（既に掛かっている場合や、Mac以外で動かす場合）",
    )
    args = parser.parse_args(argv)

    unknown = [k for k in args.db_keys if k not in DEFAULT_DB_KEYS]
    if unknown:
        print(f"エラー: 知らないdb_key: {', '.join(unknown)}", file=sys.stderr)
        return 2

    # **一晩流すのでスリープさせない。** caffeinate が防げるのは idle sleep だけで、
    # これだけでは足りない（2026-08-31・09-01の2晩ともDNSが引けなくなって止まった）ため、
    # 上の `_wait_for_network()` と流し直しと合わせて使う。
    if args.apply and not args.no_caffeinate and sys.platform == "darwin":
        if os.environ.get("_BACKFILL_UNDER_CAFFEINATE") != "1":
            env = dict(os.environ, _BACKFILL_UNDER_CAFFEINATE="1")
            print("caffeinate -is の下で流し直します", flush=True)
            return subprocess.call(
                ["/usr/bin/caffeinate", "-is", sys.executable, *sys.argv], env=env
            )

    env = _load_env()
    results: list[tuple[str, int, int]] = []
    for db_key in args.db_keys:
        rc, attempts = _run_one(db_key, apply=args.apply, env=env)
        results.append((db_key, rc, attempts))
        if rc != 0:
            print(f"{db_key} は{attempts}回とも失敗が残った。次のDBへ進む", flush=True)

    print("\n===== まとめ =====", flush=True)
    for db_key, rc, attempts in results:
        print(f"  {db_key:<14} {'OK  ' if rc == 0 else '失敗'} (rc={rc} / {attempts}回)", flush=True)
    failed = [db_key for db_key, rc, _ in results if rc != 0]
    if failed:
        print(f"\n失敗が残ったDB: {', '.join(failed)}", flush=True)
    print(
        "\n**「完了」表示だけを信用しない。** シートの実件数と IdMapping の行番号ありの件数を"
        "必ず突き合わせること。",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
