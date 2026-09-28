"""2026-09-28の本人回答による選択肢の変換。外部I/Oを持たない。"""
from __future__ import annotations

FIRST_TOUCH = {
    "テレアポ": "営業", "メルアポ": "営業", "展示会": "展示会", "紹介": "紹介",
    "お問合せ": "問合せ（その他）", "横展開・追加提案": "その他",
    "メルマガ（CRMプロジェクト）": "問合せ（その他）", "引継ぎ": "その他",
    "ホワイトペーパー": "問合せ（その他）", "個別相談（先方から営業個人へ）": "営業",
    "パートナー": "代理店",
}
CONTROLLER_ORDER = ("リンカーン", "手間いらず", "ねっぱん", "らく通")
CONTROLLERS = frozenset((*CONTROLLER_ORDER, "Beds24", "エアホスト", "なし", "不明"))


def choices(value, allowed):
    """不明値・空欄を部分的に捨てず、項目全体を保留する。"""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if any(not isinstance(v, str) or v not in allowed for v in value):
        return None
    return list(dict.fromkeys(value))


def first_touch(value):
    values = choices(value, FIRST_TOUCH)
    return FIRST_TOUCH[values[0]] if values else None


def primary_controller(value):
    values = choices(value, CONTROLLERS)
    if not values:
        return None
    return min(values, key=lambda v: (CONTROLLER_ORDER.index(v) if v in CONTROLLER_ORDER else 4, v))


def merge_choice_memo(existing, name, values, *, limit):
    """項目専用枠だけを差し替え、本文と他項目の枠を保持する。切詰めはしない。"""
    if existing is None:
        existing = ""
    if not isinstance(existing, str):
        raise ValueError("備考の型が不正です")
    begin, end = f"【CRM同期:{name}】", f"【/CRM同期:{name}】"
    block = begin + "\n" + "、".join(values) + "\n" + end
    if begin in existing or end in existing:
        if existing.count(begin) != 1 or existing.count(end) != 1:
            raise ValueError("同期メモの枠が不正です")
        start, finish = existing.index(begin), existing.index(end)
        if finish < start:
            raise ValueError("同期メモの枠が不正です")
        result = existing[:start] + block + existing[finish + len(end):]
    else:
        result = existing + ("\n\n" if existing else "") + block
    if len(result) > limit:
        raise ValueError("備考の文字数上限を超えるため保持します")
    return result


def controller_from_external(value, memo):
    """単一欄への折り畳みで失った選択を保存枠から復元する。削除は推測しない。"""
    aliases = {"TLリンカーン": "リンカーン", "TEMAIRAZU": "手間いらず", "ねっぱん！": "ねっぱん"}
    if not isinstance(value, str) or not isinstance(memo, (str, type(None))):
        return None
    values = [aliases.get(v.strip(), v.strip()) for v in value.split(",") if v.strip()]
    begin, end = "【CRM同期:サイトコントローラー】", "【/CRM同期:サイトコントローラー】"
    memo = memo or ""
    if begin not in memo and end not in memo:
        # 保存枠のない既存レコードから複数選択の削除を推測しない。
        return None
    if begin in memo or end in memo:
        if memo.count(begin) != 1 or memo.count(end) != 1 or memo.index(begin) > memo.index(end):
            return None
        saved = memo.split(begin)[1].split(end)[0].strip().split("、")
        values = saved + values
    return choices(values, CONTROLLERS)


def split_controller_memo(value):
    """自動枠と利用者本文を分離する。不正な枠を削除と解釈しない。"""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("備考の型が不正です")
    begin, end = "【CRM同期:サイトコントローラー】", "【/CRM同期:サイトコントローラー】"
    if begin not in value and end not in value:
        return value, ""
    if value.count(begin) != 1 or value.count(end) != 1 or value.index(begin) > value.index(end):
        raise ValueError("同期メモの枠が不正です")
    start, finish = value.index(begin), value.index(end) + len(end)
    prefix = value[:start]
    if prefix.endswith("\n\n"):
        prefix = prefix[:-2]
    return prefix + value[finish:], value[start:finish]


def replace_memo_body(existing, incoming, *, limit=2000):
    """本文だけの編集でも、別項目の全選択保存枠を落とさない。"""
    _, block = split_controller_memo(existing)
    body, _ = split_controller_memo(incoming)
    result = body + ("\n\n" if body and block else "") + block
    if len(result) > limit:
        raise ValueError("備考の文字数上限を超えるため保持します")
    return result
