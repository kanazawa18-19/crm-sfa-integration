"""各cronエンドポイントが読む環境変数を棚卸しする（2026-09-08、Cloud Run移行の第2段）。

Cloud Schedulerへ1本移すたびに「その1本が読む値だけ」をCloud Runへ足したい。
どれが要るかを毎回grepで数え直すと漏れるので、静的に辿って表にする。

    python3 scripts/cloud_run/list_cron_env.py

エントリのモジュールから `from src...` を再帰的に辿り、`os.environ[...]` /
`os.environ.get(...)` / `os.getenv(...)` に現れる名前を集める。

★ 出るのは「使いうる上限」。分岐で実際には読まないものも含む。
  逆に、定数を組み立てて渡す動的な参照は拾えないので、移行前に対象1本の
  ログで実際の失敗を見ること（この表だけを根拠にしない）。
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# vercel.json の crons 9本と、その入口が呼ぶモジュール（src/api/routes/cron.py 参照）。
ENTRY_MODULES = {
    "token-encryption-healthcheck": "src.api.token_encryption_healthcheck",
    "gmail-watch-renewal": "src.gmail_sync.watch_registration",
    "zoho-webhook-renewal": "src.sync_engine.zoho_watch_channel",
    "incident-digest": "src.incident_detection.notify",
    "project-mirror-reconcile": "src.project_mirror.sync",
    "relation-sync-reconcile": "src.relation_sync.sync",
    "gmail-sync": "src.gmail_sync.sync",
    "daily-batch": "src.reports.batch",
    "spreadsheet-outbox-drain": "src.sync_engine.spreadsheet_outbox_drain",
}

# 第1段で既にCloud Runへ入れてある値。差分だけを見たいので表から外す。
ALREADY_ON_CLOUD_RUN = {"DATABASE_URL", "DATABASE_URL_UNPOOLED", "DASHBOARD_API_TOKEN"}


def _module_file(module: str) -> pathlib.Path | None:
    as_file = REPO_ROOT / (module.replace(".", "/") + ".py")
    if as_file.exists():
        return as_file
    as_package = REPO_ROOT / module.replace(".", "/") / "__init__.py"
    return as_package if as_package.exists() else None


def _env_names(module: str, visited: set[str]) -> set[str]:
    if module in visited:
        return set()
    visited.add(module)

    path = _module_file(module)
    if path is None:
        return set()
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return set()

    names: set[str] = set()
    imported: set[str] = set()

    for node in ast.walk(tree):
        # os.environ.get("X") / os.getenv("X")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            first = node.args[0] if node.args else None
            if (
                node.func.attr in ("get", "getenv")
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and ast.unparse(node.func.value) in ("os.environ", "os", "environ")
            ):
                names.add(first.value)
        # os.environ["X"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and ast.unparse(node.value) == "os.environ"
            and isinstance(node.slice, ast.Constant)
        ):
            names.add(node.slice.value)
        # from src.x import y / import src.x
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src"):
            imported.add(node.module)
            for alias in node.names:
                submodule = f"{node.module}.{alias.name}"
                if _module_file(submodule) is not None:
                    imported.add(submodule)
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names if a.name.startswith("src"))

    for child in imported:
        names |= _env_names(child, visited)
    return names


def main() -> None:
    rows = []
    for job, module in ENTRY_MODULES.items():
        names = _env_names(module, set())
        rows.append((len(names), job, sorted(names - ALREADY_ON_CLOUD_RUN)))

    for total, job, extra in sorted(rows):
        print(f"--- {job}（合計{total}個 / 第1段の3つを除くと{len(extra)}個）")
        print("    " + (", ".join(extra) if extra else "追加なし"))


if __name__ == "__main__":
    main()
