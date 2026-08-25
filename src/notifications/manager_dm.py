"""`User.isManager = true`の全ユーザーへSlack DMで通知する共通ヘルパー(2026-08-25)。

`src/incident_detection/notify.py`(高優先度インシデント検知)と`src/sync_engine/slack_notifier.py`
(Round2「新規レコード自動作成」の運用可視性通知)の両方が「高優先度イベントを
`User.isManager = true`の全員へSlack DMで通知する」という同じ要件を持つため、DB解決部分
(`find_manager_emails()`)をここに集約する。通知先はハードコード/env変数ではなく、dashboard
管理画面でON/OFFできる`User.isManager`フラグ(アクセス権限用の`role`とは別軸)からその都度
動的に解決する。

DM送信自体のプリミティブ(`_resolve_dm_channel`/`_slack_headers`/`_SLACK_API_BASE`、
`users.lookupByEmail`→`conversations.open`→`chat.postMessage`パターン)は
`src/meeting_sync/slack_approval.py`のものをそのまま再利用する(`incident_detection/notify.py`と
同じ再利用方針)。使うのは既存の`SLACK_BOT_TOKEN`。新規env変数は無い。

■ 例外を投げない設計について: `notify_managers()`は`SLACK_BOT_TOKEN`未設定・managerが0人・
`find_manager_emails()`自体の失敗(DB接続エラー等)のいずれの場合も何もせずreturnする
(`src/incident_detection/notify.py`の`notify_managers_immediate()`と同じ設計思想)。ただし
「何もせず」であってもログには必ず1行残す(下記「未設定・0人時のログについて」参照、
shirokuma-secレビュー対応)。同じ理由で、対象者ごとのDM送信失敗も本モジュール内でtry/exceptし、
1人への送信失敗が他の対象者への送信や呼び出し元へ伝播しないようにする。この結果、
`notify_managers()`自体は通常のPython例外(`KeyboardInterrupt`等を除く)を一切送出しない。

■ 未設定・0人時のログについて(2026-08-25、shirokuma-secレビュー対応): `SLACK_BOT_TOKEN`が
未設定、または`manager_emails`が空(`User.isManager = true`のユーザーが0人)の場合、以前は
ログすら残さず静かにreturnしていた。これだと「新規レコード自動作成の異常が誰にも通知され
ないだけでなく、なぜ通知が飛ばなかったかの痕跡も残らない」状態になりうる(特に本番で
`isManager`フラグがまだ誰にもONになっていない期間)。そのため両ケースとも`logger.warning`で
1行残すようにした。

■ 全体タイムアウト予算について(2026-08-25、shirokuma-secレビュー【最重要】対応): 本モジュールの
呼び出し元(`src/sync_engine/slack_notifier.py`経由の`Dispatcher`)は、Vercelのサーバーレス
関数(FastAPI)としてデプロイされたWebhookハンドラから`BackgroundTasks`を使わず同期的に
呼ばれる。Vercelのサーバーレス実行モデルではレスポンス送信後に関数プロセスが凍結/終了しうる
ため`BackgroundTasks`は導入できず、マネージャーN人ぶんのDM送信(各最大3回の逐次Slack Web API
呼び出し+DB接続)がそのままWebhookレスポンスをブロックする。これを「N人×最大30秒」から
大幅に短縮するため、`notify_managers()`全体に`_NOTIFY_MANAGERS_TIME_BUDGET_SECONDS`の合計
タイムアウト予算を設け、超過したら残りのマネージャーへの送信を打ち切り`logger.warning`で
記録する。各Slack API呼び出し自体のtimeoutも`_DM_API_CALL_TIMEOUT_SECONDS`(3秒)に短縮した
(`send_dm()`/`_resolve_dm_channel()`参照)。「1人への送信失敗が他の対象者への送信を止めない」
という既存の安全設計はそのまま維持している。

■ `incident_detection/notify.py`との役割分担について: `find_manager_emails()`はここへ移設し
`src/incident_detection/db.py`側は本モジュールへ委譲するだけの薄いラッパーへ変更した
(`incident_detection`パッケージ名がインシデント検知専用に見えるため、DB解決ロジック自体は
このドメイン非依存のモジュールへ寄せる)。一方`incident_detection/notify.py`内の
`_send_incident_dm()`(DM送信そのもの)は、既存の単体テストが`notify._resolve_dm_channel`/
`notify.db.find_manager_emails`をモジュール属性として直接monkeypatchしている前提に依存して
おり、ここへの委譲に置き換えるとテストのpatch対象がずれて既存の検証が無効化されるリスクが
あるため、あえて統合していない(既存の動作・テストを壊さないことを優先)。新規に追加する
`send_dm()`/`notify_managers()`は、それ自体は`_send_incident_dm()`とほぼ同じ処理を行うが、
`WebhookSlackNotifier`(新規の呼び出し元)向けの実装として独立させている。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import psycopg
import requests
from psycopg.rows import dict_row

from src.meeting_sync.slack_approval import (
    _SLACK_API_BASE,
    _resolve_dm_channel,
    _slack_headers,
)

logger = logging.getLogger(__name__)

# 各Slack API呼び出し(users.lookupByEmail/conversations.open/chat.postMessage)自体のtimeout。
# `src/meeting_sync/slack_approval.py`の`_REQUEST_TIMEOUT_SECONDS`(10秒)より短くする
# (2026-08-25、shirokuma-secレビュー対応。モジュールdocstring「全体タイムアウト予算について」参照)。
_DM_API_CALL_TIMEOUT_SECONDS = 3.0

# `notify_managers()`全体(全マネージャーへのDM送信ループ)の合計タイムアウト予算。超過したら
# 残りのマネージャーへの送信を打ち切る(2026-08-25、shirokuma-secレビュー対応)。
_NOTIFY_MANAGERS_TIME_BUDGET_SECONDS = 5.0


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeout/options="-c timezone=UTC"は他のsrc/*/db.py群と同じ理由
    # (ハング防止・UTC前提のタイムゾーン固定)。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def find_manager_emails() -> list[str]:
    """`User.isManager = true`の全ユーザーのemailを返す。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT email FROM "User" WHERE "isManager" = true')
        rows = cur.fetchall()
    return [row["email"] for row in rows]


def send_dm(manager_email: str, text: str, *, timeout: float = _DM_API_CALL_TIMEOUT_SECONDS) -> None:
    """`manager_email`宛にSlack DMで`text`を送る。

    ユーザー解決・DM送信のいずれかが失敗した場合は例外を送出する。呼び出し元
    (`notify_managers()`)でtry/exceptすることを前提とした実装であり、このメソッド自体は
    失敗を握りつぶさない。`timeout`は各Slack API呼び出し(users.lookupByEmail/
    conversations.open/chat.postMessage)自体のtimeout秒数(モジュールdocstring「全体
    タイムアウト予算について」参照)。
    """
    resolved = _resolve_dm_channel(manager_email, timeout=timeout)
    if resolved is None:
        raise RuntimeError(f"Slackユーザー解決に失敗しました: {manager_email}")
    channel, _user_id = resolved

    response = requests.post(
        f"{_SLACK_API_BASE}/chat.postMessage",
        headers=_slack_headers(),
        json={"channel": channel, "text": text},
        timeout=timeout,
    )
    result = response.json()
    if not result.get("ok"):
        # Slack Web APIはHTTP 200でもエラーをbody({"ok": false, "error": ...})で返す
        # (slack_approval.py/incident_detection/notify.pyと同じ注意点)。
        raise RuntimeError(f"chat.postMessage失敗: {result.get('error')}")


def notify_managers(text: str, *, log_context: str) -> None:
    """`text`を`User.isManager = true`の全員へSlack DMで送る。

    `SLACK_BOT_TOKEN`未設定・managerが0人・`find_manager_emails()`自体の失敗のいずれの
    場合もログを残した上でreturnする(モジュールdocstring「未設定・0人時のログについて」
    参照)。対象者ごとに独立してtry/exceptするため、1人への送信失敗が他の対象者への送信や
    呼び出し元へ伝播することはない(モジュールdocstring「例外を投げない設計について」参照)。
    さらに全体の合計タイムアウト予算(`_NOTIFY_MANAGERS_TIME_BUDGET_SECONDS`)を超過した場合、
    残りのマネージャーへの送信は打ち切る(モジュールdocstring「全体タイムアウト予算について」
    参照)。`log_context`はログメッセージの先頭に付与する呼び出し元識別用の文字列
    (例: `"WebhookSlackNotifier"`)。
    """
    if not os.environ.get("SLACK_BOT_TOKEN"):
        logger.warning(
            "%s: SLACK_BOT_TOKEN is not configured; skipping manager DM notification entirely "
            "(no manager will be notified)",
            log_context,
        )
        return

    try:
        manager_emails = find_manager_emails()
    except Exception:
        logger.exception("%s: failed to resolve manager emails", log_context)
        return
    if not manager_emails:
        logger.warning(
            "%s: no managers found (User.isManager = true has 0 rows); skipping manager DM "
            "notification entirely",
            log_context,
        )
        return

    deadline = time.monotonic() + _NOTIFY_MANAGERS_TIME_BUDGET_SECONDS
    for index, manager_email in enumerate(manager_emails):
        if time.monotonic() >= deadline:
            logger.warning(
                "%s: exceeded the %.1fs time budget for notifying managers; skipping the "
                "remaining %d/%d managers to avoid blocking the caller",
                log_context,
                _NOTIFY_MANAGERS_TIME_BUDGET_SECONDS,
                len(manager_emails) - index,
                len(manager_emails),
            )
            break
        try:
            send_dm(manager_email, text)
        except Exception:
            logger.exception("%s: failed to notify manager %s", log_context, manager_email)
