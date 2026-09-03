"""NotionページIDの表記ゆれ吸収（2026-09-03）。

NotionのページIDはハイフン有り（`3ced8ea8-...`）と無しの両方の形で流通する。
配信停止は「連絡先ページIDで突き合わせる」処理が3か所（署名・DBの保存値・除外判定）に
分かれるため、**どこか1か所でも生の値のまま比較すると、同じ人を別人として扱う**。
比較・保存・署名に使う形はこの関数が返す形（小文字・ハイフン無し）に統一する。

`dashboard/lib/bulkEmailUnsubscribe.ts`が同じ正規化を実装している（配信停止リンクは
Python側が発行し、TypeScript側が検証するため。片方だけ直すとリンクが全部壊れる）。
"""

from __future__ import annotations


def normalize_page_id(page_id: str) -> str:
    return (page_id or "").strip().lower().replace("-", "")
