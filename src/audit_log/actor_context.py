"""「今処理しているNotion書き込みがどの経路から来たか」を伝える`contextvars`。

`HttpNotionClient`（`src/sync_engine/clients/notion_client.py`）は1インスタンスが複数の
呼び出し経路から共有されうる（例: `production_wiring.py`が構築する`Dispatcher`用の
Notionクライアントは、kintone Webhook経由・Zoho Webhook経由のどちらの書き込みでも同じ
インスタンスが使われる）。そのため「どの経路からの書き込みか」をクライアントの
コンストラクタ引数として固定することはできず、各呼び出し元のエントリポイント
（Webhookハンドラの`handler()`、Gmail同期のエントリ関数等）が処理を開始する時点で
`set_actor()`をwithブロックとして被せ、実際にNotionへ書き込む瞬間（ネストした関数呼び出しの
奥深く）まで暗黙に伝播させる。

ネスト（同じ経路の中でさらに`set_actor()`する）は現状想定していないが、万一ネストしても
内側のwithブロックを抜けた時点で外側の値へ正しく復帰する（`ContextVar.reset()`によるトークン
方式のため）。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, NamedTuple


class Actor(NamedTuple):
    source: str
    label: str | None


_DEFAULT_ACTOR = Actor(source="unknown", label=None)

_current_actor: contextvars.ContextVar[Actor] = contextvars.ContextVar(
    "audit_log_current_actor", default=_DEFAULT_ACTOR
)


@contextmanager
def set_actor(source: str, label: str | None = None) -> Iterator[None]:
    """このwithブロック内で発生するNotion書き込みの`actorSource`/`actorLabel`を設定する。

    `source`は"kintone_webhook"/"zoho_webhook"/"gmail_sync"/"migration"等、呼び出し元を
    識別できる固定文字列を渡すこと（`AuditLog.actorSource`にそのまま保存される）。
    """
    token = _current_actor.set(Actor(source=source, label=label))
    try:
        yield
    finally:
        _current_actor.reset(token)


def get_actor() -> Actor:
    """現在のコンテキストに設定されている`Actor`を返す。

    `set_actor()`で囲まれていない状態でNotion書き込みが発生した場合（新しい書き込み経路が
    追加されたのに`set_actor()`を仕込み忘れた場合等）は`source="unknown"`を返す。これは
    サイレントに経路不明のまま記録され続けると気づきにくいため、`recorder.py`側で
    warningログを残す。
    """
    return _current_actor.get()
