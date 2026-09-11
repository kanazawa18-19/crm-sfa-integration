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
from urllib.parse import urlsplit

import requests

from src.facility_list.infrastructure.rakuten_page_parser import (
    AreaListEntry,
    count_area_list_blocks,
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
# 429の `Retry-After` に従って待つ上限。これを超える指定は「今日は諦める」。
MAX_RETRY_AFTER_SECONDS = 120.0
# robots.txt のグループ選択に使う自分のbot名(`USER_AGENT`の先頭と揃えること)。
BOT_NAME = "CnctorFacilityListBot"

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


class RakutenRateLimitedError(RakutenFetchError):
    """429。相手が明示的に待てと言っている。`Retry-After`を持つ。"""

    def __init__(self, message: str, *, retry_after_seconds: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RobotsDisallowedError(RuntimeError):
    """robots.txt で禁止されているURL。取得してはならない。"""


class _RateLimiter:
    """プロセス内で共有する最小間隔の番人。スレッド間で直列化する。

    **`time.sleep()`をロックの外で行う。** 中で寝るとスリープ中もロックを握り続け、
    他のスレッドがロック待ちの列に並ぶ(Geminiレビュー指摘、2026-09-12)。
    ロックの中では「自分の順番(いつ投げてよいか)」を決めるだけにして、待つのは外。

    なおこの制御は**このプロセスの中だけ**に効く。同じバッチを複数のプロセスや
    Vercelの別インスタンスで同時に走らせると、楽天から見た間隔はその分だけ詰まる
    (ChatGPTレビュー指摘)。取り込みバッチを多重起動しないこと。
    """

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_allowed_at)
            self._next_allowed_at = slot + self._interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


@dataclass(frozen=True)
class RobotsRules:
    """robots.txt のうち、自分たちに適用される規則。

    **施設番号ではなくURLパスで判定する。** 以前は `Disallow: /HOTEL/{数字}/` から
    番号だけを抜き出していたため、`Disallow: /HOTEL/` のような広い禁止や
    `Disallow: /HOTEL/*/foo` のようなワイルドカードを取りこぼしていた
    (ChatGPT/Gemini の他社レビュー指摘、2026-09-12)。
    """

    disallow: tuple[re.Pattern[str], ...] = ()
    allow: tuple[re.Pattern[str], ...] = ()

    def is_path_allowed(self, path: str) -> bool:
        """パス(クエリ含む)が許可されているか。

        robots.txt の慣習どおり、`Allow` と `Disallow` が両方当たったら
        **より長く一致した方**を優先する(同じ長さなら Allow を優先)。
        """
        best_allow = max((len(m.group(0)) for p in self.allow if (m := p.match(path))), default=-1)
        best_disallow = max(
            (len(m.group(0)) for p in self.disallow if (m := p.match(path))), default=-1
        )
        if best_disallow < 0:
            return True
        return best_allow >= best_disallow


def _rule_to_pattern(value: str) -> re.Pattern[str] | None:
    """robots.txt のパス指定を正規表現にする。`*`(任意) と `$`(終端) を解釈する。"""
    if not value:
        return None
    anchored_end = value.endswith("$")
    body = value[:-1] if anchored_end else value
    escaped = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile("^" + escaped + ("$" if anchored_end else ""))


def parse_robots(text: str, *, user_agent: str = "CnctorFacilityListBot") -> RobotsRules:
    """robots.txt から、自分たちに適用される規則だけを取り出す。

    - `User-agent:` が連続して並ぶ場合は**1つのグループ**として扱う
      (`User-agent: *` の次の行に別のUAが来ると、以前は `*` のルールを読み落としていた)
    - 自分のUA名を名指ししたグループがあればそれを優先し、無ければ `*` のグループを使う
    - `Allow` も読む(`Disallow` だけ見ると、例外的に許可された経路を誤って禁止する)
    """
    groups: list[tuple[set[str], list[str], list[str]]] = []
    current_agents: set[str] = set()
    current_disallow: list[str] = []
    current_allow: list[str] = []
    expecting_agents = False

    def flush() -> None:
        if current_agents:
            groups.append((set(current_agents), list(current_disallow), list(current_allow)))

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if key in ("user-agent", "user agent"):
            if not expecting_agents:
                # 直前のグループを閉じてから新しいグループを始める。
                flush()
                current_agents = set()
                current_disallow = []
                current_allow = []
                expecting_agents = True
            current_agents.add(value.lower())
            continue

        expecting_agents = False
        if key == "disallow":
            current_disallow.append(value)
        elif key == "allow":
            current_allow.append(value)

    flush()

    target = user_agent.lower()
    selected: tuple[list[str], list[str]] | None = None
    for agents, disallow, allow in groups:
        if any(target.startswith(a) for a in agents if a and a != "*"):
            selected = (disallow, allow)
            break
    if selected is None:
        for agents, disallow, allow in groups:
            if "*" in agents:
                selected = (disallow, allow)
                break
    if selected is None:
        return RobotsRules()

    disallow_rules, allow_rules = selected
    return RobotsRules(
        disallow=tuple(p for v in disallow_rules if (p := _rule_to_pattern(v))),
        allow=tuple(p for v in allow_rules if (p := _rule_to_pattern(v))),
    )


class RakutenTravelClient:
    """公開ページの取得口。`requests.Session`を使い回す。"""

    def __init__(
        self,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_workers: int = DEFAULT_MAX_WORKERS,
        session: requests.Session | None = None,
    ) -> None:
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._timeout = timeout_seconds
        self._limiter = _RateLimiter(interval_seconds)
        # **同時接続数を実際に抑える。** 定数を置くだけでは保証にならない。
        # 応答に時間がかかると、1秒間隔でも4本以上が同時に飛びうる
        # (ChatGPTレビュー指摘、2026-09-12)。
        self._slots = threading.Semaphore(max(1, max_workers))
        # robots.txt はホストごとに違う(travel と hotel.travel で内容が別)。
        self._robots_by_host: dict[str, RobotsRules] = {}
        self._robots_lock = threading.Lock()
        # エリア一覧で「カードはあるのに読み取れなかった」ページの数。
        # 0でなければ取り込み結果を信用しない。
        self.area_list_parse_warnings = 0

    # -- robots ------------------------------------------------------------
    def load_robots(self, host: str = "travel.rakuten.co.jp") -> RobotsRules:
        """そのホストの robots.txt を取得して覚える。取得できなければ**中止する**。

        「読めなかったから全部許可」は最悪の選択なので、ここでは例外を上げて
        取り込みごと止める。
        """
        with self._robots_lock:
            cached = self._robots_by_host.get(host)
        if cached is not None:
            return cached

        text = self._fetch_without_robots_check(f"https://{host}/robots.txt")
        rules = parse_robots(text, user_agent=BOT_NAME)
        with self._robots_lock:
            self._robots_by_host[host] = rules
        logger.info(
            "%s の robots.txt を読み込んだ: Disallow %d件 / Allow %d件",
            host,
            len(rules.disallow),
            len(rules.allow),
        )
        return rules

    def is_url_allowed(self, url: str) -> bool:
        """そのURLを取得してよいか。robots.txt をホストごとに見る。"""
        parts = urlsplit(url)
        if parts.path.rstrip("/") == "/robots.txt".rstrip("/"):
            return True
        rules = self.load_robots(parts.netloc)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return rules.is_path_allowed(path)

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
            blocks = count_area_list_blocks(html)

            # **カードはあるのに読み取れていない**＝パーサが部分的に壊れている。
            # このまま進むと、件数が減ったことを「最終ページ」と読み違えて
            # 母集団が静かに欠ける(ChatGPTレビュー指摘、2026-09-12)。
            if blocks > len(entries):
                self.area_list_parse_warnings += 1
                logger.error(
                    "%s %dページ目: 施設カード%d件のうち%d件しか読み取れなかった。"
                    "楽天側のHTML変更を疑うこと",
                    prefecture,
                    page,
                    blocks,
                    len(entries),
                )

            if not entries:
                break
            fresh = [e for e in entries if e.hotel_no not in seen]
            if not fresh:
                break
            for entry in fresh:
                seen.add(entry.hotel_no)
                yield entry
            # 打ち切り判定は**読み取れた件数ではなくカードの数**で行う。
            if blocks < AREA_LIST_PAGE_SIZE:
                break
            page += 1

    # -- 施設ページ --------------------------------------------------------
    def fetch_top_page(self, hotel_no: int) -> str:
        return self._get_text(f"{_BASE}/HOTEL/{hotel_no}/{hotel_no}.html")

    def fetch_detail_page(self, hotel_no: int) -> str:
        return self._get_text(f"{_BASE}/HOTEL/{hotel_no}/{hotel_no}_std.html")

    # -- 内部 --------------------------------------------------------------
    def _ensure_url_allowed(self, url: str) -> None:
        if not self.is_url_allowed(url):
            raise RobotsDisallowedError(f"robots.txtで取得が禁止されている: {url}")

    def _fetch_without_robots_check(self, url: str) -> str:
        """robots.txt 自身を取りに行くための経路(それ自体は常に許可)。"""
        response = self._request(url, check_robots=False)
        if not response.encoding or response.encoding.lower() in ("iso-8859-1", "ascii"):
            response.encoding = response.apparent_encoding or "utf-8"
        return response.text

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

    def _request(self, url: str, *, check_robots: bool = True) -> requests.Response:
        # **すべての取得で robots を見る。** 施設ページだけ見ていた頃は、
        # サイトマップとエリア一覧が評価されていなかった
        # (ChatGPTレビュー指摘、2026-09-12)。
        if check_robots:
            self._ensure_url_allowed(url)

        with self._slots:
            self._limiter.wait()
            try:
                response = self._session.get(url, timeout=self._timeout, allow_redirects=True)
            except requests.RequestException as exc:
                raise RakutenFetchError(f"取得に失敗した: {url}") from exc

        # リダイレクト先が許可されているとは限らない。最終URLで確かめ直す
        # (ChatGPTレビュー指摘)。
        if check_robots and response.url and response.url != url:
            final = urlsplit(response.url)
            if not final.netloc.endswith("rakuten.co.jp"):
                raise RakutenFetchError(f"想定外のホストへ転送された: {response.url}")
            if not self.is_url_allowed(response.url):
                raise RobotsDisallowedError(f"転送先が robots.txt で禁止されている: {response.url}")

        if response.status_code == 404:
            # 掲載終了・欠番。`_get_text()`がここを見て空文字に変える。
            return response
        if response.status_code == 429:
            # 相手が明示的に「待て」と言っている。1度だけ素直に待ってから諦める。
            # 何度も指数バックオフで粘るより、止めて人が判断する方がよい。
            retry_after = response.headers.get("Retry-After", "")
            wait_seconds = 0.0
            if retry_after.strip().isdigit():
                wait_seconds = min(float(retry_after.strip()), MAX_RETRY_AFTER_SECONDS)
            raise RakutenRateLimitedError(
                f"レート制限(429)に当たった。間隔を広げること: {url}",
                retry_after_seconds=wait_seconds,
            )
        if response.status_code >= 400:
            raise RakutenFetchError(f"HTTP {response.status_code}: {url}")
        return response
