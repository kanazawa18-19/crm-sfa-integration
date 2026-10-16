"""今回の本文を残し、明示された過去メールの引用を除く。"""
import re


def current_body(text: str) -> str:
    lines = text.splitlines(keepends=True)
    kept = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        # 日付だけの本文や署名の罫線を引用開始と誤認しない。
        delimiter = (
            re.fullmatch(r"On .+wrote:", line, re.I)
            or re.fullmatch(r"\d{4}年\d{1,2}月\d{1,2}日.+\d{1,2}:\d{2}.+<[^<>\s]+@[^<>\s]+>[:：]", line)
            or re.fullmatch(r".+(?:さん|様)は書きました[:：]", line)
            or re.fullmatch(r"[-ー]{2,}\s*(?:Original Message|Forwarded message|転送メッセージ)\s*[-ー]{2,}", line, re.I)
        )
        if re.match(r"^(?:From|差出人)[:：]", line, re.I):
            following = "".join(lines[i + 1:i + 7])
            delimiter = delimiter or (
                re.search(r"(?im)^(?:Sent|Date|送信日時|日時)[:：]", following)
                and re.search(r"(?im)^(?:To|宛先|Subject|件名)[:：]", following)
            )
        if delimiter:
            # 引用記号付きの返信では、引用の間や後に書いた今回の回答も残す。
            first = next((value.strip() for value in lines[i + 1:] if value.strip()), "")
            if first.startswith(">"):
                continue
            return "".join(kept).rstrip()
        # 行内返信は引用行だけを除き、その後に書かれた回答を保持する。
        if not line.startswith(">"):
            kept.append(raw)
    return "".join(kept)
