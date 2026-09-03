"""Gmailの過去分を`EmailLog`へ取り込む（2026-09-03）。

■ なぜ要るのか（実測、2026-09-03）

```
   EmailLog の中身        308件（2026-08-25〜09-02 の9日分・担当者1名）
     受信                  31件 / 20連絡先
     送信                 277件 / 39連絡先
   返信ペアが作れた連絡先   44件中 13件（合計19ペア）
   信頼度                  13件すべて low（＝サンプル数4件以下）
```

日次同期`sync_rep()`は**直近2日・最大100件**しか見ないため、連携開始日より前の
メールは1通も入っていない。この状態では「連絡先ごとの平均返信ラグ」も
「返ってきやすい時間帯」も、1〜2件の偶然を断定として表示することにしかならない。
**先に過去を入れないと、この2つの機能は成立しない。**

■ 総当たりではなく、連絡先のメールアドレスで引く

メールボックス全体を`newer_than:365d`で舐めると、対象外のメール（メルマガ・社内・
通知）まで1通ずつ`messages.get`することになり、時間もAPI枠も無駄になる。
連絡先DBのメールアドレスは既に分かっているので、**アドレスで検索して当たった分だけ**
本文ヘッダを取りに行く。

**メールアドレスを持つ連絡先は30,824件**（2026-09-03実測。スプレッドシートの
「連絡先」タブは3,782行だが、Notion連絡先DBの実体はこの件数）。索引の作成だけで
Notionに約6分かかるので、`--index-cache`でファイルに残して流し直しに使う。

```
   ① Notion連絡先DB ──▶ メールアドレス→ページID の辞書を1回だけ作る
                        （メール1通ごとにNotionを叩くとレート制限で終わらない）
   ② アドレスを15件ずつまとめて Gmail 検索
        {from:a to:a cc:a from:b to:b cc:b …} newer_than:365d
   ③ 当たったメッセージIDだけ messages.get（並列8）
   ④ 連絡先突合・direction判定は日次同期と同じ classify_message() を通す
   ⑤ EmailLog へまとめて追記
```

■ 過去のメールで「今」を騒がせない

取り込みは`EmailLog`への追記だけを行い、インシデント判定・Notionの「最終メール日時」
更新・web-engagement-toolへの通知を**一切行わない**（`db.insert_email_logs()`参照）。
併せて未返信リマインドにも14日の上限を入れてある
（`src/email_reminders/reminder_check.py`の`_MAX_REMINDER_AGE_HOURS`）。
これが無いと、取り込んだ直後に過去のインシデントがダイジェストへ一斉に載り、
古い受信メールに対する「未返信です」のDMが大量に飛ぶ。

■ 使い方

    # まず試算（1通も書き込まない）。何通ヒットし、何件が連絡先と紐づくかを見る
    set -a; . dashboard/.env.local; set +a
    .venv/bin/python scripts/backfill_gmail_history.py --days 365

    # 実行
    .venv/bin/python scripts/backfill_gmail_history.py --days 365 --apply

**終わったら実件数を必ず数えること。** スクリプトの「完了」表示だけを信用しない。

    .venv/bin/python scripts/verify_gmail_backfill.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db_schema.registry import get_schema
from src.gmail_sync import db, gmail_client, sync
from src.gmail_sync.token_crypto import decrypt_token
from src.sync_engine.clients.notion_client import HttpNotionClient, parse_notion_property_value

logger = logging.getLogger("backfill_gmail_history")

_CONTACT_DB_KEY = "contact"
_EMAIL_PROPERTY = "メールアドレス"

# 1回のGmail検索に載せるメールアドレス件数。1件につき`from:` `to:` `cc:`の3語を出すため、
# 15件で概ね1,000〜1,300文字になる。クエリが長すぎるとGmail側が400を返すので欲張らない。
_ADDRESSES_PER_QUERY = 15

# `messages.get`の並列数。Gmailのユーザー単位クォータは250units/秒、`messages.get`は
# 5unitsなので理論上は50件/秒まで。429を出させても`request_with_retry`が待つだけ損なので
# 控えめにする（xargs -Pではなく必ずThreadPoolExecutorで組む、は運用ルール）。
_FETCH_WORKERS = 8

# `messages.list`（検索）の並列数。1バッチ＝アドレス15件ぶんの検索で、3万件だと
# 2,000バッチを超える。逐次だと検索だけで15分以上かかるので並列に投げる。
_SEARCH_WORKERS = 8


@dataclass
class RepResult:
    rep_email: str
    searched_addresses: int
    found_messages: int
    already_recorded: int
    fetched: int
    matched: int
    inserted: int
    inbound: int
    outbound: int
    errors: int


def load_or_build_contact_index(cache_path: str | None) -> dict[str, str]:
    """索引をキャッシュから読む。無ければ作って保存する。

    Notion連絡先DBは3万件超あり、全件取得に約6分かかる（2026-09-03実測）。
    途中で落ちたときの流し直しでここを毎回待つのは無駄なので、ファイルに残す。
    **キャッシュは連絡先の追加・メールアドレス変更に追随しない。** 最新にしたいときは
    ファイルを消してから流すこと。
    """
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            index: dict[str, str] = json.load(f)
        logger.info("索引をキャッシュから読み込んだ: %s（%d件）", cache_path, len(index))
        return index

    index = build_contact_index()
    if cache_path:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        logger.info("索引を保存した: %s", cache_path)
    return index


def build_contact_index() -> dict[str, str]:
    """連絡先DBの「メールアドレス」→ページIDの辞書を作る（小文字化して引けるようにする）。

    同じアドレスが複数の連絡先に登録されている場合は先勝ちにする。日次同期側の
    `find_page_id_by_email()`もNotionが返した先頭1件を採用しており、そちらに合わせる。
    """
    schema = get_schema(_CONTACT_DB_KEY)
    client = HttpNotionClient(_CONTACT_DB_KEY, schema.notion_database_id)
    pages = client.query_all_pages(
        filter={"property": _EMAIL_PROPERTY, "email": {"is_not_empty": True}}
    )
    index: dict[str, str] = {}
    for page in pages:
        props = page.get("properties") or {}
        raw = props.get(_EMAIL_PROPERTY)
        if raw is None:
            continue
        value = parse_notion_property_value(raw)
        if not isinstance(value, str):
            continue
        email = value.strip().lower()
        if email and email not in index:
            index[email] = page["id"]
    return index


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _build_query(addresses: Sequence[str], *, days: int) -> str:
    """`{from:a to:a cc:a …} newer_than:Nd -in:spam -in:trash`を組み立てる。

    Gmailの`{}`はORの意味。bccは検索対象にできないため、こちらがbccだけで送った
    メールは拾えない（1:1の営業メールでは稀なので割り切る）。
    """
    terms = " ".join(f"from:{a} to:{a} cc:{a}" for a in addresses)
    return f"{{{terms}}} newer_than:{days}d -in:spam -in:trash"


def search_message_ids(access_token: str, query: str) -> list[str]:
    """1クエリ分のメッセージIDを、ページングしながら全部集める。"""
    ids: list[str] = []
    page_token: str | None = None
    while True:
        page = gmail_client.list_messages_page(access_token, query=query, page_token=page_token)
        ids.extend(ref.id for ref in page.refs)
        page_token = page.next_page_token
        if not page_token:
            return ids


def backfill_rep(
    *,
    rep_email: str,
    refresh_token: str,
    contact_index: dict[str, str],
    existing_ids: set[str],
    days: int,
    apply: bool,
) -> RepResult:
    """営業担当1名分を取り込む。

    **エラー件数の加算はメインスレッドだけで行う。** ワーカーからは「失敗したかどうか」を
    戻り値で返させる。`result.errors += 1`をワーカーから直接呼ぶと、ロック無しの
    読み書きになって集計がわずかにズレる（挿入データ自体は壊れないが、
    ログの「エラーN件」を信用できなくなる）。
    """
    access_token = gmail_client.refresh_access_token(refresh_token)
    internal_domains = sync.internal_domains_from_env()
    addresses = sorted(contact_index)

    result = RepResult(
        rep_email=rep_email,
        searched_addresses=len(addresses),
        found_messages=0,
        already_recorded=0,
        fetched=0,
        matched=0,
        inserted=0,
        inbound=0,
        outbound=0,
        errors=0,
    )

    # ① 検索して、まだ記録していないメッセージIDだけを残す
    batches = list(_chunks(addresses, _ADDRESSES_PER_QUERY))
    logger.info("%s: %d件のアドレスを%dバッチに分けて検索する", rep_email, len(addresses), len(batches))

    def _search(batch: Sequence[str]) -> tuple[list[str], bool]:
        """(見つかったメッセージID, 失敗したか)を返す。"""
        try:
            return search_message_ids(access_token, _build_query(batch, days=days)), False
        except gmail_client.GmailApiError:
            logger.exception("検索に失敗（%s ほか%d件）", batch[0], len(batch) - 1)
            return [], True

    to_fetch: list[str] = []
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=_SEARCH_WORKERS) as pool:
        for done, (ids, failed) in enumerate(pool.map(_search, batches), start=1):
            if failed:
                result.errors += 1
            for message_id in ids:
                if message_id in seen:
                    continue
                seen.add(message_id)
                result.found_messages += 1
                if message_id in existing_ids:
                    result.already_recorded += 1
                else:
                    to_fetch.append(message_id)
            if done % 100 == 0:
                logger.info(
                    "  検索 %d/%dバッチ … ヒット%d件・未記録%d件",
                    done,
                    len(batches),
                    result.found_messages,
                    len(to_fetch),
                )

    logger.info(
        "%s: 検索完了 ヒット%d件（記録済み%d件）→ 取得対象%d件",
        rep_email,
        result.found_messages,
        result.already_recorded,
        len(to_fetch),
    )

    # ② ヘッダを取得して分類する
    def _fetch_and_classify(message_id: str) -> tuple[db.EmailLogRow | None, bool]:
        """(記録すべき行 または None, 失敗したか)を返す。"""
        try:
            message = gmail_client.get_message(access_token, message_id)
        except gmail_client.GmailApiError as exc:
            if exc.status_code == 404:
                # 完全削除済み等。日次同期側と同じ扱いで、エラーにはせず黙って飛ばす。
                return None, False
            logger.exception("メッセージ %s の取得に失敗", message_id)
            return None, True
        except Exception:
            logger.exception("メッセージ %s の処理に失敗", message_id)
            return None, True

        classified = sync.classify_message(
            message,
            rep_email=rep_email,
            internal_domains=internal_domains,
            resolve_contact=contact_index.get,
        )
        if classified is None:
            return None, False
        return (
            db.EmailLogRow(
                contact_page_id=classified.contact_page_id,
                contact_email=classified.contact_email,
                rep_email=rep_email,
                gmail_message_id=message.id,
                direction=classified.direction,
                subject=message.subject,
                snippet=message.snippet,
                sent_at=classified.sent_at,
            ),
            False,
        )

    rows: list[db.EmailLogRow] = []
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        for done, (row, failed) in enumerate(pool.map(_fetch_and_classify, to_fetch), start=1):
            result.fetched += 1
            if failed:
                result.errors += 1
            if row is not None:
                rows.append(row)
                result.matched += 1
                if row.direction == "inbound":
                    result.inbound += 1
                else:
                    result.outbound += 1
            if done % 500 == 0:
                logger.info("  取得 %d/%d 件 … 連絡先と紐づいた%d件", done, len(to_fetch), result.matched)

    # ③ まとめて追記する（--apply が無ければ1通も書き込まない）
    if apply and rows:
        result.inserted = db.insert_email_logs(rows)
        existing_ids.update(r.gmail_message_id for r in rows)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Gmailの過去分をEmailLogへ取り込む")
    parser.add_argument("--days", type=int, default=365, help="何日前まで遡るか（既定365）")
    parser.add_argument(
        "--rep", action="append", default=None, help="対象の営業担当メール（複数可、既定は連携済み全員）"
    )
    parser.add_argument("--apply", action="store_true", help="実際に書き込む（既定は試算のみ）")
    parser.add_argument(
        "--index-cache",
        default=None,
        help="連絡先索引の保存先（次回以降このファイルを読む。作り直すときは消す）",
    )
    parser.add_argument(
        "--limit-addresses",
        type=int,
        default=None,
        help=(
            "検索対象のメールアドレスを先頭N件に絞る。**最初の1回はこれを付けて小さく試すこと**"
            "（Gmailの検索構文が想定通り通るかを、3万件を投げる前に確かめるため）"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )

    if not args.apply:
        logger.info("★ 試算モード（--apply を付けるまで1通も書き込まない）")

    started = time.monotonic()
    logger.info("連絡先DBのメールアドレス索引を準備中 …")
    contact_index = load_or_build_contact_index(args.index_cache)
    logger.info("連絡先 %d件（メールアドレスあり）", len(contact_index))
    if not contact_index:
        logger.error("メールアドレスを持つ連絡先が0件。取り込む対象がない")
        return 1
    if args.limit_addresses is not None:
        kept = sorted(contact_index)[: args.limit_addresses]
        contact_index = {k: contact_index[k] for k in kept}
        logger.info("★ --limit-addresses により先頭%d件だけを対象にする", len(contact_index))

    existing_ids = db.fetch_existing_message_ids()
    logger.info("記録済みメール %d件", len(existing_ids))

    connections = db.list_gmail_connections()
    if args.rep:
        wanted = {r.lower() for r in args.rep}
        connections = [c for c in connections if c.rep_email.lower() in wanted]
    if not connections:
        logger.error("対象の営業担当が0名（Gmail連携済みの担当がいない、または --rep が一致しない）")
        return 1

    results: list[RepResult] = []
    for conn in connections:
        logger.info("── %s ──", conn.rep_email)
        try:
            results.append(
                backfill_rep(
                    rep_email=conn.rep_email,
                    refresh_token=decrypt_token(conn.refresh_token_enc),
                    contact_index=contact_index,
                    existing_ids=existing_ids,
                    days=args.days,
                    apply=args.apply,
                )
            )
        except Exception:
            logger.exception("%s の取り込みに失敗", conn.rep_email)

    elapsed = time.monotonic() - started
    logger.info("")
    logger.info("=========== 結果（%.1f分） ===========", elapsed / 60)
    for r in results:
        logger.info(
            "%s\n"
            "  検索ヒット      %6d件（うち記録済み %d件）\n"
            "  ヘッダ取得      %6d件\n"
            "  連絡先と紐づいた %6d件（受信 %d / 送信 %d）\n"
            "  追記            %6d件%s\n"
            "  エラー          %6d件",
            r.rep_email,
            r.found_messages,
            r.already_recorded,
            r.fetched,
            r.matched,
            r.inbound,
            r.outbound,
            r.inserted,
            "" if args.apply else "（試算のため書き込んでいない）",
            r.errors,
        )
    if args.apply:
        logger.info("")
        logger.info("★ scripts/verify_gmail_backfill.py で実件数を数えること")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
