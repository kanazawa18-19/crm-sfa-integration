"""楽天トラベルの公開ページ・サイトマップを取得する(2026-09-11)。

**楽天ウェブサービス(API)は使わない。** 利用規約第10条(4)が「楽天アフィリエイト以外の
方法で収入を得ること」を禁じており、自社商材の営業リスト作成がこれに触れる可能性が
あるため、2026-09-11に本人判断で公開ページ方式を採用した。

公開ページを機械で取得する以上、次の2点を必ず守る:

  ① robots.txt の Disallow を尊重する(`/HOTEL/{一部の施設番号}/*` が名指しで禁止されている)
  ② 1リクエストごとに間隔を空け、同時接続数を絞る(既定: 1.0秒間隔・同時3)

母集団はサイトマップから取れる(2026-09-11実測で国内43,753施設)。全国を一度に回さず、
都道府県などで対象を絞ってから取得すること。
"""

from __future__ import annotations

import gzip
import logging
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import requests

from src.facility_list.infrastructure.rakuten_page_parser import (
    AreaListEntry,
    parse_area_list_page,
)

logger = logging.getLogger(__name__)

_BASE = "https://travel.rakuten.co.jp"
_SITEMAP_INDEX = "https://hotel.travel.rakuten.co.jp/sitemap_index.xml"
_AREA_LIST_BASE = "https://hotel.travel.rakuten.co.jp/hotellist/area"
_ROBOTS_URL = f"{_BASE}/robots.txt"

# 自分たちが誰かを名乗る(問い合わせ先を明示しておく方が、遮断されたときに話が早い)。
USER_AGENT = (
    "CnctorFacilityListBot/1.0 (+https://cnctor.jp/; contact: kanazawa@cnctor.jp) "
    "python-requests"
)

DEFAULT_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_WORKERS = 3
DEFAULT_TIMEOUT_SECONDS = 20

# 都道府県 → エリア一覧ページのURLスラッグ(2026-09-11に一覧ページの「地域変更」から実取得)。
PREFECTURE_SLUGS: dict[str, str] = {
    "北海道": "hokkaido", "青森県": "aomori", "岩手県": "iwate", "宮城県": "miyagi",
    "秋田県": "akita", "山形県": "yamagata", "福島県": "fukushima", "茨城県": "ibaraki",
    "栃木県": "tochigi", "群馬県": "gunma", "埼玉県": "saitama", "千葉県": "chiba",
    "東京都": "tokyo", "神奈川県": "kanagawa", "新潟県": "niigata", "富山県": "toyama",
    "石川県": "ishikawa", "福井県": "fukui", "山梨県": "yamanashi", "長野県": "nagano",
    "岐阜県": "gifu", "静岡県": "shizuoka", "愛知県": "aichi", "三重県": "mie",
    "滋賀県": "shiga", "京都府": "kyoto", "大阪府": "osaka", "兵庫県": "hyogo",
    "奈良県": "nara", "和歌山県": "wakayama", "鳥取県": "tottori", "島根県": "shimane",
    "岡山県": "okayama", "広島県": "hiroshima", "山口県": "yamaguchi", "徳島県": "tokushima",
    "香川県": "kagawa", "愛媛県": "ehime", "高知県": "kochi", "福岡県": "fukuoka",
    "佐賀県": "saga", "長崎県": "nagasaki", "熊本県": "kumamoto", "大分県": "oita",
    "宮崎県": "miyazaki", "鹿児島県": "kagoshima", "沖縄県": "okinawa",
}

# 一覧の1ページあたり件数(楽天側の固定値)。打ち切り判定に使う。
AREA_LIST_PAGE_SIZE = 30



class RakutenFetchError(RuntimeError):
    """取得そのものに失敗した(ネットワーク・5xx・レート制限)。

    「ページは取れたが項目が読めなかった」とは区別する。前者は再実行すべきで、
    後者は楽天側のHTML変更を疑うべきなので、扱いが違う。
    """


class RobotsDisallowedError(RuntimeError):
    """robots.txt で禁止されている施設。取得してはならない。"""


class _RateLimiter:
    """プロセス内で共有する最小間隔の番人。スレッド間で直列化する。"""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed_at:
                time.sleep(self._next_allowed_at - now)
                now = time.monotonic()
            self._next_allowed_at = now + self._interval


@dataclass(frozen=True)
class RobotsRules:
    """robots.txt のうち、施設ページに関係する禁止ルール。"""

    disallowed_hotel_nos: frozenset[int]
    # `Disallow: /HOTEL/` のように施設ページ全体が禁止されているか。
    # 今のrobots.txtは施設番号を名指しする形だが、楽天が将来まとめて禁止に
    # 変えたときに「数字が続かない行は無視」で全部許可に倒れると事故になる
    # (shirokuma-secレビュー指摘、2026-09-11)。
    hotel_pages_disallowed: bool = False

    def is_allowed(self, hotel_no: int) -> bool:
        if self.hotel_pages_disallowed:
            return False
        return hotel_no not in self.disallowed_hotel_nos


def parse_robots(text: str) -> RobotsRules:
    """robots.txt から `Disallow: /HOTEL/{番号}/*` の施設番号を集める。

    `User-Agent: *` のブロックだけを見る(他のUAブロックは自分たちには適用されない)。
    """
    disallowed: set[int] = set()
    hotel_pages_disallowed = False
    in_wildcard_block = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key in ("user-agent", "user agent"):
            in_wildcard_block = value == "*"
            continue
        if key == "disallow" and in_wildcard_block:
            m = re.match(r"^/HOTEL/(\d+)/", value, re.I)
            if m:
                disallowed.add(int(m.group(1)))
                continue
            # `/HOTEL/` `/HOTEL/*` `/` のように、施設ページをまとめて禁止する指定。
            if re.match(r"^/(HOTEL/?\*?)?$", value, re.I):
                hotel_pages_disallowed = True
    return RobotsRules(
        disallowed_hotel_nos=frozenset(disallowed),
        hotel_pages_disallowed=hotel_pages_disallowed,
    )


class RakutenTravelClient:
    """公開ページの取得口。`requests.Session`を使い回す。"""

    def __init__(
        self,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._timeout = timeout_seconds
        self._limiter = _RateLimiter(interval_seconds)
        self._robots: RobotsRules | None = None

    # -- robots ------------------------------------------------------------
    def load_robots(self) -> RobotsRules:
        """robots.txt を取得して覚える。取得できなければ**中止する**。

        「読めなかったから全部許可」は最悪の選択なので、ここでは例外を上げて
        取り込みごと止める。
        """
        if self._robots is not None:
            return self._robots
        text = self._get_text(_ROBOTS_URL)
        self._robots = parse_robots(text)
        if self._robots.hotel_pages_disallowed:
            logger.error("robots.txtが施設ページ全体を禁止している。取得してはならない")
        else:
            logger.info(
                "robots.txtを読み込んだ: 取得禁止の施設番号=%d件",
                len(self._robots.disallowed_hotel_nos),
            )
        return self._robots

    # -- サイトマップ ------------------------------------------------------
    def fetch_all_hotel_numbers(self) -> tuple[int, ...]:
        """サイトマップから掲載中の全施設番号を取る(1〜2リクエストで済む)。"""
        index_xml = self._get_text(_SITEMAP_INDEX)
        sitemap_urls = re.findall(r"<loc>([^<]+)</loc>", index_xml)
        numbers: set[int] = set()
        for url in sitemap_urls:
            body = self._get_bytes(url)
            if url.endswith(".gz"):
                body = gzip.decompress(body)
            xml = body.decode("utf-8", "ignore")
            for m in re.finditer(r"/(?:hotelinfo/plan|hinfo|HOTEL)/(\d+)", xml):
                numbers.add(int(m.group(1)))
        logger.info("サイトマップから施設番号を%d件取得した", len(numbers))
        return tuple(sorted(numbers))


    # -- エリア一覧 --------------------------------------------------------
    def iter_area_list(
        self, prefecture: str, *, max_pages: int | None = None
    ) -> Iterator[AreaListEntry]:
        """都道府県のエリア一覧を1ページ30件ずつ辿り、施設を順に返す。

        施設ページを1軒ずつ開かなくても、ここで**施設名・クチコミ点数・住所・最安料金**
        まで取れる(2026-09-11実測)。都道府県とクチコミ点数での一次絞り込みはこれで足り、
        取得リクエスト数が約1/30になる。

        終了判定は「0件になった」または「前のページと同じ施設しか出なくなった」。
        楽天側が最終ページの次でも同じ内容を返す作りに変わっても止まるようにしている。
        """
        slug = PREFECTURE_SLUGS.get(prefecture)
        if not slug:
            raise ValueError(f"未知の都道府県: {prefecture}")

        seen: set[int] = set()
        page = 1
        while max_pages is None or page <= max_pages:
            url = f"{_AREA_LIST_BASE}/{slug}/"
            if page > 1:
                url = f"{url}?f_page={page}"
            html = self._get_text(url)
            entries = parse_area_list_page(html)
            if not entries:
                break
            fresh = [e for e in entries if e.hotel_no not in seen]
            if not fresh:
                break
            for entry in fresh:
                seen.add(entry.hotel_no)
                yield entry
            if len(entries) < AREA_LIST_PAGE_SIZE:
                break
            page += 1

    # -- 施設ページ --------------------------------------------------------
    def fetch_top_page(self, hotel_no: int) -> str:
        self._ensure_allowed(hotel_no)
        return self._get_text(f"{_BASE}/HOTEL/{hotel_no}/{hotel_no}.html")

    def fetch_detail_page(self, hotel_no: int) -> str:
        self._ensure_allowed(hotel_no)
        return self._get_text(f"{_BASE}/HOTEL/{hotel_no}/{hotel_no}_std.html")

    # -- 内部 --------------------------------------------------------------
    def _ensure_allowed(self, hotel_no: int) -> None:
        robots = self.load_robots()
        if not robots.is_allowed(hotel_no):
            raise RobotsDisallowedError(f"robots.txtで取得が禁止されている施設: {hotel_no}")

    def _get_text(self, url: str) -> str:
        response = self._request(url)
        if response.status_code == 404:
            # **404の本文を返さない。** 楽天の404ページは空ではなく1,600バイト前後の
            # HTMLで、そのまま渡すとパーサが「ページは取れた」と誤認しうる
            # (kuma-qaレビューで実測、2026-09-11)。掲載なしは空文字で表す。
            return ""
        # 楽天トラベルはUTF-8だが、明示が無いとrequestsがISO-8859-1と誤認することがある。
        if not response.encoding or response.encoding.lower() in ("iso-8859-1", "ascii"):
            response.encoding = response.apparent_encoding or "utf-8"
        return response.text

    def _get_bytes(self, url: str) -> bytes:
        return self._request(url).content

    def _request(self, url: str) -> requests.Response:
        self._limiter.wait()
        try:
            response = self._session.get(url, timeout=self._timeout, allow_redirects=True)
        except requests.RequestException as exc:
            raise RakutenFetchError(f"取得に失敗した: {url}") from exc
        if response.status_code == 404:
            # 掲載終了・欠番。`_get_text()`がここを見て空文字に変える。
            return response
        if response.status_code == 429:
            raise RakutenFetchError(f"レート制限(429)に当たった。間隔を広げること: {url}")
        if response.status_code >= 400:
            raise RakutenFetchError(f"HTTP {response.status_code}: {url}")
        return response
