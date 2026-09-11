"""楽天トラベルの公開施設ページ(HTML)から施設情報を取り出す(2026-09-11)。

**HTTPは行わない。** 引数で受け取ったHTML文字列を解析するだけの純粋関数群にしてあり、
保存したHTMLでそのまま単体テストできる(取得は`rakuten_client.py`の担当)。

対象は2種類のページ:

  https://travel.rakuten.co.jp/HOTEL/{no}/{no}.html       施設トップ
      施設名 / クチコミ点数・件数 / 写真枚数 / カスタマイズページの有無 / 温泉ページの有無
  https://travel.rakuten.co.jp/HOTEL/{no}/{no}_std.html   詳細情報
      住所・郵便番号 / 総部屋数 / 館内設備 / 部屋設備 / 風呂の種類・泉質

抽出箇所は2026-09-11に実データで確認した。楽天側のHTML変更で壊れうるため、
`parse_*`は**見つからなければNoneを返す**(推測で埋めない)。取り込みバッチ側で
「主要項目がNoneの施設が急増していないか」を見て変更を検知する。
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# 都道府県。住所文字列の先頭一致で判定する(「〒530-0001大阪府大阪市北区…」の形)。
PREFECTURES: tuple[str, ...] = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)


@dataclass(frozen=True)
class TopPageFacts:
    """施設トップページから読み取れた事実。読み取れなかった項目はNone。"""

    name: str | None = None
    review_average: Decimal | None = None
    review_count: int | None = None
    photo_count: int | None = None
    custom_page_ids: tuple[str, ...] = ()
    has_onsen_page: bool = False
    is_available: bool = True  # 施設ページとして成立しているか(掲載終了・404でFalse)


@dataclass(frozen=True)
class DetailPageFacts:
    """詳細情報ページ(`_std.html`)から読み取れた事実。"""

    postal_code: str | None = None
    address: str | None = None
    prefecture: str | None = None
    city: str | None = None
    room_count: int | None = None
    facilities: tuple[str, ...] = ()
    room_facilities: tuple[str, ...] = ()
    bath_types: tuple[str, ...] = ()
    check_in: str | None = None
    check_out: str | None = None


def _unescape(value: str) -> str:
    return html_module.unescape(value).replace(" ", " ").strip()


def _to_text_lines(html: str) -> list[str]:
    """タグを落として、意味のある行だけの一覧にする。

    楽天のページは表のセル1つ1つが別要素なので、タグ境界で改行すると
    「総部屋数」「471室」のように**見出しと値が隣り合う行**として並ぶ。
    この性質を使って値を拾う(`_value_after`)。
    """
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    lines = []
    for raw in text.split("\n"):
        line = _unescape(re.sub(r"[ \t　]+", " ", raw))
        if line:
            lines.append(line)
    return lines


def _value_after(lines: list[str], label: str, *, max_ahead: int = 2) -> str | None:
    """`label`と完全一致する行の直後にある値を返す。"""
    for i, line in enumerate(lines):
        if line == label:
            for j in range(i + 1, min(i + 1 + max_ahead, len(lines))):
                candidate = lines[j]
                if candidate and candidate != label:
                    return candidate
    return None


def _collect_between(lines: list[str], start: str, stops: tuple[str, ...]) -> tuple[str, ...]:
    """`start`の次の行から、`stops`のいずれかに当たるまでの行を集める。"""
    try:
        begin = lines.index(start)
    except ValueError:
        return ()
    collected: list[str] = []
    for line in lines[begin + 1 :]:
        if line in stops:
            break
        collected.append(line)
        if len(collected) > 80:  # 見出しを取り違えたときの暴走止め
            break
    return tuple(collected)


def parse_top_page(html: str, hotel_no: int) -> TopPageFacts:
    """施設トップページを解析する。"""
    if not html or len(html) < 2000:
        return TopPageFacts(is_available=False)

    name = None
    title = re.search(r"<title>([^<]*)</title>", html)
    if title:
        # 「ハートンホテル西梅田 宿泊予約【楽天トラベル】」から施設名だけ取る
        name = _unescape(re.split(r"\s*宿泊予約", title.group(1))[0]) or None
    if not name:
        return TopPageFacts(is_available=False)

    review_average = None
    m = re.search(r'property="ratingValue"\s+content="([0-9.]+)"', html)
    if m:
        try:
            review_average = Decimal(m.group(1))
        except InvalidOperation:
            review_average = None

    review_count = None
    m = re.search(r'property="reviewCount"\s+content="(\d+)"', html)
    if m:
        review_count = int(m.group(1))

    photo_count = None
    m = re.search(r"写真・動画\s*\(\s*([\d,]+)", html)
    if m:
        photo_count = int(m.group(1).replace(",", ""))

    # カスタマイズページは `GW{施設番号}{連番}.html` という固定の形で出る
    # (2026-09-11、大阪市30施設で実測)。同じページが複数箇所から貼られるため重複を除く。
    custom_page_ids = tuple(sorted(set(re.findall(rf"(GW{hotel_no}\d+)\.html", html))))

    has_onsen_page = bool(re.search(rf"HOTEL/{hotel_no}/{hotel_no}_onsen\.html", html))

    return TopPageFacts(
        name=name,
        review_average=review_average,
        review_count=review_count,
        photo_count=photo_count,
        custom_page_ids=custom_page_ids,
        has_onsen_page=has_onsen_page,
        is_available=True,
    )


def split_address(address: str | None) -> tuple[str | None, str | None, str | None]:
    """「〒530-0001大阪府大阪市北区梅田3-3-55」を(郵便番号, 都道府県, 市区町村以降)に割る。"""
    if not address:
        return None, None, None
    postal = None
    m = re.match(r"〒?\s*(\d{3}-?\d{4})\s*(.*)$", address)
    rest = address
    if m:
        postal = m.group(1)
        if len(postal) == 7:
            postal = f"{postal[:3]}-{postal[3:]}"
        rest = m.group(2)
    for pref in PREFECTURES:
        if rest.startswith(pref):
            return postal, pref, rest[len(pref) :].strip() or None
    return postal, None, rest.strip() or None


def parse_detail_page(html: str) -> DetailPageFacts:
    """詳細情報ページ(`_std.html`)を解析する。"""
    if not html or len(html) < 2000:
        return DetailPageFacts()

    lines = _to_text_lines(html)

    room_count = None
    raw_rooms = _value_after(lines, "総部屋数")
    if raw_rooms:
        m = re.search(r"([\d,]+)\s*室", raw_rooms)
        if m:
            room_count = int(m.group(1).replace(",", ""))

    address_raw = _value_after(lines, "住所")
    postal, prefecture, city = split_address(address_raw)

    facilities = _collect_between(
        lines, "館内設備", ("部屋設備・備品", "その他設備・サービス", "特典", "食事場所", "風呂")
    )
    room_facilities = _collect_between(
        lines, "部屋設備・備品", ("その他設備・サービス", "特典", "食事場所", "風呂", "館内設備")
    )

    bath_types: tuple[str, ...] = ()
    for line in lines:
        if line.startswith("[種類]"):
            bath_types = tuple(p for p in re.split(r"[\s　]+", line[len("[種類]") :]) if p)
            break

    check_in = None
    check_out = None
    raw_in = _value_after(lines, "チェックイン")
    if raw_in:
        check_in = raw_in
    raw_out = _value_after(lines, "チェックアウト")
    if raw_out:
        check_out = raw_out

    return DetailPageFacts(
        postal_code=postal,
        address=address_raw,
        prefecture=prefecture,
        city=city,
        room_count=room_count,
        facilities=facilities,
        room_facilities=room_facilities,
        bath_types=bath_types,
        check_in=check_in,
        check_out=check_out,
    )


@dataclass(frozen=True)
class AreaListEntry:
    """都道府県のエリア一覧ページ(30件/ページ)に出ている1施設分の情報。

    一覧には**施設名・クチコミ点数・住所・最安料金**が載っている。施設ページを1軒ずつ
    開かなくてもここまで取れるため、都道府県とクチコミ点数での一次絞り込みはこの
    一覧だけで済む(取得リクエスト数が約1/30になる)。
    客室数・館内設備・カスタマイズページの有無は一覧に無いので、絞り込んだ後に
    施設ページを取りに行く。
    """

    hotel_no: int
    name: str
    review_average: Decimal | None = None
    postal_code: str | None = None
    prefecture: str | None = None
    city: str | None = None
    address: str | None = None
    min_charge: int | None = None


def parse_area_list_page(html: str) -> tuple[AreaListEntry, ...]:
    """エリア一覧ページから施設を取り出す。

    URL例: `https://hotel.travel.rakuten.co.jp/hotellist/area/tottori/?f_page=2`
    1施設が `<div class="hotelBox">` 1つに対応する(2026-09-11実測)。
    """
    if not html:
        return ()

    blocks = [b for b in re.split(r'(?=<div[^>]*class="hotelBox")', html) if 'class="hotelBox"' in b]
    entries: list[AreaListEntry] = []

    for block in blocks:
        m = re.search(r"f_hotel_no=(\d+)", block)
        if not m:
            continue
        hotel_no = int(m.group(1))

        plain = re.sub(r"<[^>]+>", " ", block)
        plain = html_module.unescape(plain)

        # 施設名は最初のアンカーテキスト(「宿泊プラン一覧」等の定型リンクより前に出る)。
        name = None
        for a in re.finditer(r"<a[^>]*>([^<]{2,60})</a>", block):
            candidate = _unescape(a.group(1))
            if candidate and candidate not in (
                "宿泊プラン一覧",
                "航空券付き宿泊プラン",
                "特徴",
                "地図・アクセス",
                "写真",
                "お客さまの声",
            ):
                name = candidate
                break
        if not name:
            continue

        review_average = None
        r = re.search(r"お客さまの声[^0-9]{0,40}([0-9]\.[0-9]+)", plain)
        if r:
            try:
                review_average = Decimal(r.group(1))
            except InvalidOperation:
                review_average = None

        postal = prefecture = city = None
        address = None
        a = re.search(r"(〒\s*\d{3}-?\d{4}[^\n\r]{2,80})", html_module.unescape(re.sub(r"<[^>]+>", "\n", block)))
        if a:
            address = _unescape(a.group(1))
            postal, prefecture, city = split_address(address)

        min_charge = None
        c = re.search(r"最安料金\]?\s*([\d,]+)", plain)
        if c:
            min_charge = int(c.group(1).replace(",", ""))

        entries.append(
            AreaListEntry(
                hotel_no=hotel_no,
                name=name,
                review_average=review_average,
                postal_code=postal,
                prefecture=prefecture,
                city=city,
                address=address,
                min_charge=min_charge,
            )
        )

    return tuple(entries)
