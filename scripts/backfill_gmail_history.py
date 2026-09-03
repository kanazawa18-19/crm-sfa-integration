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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime, timezone
from typing import Iterable, Sequence, TypeVar
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db_schema.registry import get_schema
from src.gmail_sync import db, gmail_client, sync
from src.gmail_sync.token_crypto import decrypt_token
from src.sync_engine.clients.notion_client import HttpNotionClient, parse_notion_property_value

_T = TypeVar("_T")

JST = ZoneInfo("Asia/Tokyo")

logger = logging.getLogger("backfill_gmail_history")

_CONTACT_DB_KEY = "contact"
_EMAIL_PROPERTY = "メールアドレス"

# 1回のGmail検索に載せるメールアドレス件数。1件につき`from:` `to:` `cc:`の3語を出すため、
# 15件で概ね1,000〜1,300文字になる。クエリが長すぎるとGmail側が400を返すので欲張らない。
_ADDRESSES_PER_QUERY = 15

# `messages.get`の並列数。
#
# **「250units/秒だから50件/秒まで出せる」という前提はもう成り立たない**
# （ChatGPTレビュー指摘、2026-09-03）。Gmail APIのクォータ体系は2025〜2026年に
# 変わっており、新体系では`messages.get`は概ね5件/秒が目安。2025年11月〜2026年4月に
# 使っていた既存プロジェクトは旧クォータのまま、という猶予もあるため、
# **このプロジェクトがどちらかは実際に確かめるまで分からない。**
# 8は「429が出たら`request_with_retry`が待つ」前提の控えめな値。
# 遅い・429が多いようなら下げる（xargs -Pではなく必ずThreadPoolExecutorで組む、は運用ルール）。
_FETCH_WORKERS = 8

# `messages.list`（検索）の並列数。1バッチ＝アドレス15件ぶんの検索で、3万件だと
# 2,000バッチを超える。逐次だと検索だけで15分以上かかるので並列に投げる。
_SEARCH_WORKERS = 8

# 1回のINSERTに載せる行数。数万件を1トランザクションで投げるとメモリとDB側の
# トランザクションが膨らむため割る（Geminiレビュー指摘、2026-09-03）。
_INSERT_BATCH_SIZE = 500


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


# 索引キャッシュの寿命。これを超えたら作り直す。
#
# **古いキャッシュは「取りこぼす」だけでなく「間違った連絡先に履歴を付ける」**
# （ChatGPTレビュー指摘、2026-09-03）。キャッシュ作成後にNotion側でアドレスの
# 持ち主が変わっていると、そのメールを**別人の履歴として恒久的に保存**してしまう。
# 取り込みは1〜2時間で終わる想定なので、24時間を超えたキャッシュは使わない。
_INDEX_CACHE_MAX_AGE_HOURS = 24


def load_or_build_contact_index(cache_path: str | None) -> dict[str, str]:
    """索引をキャッシュから読む。無い・古い場合は作って保存する。

    Notion連絡先DBは3万件超あり、全件取得に約6分かかる（2026-09-03実測）。
    途中で落ちたときの流し直しでここを毎回待つのは無駄なので、ファイルに残す。
    """
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                payload = json.load(f)
            built_at = datetime.fromisoformat(payload["built_at"])
            index: dict[str, str] = payload["index"]
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            logger.warning("索引キャッシュを読めなかったので作り直す: %s", cache_path)
        else:
            age_hours = (datetime.now(timezone.utc) - built_at).total_seconds() / 3600
            if age_hours <= _INDEX_CACHE_MAX_AGE_HOURS:
                logger.info(
                    "索引をキャッシュから読み込んだ: %s（%d件・%.1f時間前）",
                    cache_path,
                    len(index),
                    age_hours,
                )
                return index
            logger.warning(
                "索引キャッシュが古い（%.1f時間前 > %d時間）ので作り直す",
                age_hours,
                _INDEX_CACHE_MAX_AGE_HOURS,
            )

    index = build_contact_index()
    if cache_path:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(
                {"built_at": datetime.now(timezone.utc).isoformat(), "index": index},
                f,
                ensure_ascii=False,
            )
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


def _chunks(items: Sequence[_T], size: int) -> Iterable[Sequence[_T]]:
    """メールアドレスの列にも、追記する行の列にも使う。"""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _build_query(addresses: Sequence[str], *, days: int, before: date) -> str:
    """`{from:a to:a …} newer_than:Nd before:YYYY/MM/DD -in:spam -in:trash`を組み立てる。

    ■ `before` は必須（★これが無いと本番の通知が黙って止まる）

    `newer_than:365d`だけだと**今日のメールまで対象**になる。取り込みが先に
    `EmailLog`へ入れてしまうと、直後に走る通常同期の`_process_message_ref()`が
    冒頭の`email_log_exists()`でTrueを返して即座に抜ける。

    ```
       10:00     顧客から重大なクレームが届く
       10:00:10  取り込みが先に見つけて EmailLog へ（インシデント判定はしない）
       10:01     通常同期が同じメールを処理 → 既に在るので即 return
                 → インシデント検知されない
                 → Notionの「最終メール日時」も更新されない
                 → web-engagement-tool へも通知されない
    ```

    「副作用を起こさない」つもりが、**正常な副作用を打ち消す**状態になる
    （ChatGPTレビューのBLOCKER指摘、2026-09-03）。そのため通常同期が面倒を見ている
    期間とは**絶対に重ならない**ところで打ち切る。

    ■ `cc:` は入れない

    突合側（`classify_message()`）はFrom/Toヘッダーしか見ていない。検索だけ`cc:`を
    含めても、当たったメールは突合で必ず落ちる＝`messages.get`を無駄に呼ぶだけになる
    （ChatGPTレビュー指摘）。**検索条件と突合条件を揃える。**
    連絡先がCCにしか居ないメールを拾いたくなったら、突合側から直すこと。

    bccも検索できないため、こちらがbccだけで送ったメールは拾えない。
    """
    terms = " ".join(f"from:{a} to:{a}" for a in addresses)
    return (
        f"{{{terms}}} newer_than:{days}d "
        f"before:{before.year}/{before.month}/{before.day} -in:spam -in:trash"
    )


def live_sync_coverage_start() -> date | None:
    """通常同期が面倒を見ている期間の始まり（＝`EmailLog`の最も古い日時、JST）。

    ここより前だけを取り込めば、通常同期と同じメールを奪い合うことがない。
    `EmailLog`が空なら`None`（呼び出し側が`--before`を要求する）。
    """
    oldest = db.fetch_oldest_email_sent_at()
    if oldest is None:
        return None
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return oldest.astimezone(JST).date()


class _AccessToken:
    """アクセストークンを、期限が来たら黙って取り直すホルダー（2026-09-03）。

    3万件のアドレスを検索し、当たったメールを全部取りに行くと**1担当で1時間を超えうる**。
    最初に1回だけ`refresh_access_token()`する作りだと、途中でトークンが切れて
    そこから先が全部401で落ちる（ChatGPTレビュー指摘）。

    Googleのアクセストークンの寿命は1時間なので、余裕を見て45分で取り直す。
    複数のワーカースレッドから呼ばれるためロックで囲う。
    """

    _TTL_SECONDS = 45 * 60

    def __init__(self, refresh_token: str) -> None:
        self._refresh_token = refresh_token
        self._lock = threading.Lock()
        self._token: str | None = None
        self._issued_at = 0.0

    def __call__(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._token is None or now - self._issued_at > self._TTL_SECONDS:
                self._token = gmail_client.refresh_access_token(self._refresh_token)
                self._issued_at = now
            return self._token


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
    before: date,
    apply: bool,
) -> RepResult:
    """営業担当1名分を取り込む。

    **エラー件数の加算はメインスレッドだけで行う。** ワーカーからは「失敗したかどうか」を
    戻り値で返させる。`result.errors += 1`をワーカーから直接呼ぶと、ロック無しの
    読み書きになって集計がわずかにズレる（挿入データ自体は壊れないが、
    ログの「エラーN件」を信用できなくなる）。
    """
    token = _AccessToken(refresh_token)
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
            query = _build_query(batch, days=days, before=before)
            return search_message_ids(token(), query), False
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
            message = gmail_client.get_message(token(), message_id)
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
            # `_extract_addresses()`が既に小文字化しているので現状は素の`.get`でも
            # 一致するが、あちらの正規化に依存させない（Geminiレビュー指摘、2026-09-03）。
            # 索引側は`build_contact_index()`で小文字化済み。
            resolve_contact=lambda addr: contact_index.get(addr.lower()),
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
                gmail_thread_id=classified.thread_id,
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

    # ③ 追記する（--apply が無ければ1通も書き込まない）
    #
    # **まとめて1回のINSERTにしない。** 対象が数万件になると、巨大なパラメータ列を
    # 1トランザクションで投げることになり、メモリとDB側のトランザクションが膨らむ
    # （Geminiレビュー指摘、2026-09-03）。500件ずつに割る。
    # 途中で落ちてもそこまでの分は残り、流し直しは`ON CONFLICT DO NOTHING`で冪等。
    if apply and rows:
        for batch in _chunks(rows, _INSERT_BATCH_SIZE):
            result.inserted += db.insert_email_logs(list(batch))
            existing_ids.update(r.gmail_message_id for r in batch)
            logger.info("  追記 %d/%d 件", result.inserted, len(rows))
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
        "--before",
        default=None,
        help=(
            "この日より前だけを取り込む（YYYY-MM-DD、日本時間）。"
            "既定はEmailLogの最も古い日付＝通常同期が面倒を見ている期間の始まり。"
            "**通常同期と重なる期間を指定しない**（重なると通常同期の通知が黙って止まる）"
        ),
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

    # ★ 通常同期が面倒を見ている期間と重ならないところで打ち切る（_build_query参照）。
    if args.before:
        try:
            before = date.fromisoformat(args.before)
        except ValueError:
            logger.error("--before は YYYY-MM-DD で指定する: %r", args.before)
            return 1
    else:
        before = live_sync_coverage_start()
        if before is None:
            logger.error(
                "EmailLogが空なので打ち切り日を決められない。--before YYYY-MM-DD を指定すること"
            )
            return 1
        logger.info("打ち切り日（EmailLogの最も古い日付）: %s より前だけを取り込む", before)

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
                    before=before,
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

    # ★ 1件でも失敗していたら成功で終わらせない（ChatGPTレビュー指摘、2026-09-03）。
    # rc=0 だけを見て「取り込めた」と判断すると、検索や取得が静かに欠けたまま
    # 「完了」になる。担当ごとの失敗（例外で results に入らなかった分）も同じ扱い。
    total_errors = sum(r.errors for r in results)
    if total_errors or len(results) != len(connections):
        logger.error(
            "★ 失敗あり: エラー%d件 / 担当 %d名中 %d名しか完了していない",
            total_errors,
            len(connections),
            len(results),
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
