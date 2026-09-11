"""楽天トラベルの公開ページのパーサの検証。

HTMLは2026-09-11に実ページから採った構造を、検証に必要な部分だけ抜き出したもの
(ページ全体を置くと巨大な上に他社のコンテンツをリポジトリへ持ち込むことになる)。
実ページでの動作は同日に確認済み: 1008(ハートンホテル西梅田)・168000(醸す森)・
103(リーガロイヤル大阪)・鳥取県のエリア一覧。
"""

from __future__ import annotations

from decimal import Decimal

from src.facility_list.infrastructure.rakuten_page_parser import (
    count_area_list_blocks,
    parse_area_list_page,
    parse_detail_page,
    parse_top_page,
    split_address,
)

TOP_PAGE = """
<html><head><title>ハートンホテル西梅田 宿泊予約【楽天トラベル】</title></head>
<body>
<ul property="aggregateRating" typeof="AggregateRating" class="header__links">
  <meta property="ratingValue" content="4.2">
  <a href="//travel.rakuten.co.jp/HOTEL/1008/review.html" property="reviewCount" content="7472"
     class="review-count__text">(<em>7,472</em>件)</a>
</ul>
<a href="https://travel.rakuten.co.jp/HOTEL/1008/gallery.html">写真・動画(83)</a>
<a href="https://travel.rakuten.co.jp/HOTEL/1008/rtmap.html">地図・アクセス</a>
<a href="GW1008191122100615.html?">梅田駅・大阪駅からのアクセス方法</a>
<a href="GW1008191122100912.html?">お知らせ</a>
<a href="GW1008191122100912.html#info_tax">コチラ</a>
<a href="https://travel.rakuten.co.jp/HOTEL/1008/1008_std.html">詳細情報</a>
</body></html>
""" + "<!-- padding -->" * 200

TOP_PAGE_WITHOUT_CUSTOM = """
<html><head><title>アパホテル〈新大阪駅前〉 宿泊予約【楽天トラベル】</title></head>
<body>
<meta property="ratingValue" content="3.85">
<a href="https://travel.rakuten.co.jp/HOTEL/179018/179018_onsen.html">温泉</a>
</body></html>
""" + "<!-- padding -->" * 200

DETAIL_PAGE = """
<html><body>
<table>
 <tr><th>基本情報</th></tr>
 <tr><th>総部屋数</th><td>471室</td></tr>
 <tr><th>館内設備</th><td><ul><li>レストラン</li><li>会議室</li><li>大浴場</li></ul></td></tr>
 <tr><th>部屋設備・備品</th><td><ul><li>テレビ</li><li>浴衣</li></ul></td></tr>
 <tr><th>その他設備・サービス</th><td>-</td></tr>
 <tr><th>風呂</th><td>[種類] 温泉&nbsp;大浴場&nbsp;天然温泉&nbsp;</td></tr>
 <tr><th>チェックイン</th><td>14:00 （最終チェックイン：29:30）</td></tr>
 <tr><th>チェックアウト</th><td>12:00</td></tr>
 <tr><th>住所</th><td>〒530-0001大阪府大阪市北区梅田3-3-55</td></tr>
</table>
</body></html>
""" + "<!-- padding -->" * 200

AREA_LIST_PAGE = """
<html><body>
<div class="hotelBox">
  <a href="//hotel.travel.rakuten.co.jp/hotelinfo/plan/9219?f_hotel_no=9219">メルキュール鳥取大山リゾート＆スパ</a>
  <span>[最安料金] 4,500 円～</span>
  <a href="/customerVoice.do?f_hotel_no=9219">お客さまの声</a> 4.13
  <p>〒689-4108鳥取県西伯郡伯耆町丸山1647-13
     米子道『溝口IC』より車で約15分</p>
  <a href="//hotel.travel.rakuten.co.jp/hotelinfo/plan/9219">宿泊プラン一覧</a>
</div>
<div class="hotelBox">
  <a href="//hotel.travel.rakuten.co.jp/hotelinfo/plan/14991?f_hotel_no=14991">はわい温泉　望湖楼</a>
  <span>[最安料金] 9,000 円～</span>
  <a href="/customerVoice.do?f_hotel_no=14991">お客さまの声</a> 4.59
  <p>〒682-0704鳥取県東伯郡湯梨浜町はわい温泉4-25</p>
</div>
</body></html>
"""


class TestParseTopPage:
    def test_主要項目を取り出す(self) -> None:
        facts = parse_top_page(TOP_PAGE, 1008)
        assert facts.is_available
        assert facts.name == "ハートンホテル西梅田"
        assert facts.review_average == Decimal("4.2")
        assert facts.review_count == 7472
        assert facts.photo_count == 83

    def test_カスタマイズページを重複なく数える(self) -> None:
        # 同じページが複数箇所から貼られるため、重複を除いて数える。
        facts = parse_top_page(TOP_PAGE, 1008)
        assert facts.custom_page_ids == ("GW1008191122100615", "GW1008191122100912")

    def test_カスタマイズページが無い施設(self) -> None:
        facts = parse_top_page(TOP_PAGE_WITHOUT_CUSTOM, 179018)
        assert facts.custom_page_ids == ()
        assert facts.has_onsen_page is True

    def test_中身が無ければ利用不可として返す(self) -> None:
        assert parse_top_page("", 1).is_available is False
        assert parse_top_page("<html><body>404</body></html>", 1).is_available is False

    def test_読めない項目は推測で埋めない(self) -> None:
        facts = parse_top_page(TOP_PAGE_WITHOUT_CUSTOM, 179018)
        assert facts.review_count is None
        assert facts.photo_count is None


class TestParseDetailPage:
    def test_基本情報を取り出す(self) -> None:
        facts = parse_detail_page(DETAIL_PAGE)
        assert facts.room_count == 471
        assert facts.postal_code == "530-0001"
        assert facts.prefecture == "大阪府"
        assert facts.city == "大阪市北区梅田3-3-55"

    def test_設備を見出しごとに分けて取る(self) -> None:
        facts = parse_detail_page(DETAIL_PAGE)
        assert facts.facilities == ("レストラン", "会議室", "大浴場")
        assert facts.room_facilities == ("テレビ", "浴衣")

    def test_風呂の種類を取る(self) -> None:
        facts = parse_detail_page(DETAIL_PAGE)
        assert "温泉" in facts.bath_types
        assert "天然温泉" in facts.bath_types

    def test_チェックイン時刻を取る(self) -> None:
        facts = parse_detail_page(DETAIL_PAGE)
        assert facts.check_in is not None and facts.check_in.startswith("14:00")
        assert facts.check_out == "12:00"

    def test_空なら全てNone(self) -> None:
        facts = parse_detail_page("")
        assert facts.room_count is None
        assert facts.facilities == ()


class TestSplitAddress:
    def test_郵便番号と都道府県を切り出す(self) -> None:
        assert split_address("〒530-0001大阪府大阪市北区梅田3-3-55") == (
            "530-0001",
            "大阪府",
            "大阪市北区梅田3-3-55",
        )

    def test_ハイフン無しの郵便番号も整える(self) -> None:
        postal, pref, _ = split_address("〒5300001大阪府大阪市北区")
        assert postal == "530-0001"
        assert pref == "大阪府"

    def test_未知の住所は都道府県をNoneにする(self) -> None:
        assert split_address("Somewhere") == (None, None, "Somewhere")

    def test_Noneはそのまま(self) -> None:
        assert split_address(None) == (None, None, None)


class TestParseAreaListPage:
    def test_1ページ分の施設を取り出す(self) -> None:
        entries = parse_area_list_page(AREA_LIST_PAGE)
        assert len(entries) == 2
        first = entries[0]
        assert first.hotel_no == 9219
        assert first.name == "メルキュール鳥取大山リゾート＆スパ"
        assert first.review_average == Decimal("4.13")
        assert first.prefecture == "鳥取県"
        assert first.min_charge == 4500

    def test_定型リンクを施設名として拾わない(self) -> None:
        entries = parse_area_list_page(AREA_LIST_PAGE)
        assert all(e.name not in ("宿泊プラン一覧", "お客さまの声") for e in entries)

    def test_空なら空を返す(self) -> None:
        assert parse_area_list_page("") == ()
        assert parse_area_list_page("<html><body>該当なし</body></html>") == ()


class TestCountAreaListBlocks:
    """カードの数と読み取れた件数がズレたら気づけるようにする。"""

    def test_カードの数を数える(self) -> None:
        assert count_area_list_blocks(AREA_LIST_PAGE) == 2

    def test_読み取れない壊れたカードも1件として数える(self) -> None:
        # 「カードは30あるのに2件しか読めない」を検知するための数え方。
        broken = AREA_LIST_PAGE + '<div class="hotelBox"><p>読めない</p></div>'
        assert count_area_list_blocks(broken) == 3
        assert len(parse_area_list_page(broken)) == 2

    def test_空なら0(self) -> None:
        assert count_area_list_blocks("") == 0
