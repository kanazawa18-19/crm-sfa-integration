"""楽天トラベルの取得口の検証。実際のHTTPは出さない(requests_mockで差し替える)。"""

from __future__ import annotations

import gzip

import pytest
import requests
import requests_mock

from src.facility_list.infrastructure.rakuten_client import (
    RakutenFetchError,
    RakutenTravelClient,
    RobotsDisallowedError,
    parse_robots,
)

ROBOTS = """User-Agent: *
Disallow: /cgi-bin/
Disallow: /HOTEL/5/*
Disallow: /HOTEL/146329/*

User-Agent: meta-webindexer
Disallow: /HOTEL/777/*
"""

# hotel.travel.rakuten.co.jp 側は別内容(実物も別)。エリア一覧・サイトマップ用。
HOTEL_ROBOTS = """User-agent: *
Disallow: /hotelinfo/calendar
"""


def _list_page(hotel_nos: list[int]) -> str:
    blocks = "".join(
        f'<div class="hotelBox"><a href="//x/?f_hotel_no={no}">宿{no}</a>'
        f"<p>〒680-0000鳥取県鳥取市{no}</p></div>"
        for no in hotel_nos
    )
    return f"<html><body>{blocks}</body></html>"


@pytest.fixture
def client() -> RakutenTravelClient:
    # 間隔0で回す(テストを待たせない)。実運用の既定は1.0秒。
    return RakutenTravelClient(interval_seconds=0.0, session=requests.Session())


class TestParseRobots:
    def test_自分に適用されるグループを選ぶ(self) -> None:
        rules = parse_robots(ROBOTS)
        assert rules.is_path_allowed("/HOTEL/1234/1234.html") is True
        assert rules.is_path_allowed("/HOTEL/5/5.html") is False
        assert rules.is_path_allowed("/HOTEL/146329/x.html") is False
        # 他のUA向けの禁止は自分たちには適用されない。
        assert rules.is_path_allowed("/HOTEL/777/777.html") is True

    def test_User_agentが連続していても1つのグループとして読む(self) -> None:
        # 「*」の次の行に別のUAが来ると、以前は「*」のルールを読み落としていた
        # (ChatGPTレビュー指摘、2026-09-12)。
        rules = parse_robots("User-agent: *\nUser-agent: OtherBot\nDisallow: /HOTEL/\n")
        assert rules.is_path_allowed("/HOTEL/1234/1234.html") is False

    def test_ワイルドカードを解釈する(self) -> None:
        rules = parse_robots("User-agent: *\nDisallow: /HOTEL/*/secret\n")
        assert rules.is_path_allowed("/HOTEL/999/secret") is False
        assert rules.is_path_allowed("/HOTEL/999/999.html") is True

    def test_Allowが長く一致すればDisallowに優先する(self) -> None:
        rules = parse_robots(
            "User-agent: *\nDisallow: /HOTEL/5/\nAllow: /HOTEL/5/public\n"
        )
        assert rules.is_path_allowed("/HOTEL/5/public") is True
        assert rules.is_path_allowed("/HOTEL/5/private") is False

    def test_自分のUAを名指ししたグループを優先する(self) -> None:
        rules = parse_robots(
            "User-agent: CnctorFacilityListBot\nDisallow: /\n\nUser-agent: *\nDisallow:\n"
        )
        assert rules.is_path_allowed("/HOTEL/1234/1234.html") is False

    def test_空なら何も禁止されない(self) -> None:
        assert parse_robots("").is_path_allowed("/HOTEL/1/1.html") is True


class TestRobotsEnforcement:
    def test_禁止された施設は取りに行かない(self, client: RakutenTravelClient) -> None:
        with requests_mock.Mocker() as mock:
            mock.get("https://travel.rakuten.co.jp/robots.txt", text=ROBOTS)
            with pytest.raises(RobotsDisallowedError):
                client.fetch_top_page(5)
            # robots.txt以外へのリクエストは1本も出ていないこと。
            assert [r.path for r in mock.request_history] == ["/robots.txt"]

    def test_エリア一覧もrobotsを見る(self, client: RakutenTravelClient) -> None:
        # 施設ページだけ見ていた頃は、サイトマップとエリア一覧が素通りだった
        # (ChatGPTレビュー指摘、2026-09-12)。
        with requests_mock.Mocker() as mock:
            mock.get(
                "https://hotel.travel.rakuten.co.jp/robots.txt",
                text="User-agent: *\nDisallow: /hotellist/\n",
            )
            with pytest.raises(RobotsDisallowedError):
                list(client.iter_area_list("鳥取県"))
            assert [r.path for r in mock.request_history] == ["/robots.txt"]

    def test_ホストごとに別のrobotsを読む(self, client: RakutenTravelClient) -> None:
        with requests_mock.Mocker() as mock:
            mock.get("https://travel.rakuten.co.jp/robots.txt", text=ROBOTS)
            mock.get("https://hotel.travel.rakuten.co.jp/robots.txt", text=HOTEL_ROBOTS)
            assert client.is_url_allowed("https://travel.rakuten.co.jp/HOTEL/5/5.html") is False
            assert (
                client.is_url_allowed("https://hotel.travel.rakuten.co.jp/HOTEL/5/5.html") is True
            )

    def test_robotsが読めなければ取り込みごと止める(self, client: RakutenTravelClient) -> None:
        # 「読めなかったから全部許可」にしない。
        with requests_mock.Mocker() as mock:
            mock.get("https://travel.rakuten.co.jp/robots.txt", status_code=500)
            with pytest.raises(RakutenFetchError):
                client.fetch_top_page(1234)


class TestIterAreaList:
    def _mock_robots(self, mock: requests_mock.Mocker) -> None:
        mock.get("https://travel.rakuten.co.jp/robots.txt", text=ROBOTS)
        mock.get("https://hotel.travel.rakuten.co.jp/robots.txt", text=HOTEL_ROBOTS)

    def test_ページを辿って重複なく返す(self, client: RakutenTravelClient) -> None:
        with requests_mock.Mocker() as mock:
            self._mock_robots(mock)
            mock.get(
                "https://hotel.travel.rakuten.co.jp/hotellist/area/tottori/",
                text=_list_page(list(range(1, 31))),
            )
            mock.get(
                "https://hotel.travel.rakuten.co.jp/hotellist/area/tottori/?f_page=2",
                text=_list_page(list(range(31, 41))),
            )
            entries = list(client.iter_area_list("鳥取県"))
        assert [e.hotel_no for e in entries] == list(range(1, 41))

    def test_同じ施設しか出なくなったら止める(self, client: RakutenTravelClient) -> None:
        # 楽天側が最終ページの次でも同じ内容を返す作りに変わっても無限ループしないこと。
        with requests_mock.Mocker() as mock:
            self._mock_robots(mock)
            same = _list_page(list(range(1, 31)))
            mock.get("https://hotel.travel.rakuten.co.jp/hotellist/area/tottori/", text=same)
            mock.get(
                "https://hotel.travel.rakuten.co.jp/hotellist/area/tottori/?f_page=2", text=same
            )
            entries = list(client.iter_area_list("鳥取県"))
        assert len(entries) == 30

    def test_未知の都道府県は弾く(self, client: RakutenTravelClient) -> None:
        with pytest.raises(ValueError):
            list(client.iter_area_list("架空県"))


class TestFetchErrors:
    def test_レート制限は専用の例外にする(self, client: RakutenTravelClient) -> None:
        with requests_mock.Mocker() as mock:
            mock.get("https://travel.rakuten.co.jp/robots.txt", text=ROBOTS)
            mock.get("https://travel.rakuten.co.jp/HOTEL/1234/1234.html", status_code=429)
            with pytest.raises(RakutenFetchError, match="429"):
                client.fetch_top_page(1234)

    def test_404は例外にせず掲載なしとして返す(self, client: RakutenTravelClient) -> None:
        # 掲載終了・欠番は「失敗」ではなく「掲載なし」として扱う。
        # **楽天の404ページは空ではなく1,600バイト前後のHTML**（2026-09-11実測）なので、
        # 本文の長さではなくステータスコードで判定していることを固定する。
        not_found_html = (
            "<html><head><title>お探しのページは見つかりません｜楽天トラベル</title></head>"
            "<body>" + "<p>ご指定のページは見つかりませんでした。</p>" * 120 + "</body></html>"
        )
        assert len(not_found_html) > 2000  # 長さヒューリスティックでは救えない大きさ
        with requests_mock.Mocker() as mock:
            mock.get("https://travel.rakuten.co.jp/robots.txt", text=ROBOTS)
            mock.get(
                "https://travel.rakuten.co.jp/HOTEL/1234/1234.html",
                status_code=404,
                text=not_found_html,
            )
            assert client.fetch_top_page(1234) == ""


class TestSitemap:
    def test_gzのサイトマップから施設番号を取る(self, client: RakutenTravelClient) -> None:
        index = (
            "<sitemapindex><sitemap><loc>"
            "https://hotel.travel.rakuten.co.jp/sitemap_a.xml.gz"
            "</loc></sitemap></sitemapindex>"
        )
        body = gzip.compress(
            b"<urlset>"
            b"<url><loc>https://hotel.travel.rakuten.co.jp/hotelinfo/plan/103</loc></url>"
            b"<url><loc>https://hotel.travel.rakuten.co.jp/hinfo/9219/pet/</loc></url>"
            b"<url><loc>https://hotel.travel.rakuten.co.jp/hotelinfo/plan/103</loc></url>"
            b"</urlset>"
        )
        with requests_mock.Mocker() as mock:
            mock.get("https://hotel.travel.rakuten.co.jp/robots.txt", text=HOTEL_ROBOTS)
            mock.get("https://hotel.travel.rakuten.co.jp/sitemap_index.xml", text=index)
            mock.get("https://hotel.travel.rakuten.co.jp/sitemap_a.xml.gz", content=body)
            numbers = client.fetch_all_hotel_numbers()
        assert numbers == (103, 9219)


class TestRateLimiterAndSlots:
    """レート制御と同時接続数(他社レビューで指摘された2点)。"""

    def test_スリープ中にロックを握り続けない(self) -> None:
        # ロックの中で寝ると、他スレッドがロック待ちの列に並ぶ
        # (Geminiレビュー指摘、2026-09-12)。ロック内では順番を決めるだけにする。
        import threading
        import time as _time

        from src.facility_list.infrastructure.rakuten_client import _RateLimiter

        limiter = _RateLimiter(0.2)
        started = threading.Event()

        def first() -> None:
            limiter.wait()  # 1本目は待たずに通る
            started.set()
            limiter.wait()  # 2本目は0.2秒待つ

        thread = threading.Thread(target=first)
        thread.start()
        assert started.wait(timeout=1.0)
        # 別スレッドが待っている間でも、ロック自体は空いている。
        acquired = limiter._lock.acquire(timeout=0.1)
        assert acquired is True
        limiter._lock.release()
        thread.join(timeout=2.0)

    def test_同時接続数を実際に絞る(self) -> None:
        # 定数を置くだけでは保証にならない(ChatGPTレビュー指摘、2026-09-12)。
        client = RakutenTravelClient(interval_seconds=0.0, max_workers=2)
        assert client._slots._value == 2
