"""Notionの「1クエリ1万件」の壁を越えて全件取る（2026-09-01）。

■ 何が起きていたか

Notion の Database Query は、**1クエリあたり1万件までしか返さない。**
しかも打ち切るときに `has_more: false` を返すため、呼び出し側からは
**「全部取れた」ように見える。** エラーも警告も出ない。

実測: 取引先マスターDBは **102,799件** あるのに、素直に取ると 10,000件で止まる。
案件管理のPostgresミラーも取引先名インデックス（名寄せ）も同じ取り方をしており、
**9割以上が「存在しない」扱いになっていた。**
金沢さんが「ダッシュボードの案件数が10000になっている」と気づかなければ、そのままだった。

■ どう越えるか

**作成日時の昇順に並べ、最後に見た作成日時から先を取り直す**（キーセット方式）。

- 境界は `on_or_after` で重ねて取り、**ページIDで重複を除く**。
  `after` にすると、同じ秒に作られたレコードを取りこぼす
- 同じ作成日時が上限ぶん並ぶと前へ進めない。そのときは**打ち切らずERRORを出す**
  （黙って欠けるのが今回の問題そのものなので、気づける形で止める）

■ 時間予算で中断できる

全件取得は約18分かかる。Vercelの実行上限は300秒なので、1回では終わらない。
**「1万件で静かに切れる」を直したら、今度は「時間切れで何もしない」になる。**
そこで`time_budget_seconds`で区切って中断し、`watermark`を返して次回に続けられる。
中断は必ず「周」の区切りで行う（ページの途中で止めると、cursorを保存できず取りこぼす）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

#: Notionが1クエリで返す上限。
QUERY_RESULT_CAP = 10_000

#: 上限ちょうどでなくても、近ければ「まだ先がある」とみなす。
#: `request_status`（下記）が返らない場合の予備の判定。
_NEXT_ROUND_THRESHOLD = 9_000


def _is_truncated(data: Mapping[str, Any]) -> bool:
    """Notionが「このクエリは打ち切った」と明示しているか（2026-09-01追加）。

    ■ 件数の勘ではなく、公式のシグナルを使う

    Notionは応答に`request_status`を返し、1万件で打ち切ったときは
    `{"type": "incomplete", "incomplete_reason": "query_result_limit_reached"}` になる。
    **`has_more: false` と同時にこれが返る**ため、`has_more`だけを見ていると
    「全部取れた」に見えるが、ここを見れば確実に分かる。

    本番で実測（2026-09-01、ChatGPTのレビュー指摘を受けて確認）:

        案件管理DB（26,017件）  request_status = {"type": "incomplete",
                                "incomplete_reason": "query_result_limit_reached"}
        サービス・商品（83件）   request_status = null
        チェーン（1,029件）      request_status = null

    フィールドが無い応答（古いAPIバージョン等）ではFalseを返し、
    従来どおり件数の閾値（`_NEXT_ROUND_THRESHOLD`）で判断させる。
    """
    status = data.get("request_status")
    return isinstance(status, Mapping) and status.get("type") == "incomplete"


@dataclass(frozen=True)
class KeysetPage:
    """取れた結果と、続きから再開するためのしおり。"""

    pages: list[dict[str, Any]]
    #: 最後に見たページの created_time。次回はここから取り直す。
    watermark: str | None
    #: 全部取り切ったか。Falseなら続きがある。
    completed: bool
    #: **前へ進めなくなったか**（同じ作成日時が1周の上限ぶん並んでいる）。
    #: Trueなら取りこぼしており、人が直すまで自力では回復しない（2026-09-01追加）。
    #: このとき`completed`は必ずFalse。**掃除に進ませてはいけない。**
    stalled: bool = False


def _build_body(
    base_filter: dict[str, Any] | None,
    watermark: str | None,
    page_size: int,
    cursor: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "page_size": page_size,
        "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
    }
    conditions = list(
        (base_filter or {}).get("and", []) or ([base_filter] if base_filter else [])
    )
    if watermark is not None:
        conditions.append(
            {"timestamp": "created_time", "created_time": {"on_or_after": watermark}}
        )
    if conditions:
        body["filter"] = conditions[0] if len(conditions) == 1 else {"and": conditions}
    if cursor is not None:
        body["start_cursor"] = cursor
    return body


def query_keyset_slice(
    post_query: Callable[[dict[str, Any]], Mapping[str, Any]],
    *,
    base_filter: dict[str, Any] | None = None,
    watermark: str | None = None,
    page_size: int = 100,
    round_limit: int = QUERY_RESULT_CAP,
    time_budget_seconds: float | None = None,
    label: str = "",
) -> KeysetPage:
    """`watermark`から先を取る。時間予算を指定すると、周の区切りで中断する。

    `round_limit`は1周で取る件数。既定はNotionの上限（1万件）だが、
    小さくすると中断の粒度が細かくなる（時間予算を守りやすい）。
    """
    started = time.monotonic()
    collected: dict[str, dict[str, Any]] = {}
    current = watermark

    while True:
        round_count = 0
        cursor: str | None = None
        newest = current

        truncated = False
        while round_count < round_limit:
            data = post_query(_build_body(base_filter, current, page_size, cursor))
            if _is_truncated(data):
                truncated = True
            results = data.get("results") or []
            for page in results:
                page_id = page.get("id")
                if page_id:
                    collected[str(page_id)] = page
                created = page.get("created_time")
                if created:
                    newest = created
            round_count += len(results)

            if not data.get("has_more"):
                # **`has_more: false` を額面どおり受け取らない。**
                # Notionが「打ち切った」と明示していれば、まだ先がある。
                # これが今回の問題そのもの（`has_more`だけ見て9割欠けていた）。
                if truncated:
                    break
                # このクエリで取り切った。上限に届いていなければ本当に終わり。
                if round_count < min(round_limit, _NEXT_ROUND_THRESHOLD):
                    return KeysetPage(list(collected.values()), newest, True)
                break
            cursor = data.get("next_cursor")
            if not cursor:
                # Notion APIの契約上は起きない応答。先頭ページの取り直しを繰り返す
                # 無限ループを避けて打ち切る。
                logger.warning(
                    "query_keyset_slice: has_more=True なのに next_cursor が空です"
                    "（label=%r）。ここで打ち切ります",
                    label,
                )
                return KeysetPage(list(collected.values()), newest, True)

        if newest == current:
            # **ここで completed=True を返してはいけない**（2026-09-01、Geminiレビュー指摘）。
            # 取りこぼしているのに「取り切った」と伝えると、呼び出し元がそのまま掃除
            # （mark-and-sweep）へ進み、**まだ見ていない後半の行が全部消える。**
            # 取得済みが全体の半分を超えていれば件数の急減チェックもすり抜けるため、
            # ミラーを全消失させた2026-08-25の事故と同じ結末になりうる。
            # 自力では回復しない（round_limitを上げるか、データ側を直すしかない）ので、
            # stalled=True で「人が要る」と伝え、掃除には絶対に進ませない。
            logger.error(
                "query_keyset_slice: 同じ作成日時が上限ぶん並んでおり、これ以上進めません"
                "（label=%r, 取得済み=%d件, created_time=%r）。**取りこぼしています。**"
                "round_limitを上げるか、作成日時の重なりを解消してください",
                label,
                len(collected),
                current,
            )
            return KeysetPage(list(collected.values()), newest, False, stalled=True)

        current = newest
        if time_budget_seconds is not None and time.monotonic() - started >= time_budget_seconds:
            logger.info(
                "query_keyset_slice: 時間予算に達したので中断します"
                "（label=%r, ここまで%d件, 次は %s 以降）",
                label,
                len(collected),
                current,
            )
            return KeysetPage(list(collected.values()), current, False)
        logger.info(
            "query_keyset_slice: 続きを取ります（label=%r, ここまで%d件, 次は %s 以降）",
            label,
            len(collected),
            current,
        )


class KeysetStalledError(RuntimeError):
    """前へ進めず取りこぼした（同じ作成日時が1周の上限ぶん並んでいる）。

    **黙って部分的な結果を返さないために送出する**（2026-09-01）。
    9割欠けたリストが「全件」として下流へ流れるのが、そもそも今回直した問題そのもの。
    自力では回復しないので、握りつぶさず人へ届ける。
    """


def query_all_with_keyset(
    post_query: Callable[[dict[str, Any]], Mapping[str, Any]],
    *,
    base_filter: dict[str, Any] | None = None,
    page_size: int = 100,
    label: str = "",
) -> list[dict[str, Any]]:
    """1万件の壁を越えて**全件**返す（時間の制約が無い場所から使う）。

    取りこぼしたときは`KeysetStalledError`を送出する。**部分的な結果を「全件」として
    返さない。** ここの既定の`round_limit`はNotionの上限（1万件）なので、これが起きるのは
    「同じ作成日時のレコードが1万件並んでいる」ときだけ。
    """
    slice_ = query_keyset_slice(
        post_query, base_filter=base_filter, page_size=page_size, label=label
    )
    if slice_.stalled:
        raise KeysetStalledError(
            f"query_all_with_keyset: 前へ進めず取りこぼしました"
            f"（label={label!r}, 取得済み={len(slice_.pages)}件, "
            f"created_time={slice_.watermark!r}）"
        )
    return slice_.pages
