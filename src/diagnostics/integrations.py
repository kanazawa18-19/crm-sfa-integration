"""外部連携の疎通診断（読み取りのみ・副作用なし）。

**なぜ必要か**

各連携は「認証情報が未設定なら警告ログを1行出して黙って無効化する」作りになっている
（`production_wiring.build_spreadsheet_targets_by_db()`等）。障害時に例外が飛ばないため、
同期が丸ごと止まっていても画面上は何も起きない。実際、2026-08-31に
「スプレッドシート同期が動いていないのでは」と気付いたのは人間の目視だった。

Vercel Hobbyのランタイムログ保持は約1時間しかなく、後追いでは追えない。
そのため**こちらから能動的に相手を叩いて到達を確かめる**手段を用意する。
CLAUDE.mdの「rc=0を信用しない／到達確認を入れる」に対応するもの。

**副作用を出さないこと**

ここで叩くのは全てGET相当の読み取り系のみ。レコードの作成・更新・削除は一切行わない。
唯一の例外はPostgresのadvisory lockで、これは**取得したその場で必ず解放する**
（`pg_advisory_unlock`をfinallyで呼ぶ）。診断が本番の排他制御を巻き込まないため。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import psycopg
import requests

from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.webhook_receipts import (
    KINTONE,
    NOTION,
    SPREADSHEET,
    ZOHO,
    list_webhook_receipts,
)

logger = logging.getLogger(__name__)

#: 診断の1件あたりのタイムアウト。全体で300秒(Vercelのmax_duration)を超えないよう短めに取る。
PROBE_TIMEOUT_SECONDS = 15.0

#: kintone用の環境変数サフィックス（`production_wiring._KINTONE_DB_ENV_SUFFIX`と同じ対象）。
KINTONE_ENV_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("client_master", "CLIENT"),
    ("project", "PROJECT"),
    ("action", "ACTION"),
)

OK = "ok"
NOT_CONFIGURED = "not_configured"
FAILED = "failed"


@dataclass
class ProbeResult:
    """1つの連携先に対する到達確認の結果。

    `status`は3値。`not_configured`（環境変数が無いので意図的に無効）と
    `failed`（設定はあるのに繋がらない）を**必ず区別する**。
    未設定を異常として通知すると誤報になり、誤報を鳴らし続けると本物の通知も無視される。
    """

    name: str
    status: str
    elapsed_ms: int = 0
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.extra:
            payload["extra"] = self.extra
        return payload


def _run(name: str, probe: Callable[[], ProbeResult]) -> ProbeResult:
    """1件の診断を実行し、所要時間の計測と例外の握り潰しを共通化する。

    1つの連携が落ちていても残りの診断は続ける。「全部見る」ことが目的なので、
    最初の失敗で止まると2つ目以降の状態が分からなくなる。
    """
    started = time.monotonic()
    try:
        result = probe()
    except Exception as exc:  # noqa: BLE001 - 診断なので全て結果として返す
        result = ProbeResult(name=name, status=FAILED, detail=f"{type(exc).__name__}: {exc}")
    result.name = name
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


# --- 個別の診断 ---------------------------------------------------------------------------


def probe_notion() -> ProbeResult:
    """NotionのデータベースをRetrieveして到達を確かめる（読み取りのみ）。"""
    api_key = os.environ.get("NOTION_API_KEY")
    if not api_key:
        return ProbeResult("notion", NOT_CONFIGURED, detail="NOTION_API_KEY未設定")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": "2022-06-28",
    }
    per_db: dict[str, str] = {}
    failures: list[str] = []
    for schema in ALL_SCHEMAS:
        if not schema.notion_database_id:
            per_db[schema.key] = "database_id未設定"
            continue
        response = requests.get(
            f"https://api.notion.com/v1/databases/{schema.notion_database_id}",
            headers=headers,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        if response.ok:
            per_db[schema.key] = "ok"
        else:
            per_db[schema.key] = f"HTTP {response.status_code}"
            failures.append(f"{schema.key}: HTTP {response.status_code}")

    status = FAILED if failures else OK
    return ProbeResult("notion", status, detail="; ".join(failures), extra={"databases": per_db})


def probe_kintone() -> ProbeResult:
    """kintoneの各アプリへ`records.json`を1件だけ問い合わせ、到達と件数を確かめる。

    レコードを作らずに済ませたいので取得系のみを使う。`limit 1`と`totalCount=true`を
    付けることで「認証・アプリID・閲覧権限の3点が揃っている」ことと「実際に何件あるか」が
    同時に分かる。

    **GETに`Content-Type`を付けてはいけない**（付けると400 `CB_IL02`になる。2026-08-28に
    `get_record()`が常に失敗していた原因がこれだった）。ここでもヘッダはAPIトークンのみ。
    """
    domain = os.environ.get("KINTONE_DOMAIN")
    if not domain:
        return ProbeResult("kintone", NOT_CONFIGURED, detail="KINTONE_DOMAIN未設定")

    per_app: dict[str, str] = {}
    failures: list[str] = []
    for db_key, suffix in KINTONE_ENV_SUFFIXES:
        app_id = os.environ.get(f"KINTONE_APP_ID_{suffix}")
        api_token = os.environ.get(f"KINTONE_API_TOKEN_{suffix}")
        if not app_id or not api_token:
            per_app[db_key] = "環境変数未設定"
            continue
        try:
            response = requests.get(
                f"https://{domain}/k/v1/records.json",
                headers={"X-Cybozu-API-Token": api_token},
                params={"app": app_id, "query": "limit 1", "totalCount": "true"},
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            if not response.ok:
                per_app[db_key] = f"HTTP {response.status_code}: {response.text[:120]}"
                failures.append(f"{db_key}: HTTP {response.status_code}")
                continue
            total = response.json().get("totalCount")
            per_app[db_key] = f"ok ({total}件)"
        except Exception as exc:  # noqa: BLE001
            per_app[db_key] = f"{type(exc).__name__}: {exc}"
            failures.append(f"{db_key}: {exc}")

    status = FAILED if failures else OK
    return ProbeResult("kintone", status, detail="; ".join(failures), extra={"apps": per_app})


def probe_zoho() -> ProbeResult:
    """Zohoのモジュール一覧を読み、OAuthリフレッシュトークンが生きていることを確かめる。

    読み取り専用でレコードに触れない。リフレッシュトークンが失効していればここで現れる。

    **エンドポイントは付与済みスコープの内側から選ぶこと。**このトークンに付いているのは
    `ZohoCRM.modules.ALL` / `ZohoCRM.settings.ALL` / `ZohoCRM.notifications.ALL` の3つで、
    `/org`は`ZohoCRM.org.READ`を要求するため401 `OAUTH_SCOPE_MISMATCH`になる
    （2026-08-31、これを「Zoho連携が壊れている」と誤診しかけた）。
    `/settings/modules`は`settings.ALL`の範囲内で、同期先モジュールの存在確認も兼ねられる。
    """
    from src.sync_engine.production_wiring import build_zoho_client

    client = build_zoho_client()
    if client is None:
        return ProbeResult("zoho", NOT_CONFIGURED, detail="ENABLE_ZOHO=false または認証情報未設定")

    base = os.environ.get("ZOHO_API_BASE_URL") or "https://www.zohoapis.jp/crm/v2"
    response = client.request("GET", f"{base.rstrip('/')}/settings/modules")
    if not response.ok:
        return ProbeResult(
            "zoho", FAILED, detail=f"HTTP {response.status_code}: {response.text[:160]}"
        )

    try:
        modules = response.json().get("modules") or []
    except (ValueError, AttributeError):
        return ProbeResult("zoho", FAILED, detail="モジュール一覧の読み取りに失敗")

    api_names = {str(m.get("api_name")) for m in modules}
    # 同期先として指定しているモジュールが実在するかまで見る（スプレッドシートのシート名と同じ考え方）。
    expected = {schema.key: schema.zoho_api_module for schema in ALL_SCHEMAS}
    missing = {k: v for k, v in expected.items() if v not in api_names}

    status = FAILED if missing else OK
    detail = (
        "同期先モジュールが見つからない: "
        + ", ".join(f"{k}→「{v}」" for k, v in missing.items())
        if missing
        else ""
    )
    return ProbeResult(
        "zoho",
        status,
        detail=detail,
        extra={"module_count": len(modules), "expected": expected},
    )


def probe_spreadsheet() -> ProbeResult:
    """スプレッドシートのシート名一覧を取得し、**同期先のシートが実在するか**まで確かめる。

    認証が通ることと、書き込み先が存在することは別。シート名が変わっていたり
    シートごと消えていたりすると、認証は通るのに同期だけが失敗し続ける。
    ここでは`ALL_SCHEMAS`が期待するシート名が全て存在するかを照合する。
    """
    from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient

    try:
        client = HttpSpreadsheetClient(timeout=PROBE_TIMEOUT_SECONDS)
    except ValueError as exc:
        return ProbeResult("spreadsheet", NOT_CONFIGURED, detail=str(exc))

    title, sheet_names = client.list_sheet_names()
    expected = {schema.key: schema.spreadsheet_sheet_name for schema in ALL_SCHEMAS}
    missing = {k: v for k, v in expected.items() if v not in sheet_names}

    # 「認証は通るが1件も書かれていない」を見逃さないため、実際の行数まで数える。
    row_counts: dict[str, int] = {}
    present = [v for v in expected.values() if v in sheet_names]
    if present:
        row_counts = client.count_rows(present)
    # ヘッダ行しか無いシート（＝データ0件）を明示的に拾う。
    empty = sorted(name for name, count in row_counts.items() if count <= 1)

    problems: list[str] = []
    if missing:
        problems.append(
            "同期先シートが見つからない: " + ", ".join(f"{k}→「{v}」" for k, v in missing.items())
        )
    if empty:
        problems.append("データが1件も無いシート: " + ", ".join(f"「{n}」" for n in empty))

    status = FAILED if problems else OK
    detail = " / ".join(problems)
    return ProbeResult(
        "spreadsheet",
        status,
        detail=detail,
        extra={
            "title": title,
            "sheets": sheet_names,
            "expected": expected,
            "row_counts": row_counts,
        },
    )


def probe_slack() -> ProbeResult:
    """Slackの`auth.test`でBotトークンの有効性を確かめる（読み取りのみ）。"""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return ProbeResult("slack", NOT_CONFIGURED, detail="SLACK_BOT_TOKEN未設定")

    response = requests.post(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    body = response.json() if response.content else {}
    if not body.get("ok"):
        return ProbeResult("slack", FAILED, detail=str(body.get("error", response.status_code)))
    return ProbeResult("slack", OK, extra={"team": body.get("team", ""), "bot": body.get("user", "")})


def probe_google_credentials() -> ProbeResult:
    """Googleのサービスアカウント認証情報からアクセストークンが取得できるかを確かめる。

    スプレッドシート・Drive・カレンダーが共通で使う土台なので、単独で切り出しておく。
    ここが落ちていれば下流の3つが同時に落ちるため、原因の切り分けが速くなる。
    """
    from src.document_generation.google_auth import get_google_access_token

    token = get_google_access_token()
    if not token:
        return ProbeResult("google_auth", FAILED, detail="アクセストークンが空")
    return ProbeResult("google_auth", OK, extra={"token_length": len(token)})


def probe_postgres() -> ProbeResult:
    """通常接続（pooled）で`SELECT 1`が通ることを確かめる。"""
    import psycopg

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return ProbeResult("postgres", NOT_CONFIGURED, detail="DATABASE_URL未設定")
    with psycopg.connect(dsn, connect_timeout=int(PROBE_TIMEOUT_SECONDS)) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    return ProbeResult("postgres", OK, extra={"pooled": "-pooler" in dsn})


def probe_advisory_lock() -> ProbeResult:
    """排他制御用の非pooled接続でadvisory lockが実際に取れることを確かめる。

    **取得したら必ず解放する。**本番のreconcileバッチと同じキー空間を使うため、
    握ったままにすると夜間バッチを止めてしまう。診断専用のキーを使い、finallyで解放する。

    Neonのpooled接続ではsession単位のadvisory lockが無言で無効になる（例外も出ない）ため、
    「取れた」だけでは不十分。ここでは`DATABASE_URL_UNPOOLED`が実際に使われているかも併せて返す。
    """
    from src.db_utils import connect_for_advisory_lock

    unpooled = os.environ.get("DATABASE_URL_UNPOOLED")
    if not os.environ.get("DATABASE_URL") and not unpooled:
        return ProbeResult("advisory_lock", NOT_CONFIGURED, detail="DATABASE_URL系が未設定")

    # 本番のロックキーと衝突しない診断専用のキー。
    probe_key = 918_273_645
    conn = connect_for_advisory_lock(logger)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (probe_key,))
            row = cur.fetchone()
            acquired = bool(row and row["acquired"])
            if acquired:
                cur.execute("SELECT pg_advisory_unlock(%s)", (probe_key,))
    finally:
        conn.close()

    if not acquired:
        return ProbeResult(
            "advisory_lock",
            FAILED,
            detail="診断用キーのロックが取得できなかった（他プロセスが握ったままの可能性）",
        )
    return ProbeResult(
        "advisory_lock",
        OK,
        extra={
            "unpooled_env_set": bool(unpooled),
            "unpooled_looks_pooled": bool(unpooled and "-pooler" in unpooled),
        },
    )


def probe_web_engagement_tool() -> ProbeResult:
    """MA（web-engagement-tool）が応答することを確かめる（トップページのGETのみ）。"""
    url = os.environ.get("WEB_ENGAGEMENT_TOOL_URL")
    if not url:
        return ProbeResult("web_engagement_tool", NOT_CONFIGURED, detail="WEB_ENGAGEMENT_TOOL_URL未設定")
    response = requests.get(url.rstrip("/"), timeout=PROBE_TIMEOUT_SECONDS, allow_redirects=True)
    if response.status_code >= 500:
        return ProbeResult("web_engagement_tool", FAILED, detail=f"HTTP {response.status_code}")
    return ProbeResult("web_engagement_tool", OK, extra={"http_status": response.status_code})


def probe_webhook_receipts() -> ProbeResult:
    """各ツールのWebhookを最後に受け取った時刻（`src/sync_engine/webhook_receipts.py`）。

    **ここでは異常判定をしない。** 受信が無いのは「購読が切れている」場合もあれば
    「単に変更が無かった」場合もあり、機械的には区別できない。誤報を鳴らすと本物の通知まで
    無視されるようになるため、事実（最終受信時刻と経過時間）だけを返し、解釈は人に任せる。
    """
    if not os.environ.get("DATABASE_URL"):
        return ProbeResult("webhook_receipts", NOT_CONFIGURED, detail="DATABASE_URL未設定")
    try:
        rows = list_webhook_receipts()
    except psycopg.errors.UndefinedTable:
        return ProbeResult(
            "webhook_receipts",
            NOT_CONFIGURED,
            detail="WebhookReceiptテーブルが未作成（マイグレーション未適用）",
        )
    now = datetime.now(timezone.utc)
    sources = {}
    for row in rows:
        last = row["lastReceivedAt"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        sources[row["source"]] = {
            "last_received_at": last.isoformat(),
            "hours_since": round((now - last).total_seconds() / 3600, 1),
            "count": int(row["receiptCount"]),
        }
    never = [name for name in (NOTION, KINTONE, ZOHO, SPREADSHEET) if name not in sources]
    return ProbeResult(
        "webhook_receipts",
        OK,
        detail=("記録開始以降まだ受信していない: " + ", ".join(never)) if never else "",
        extra={"sources": sources},
    )


#: 実行する診断の一覧。追加はここに1行足すだけで済むようにしている。
PROBES: tuple[tuple[str, Callable[[], ProbeResult]], ...] = (
    ("postgres", probe_postgres),
    ("advisory_lock", probe_advisory_lock),
    ("notion", probe_notion),
    ("kintone", probe_kintone),
    ("zoho", probe_zoho),
    ("google_auth", probe_google_credentials),
    ("spreadsheet", probe_spreadsheet),
    ("slack", probe_slack),
    ("web_engagement_tool", probe_web_engagement_tool),
    ("webhook_receipts", probe_webhook_receipts),
)


def run_integration_diagnostics(only: tuple[str, ...] = ()) -> dict[str, Any]:
    """全連携の到達確認を実行し、まとめた結果を返す。

    `only`を指定すると対象を絞れる（1つだけ再確認したいときのため）。
    """
    targets = [(name, fn) for name, fn in PROBES if not only or name in only]
    results = [_run(name, fn) for name, fn in targets]

    failed = [r.name for r in results if r.status == FAILED]
    not_configured = [r.name for r in results if r.status == NOT_CONFIGURED]
    return {
        # 「対処が要るもの」だけをこのキーに入れる。未設定はここに含めない。
        "failed": failed,
        "not_configured": not_configured,
        "ok": [r.name for r in results if r.status == OK],
        "results": [r.as_dict() for r in results],
    }
