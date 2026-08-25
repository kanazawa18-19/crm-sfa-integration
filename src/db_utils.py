"""複数モジュールで共有する小さなDBまわりの純粋関数群(2026-08-25)。

`src/migration/_utils.py`と同じ位置付け(特定モジュールに属さない共有ヘルパー)。
"""

from __future__ import annotations

from datetime import datetime, timezone


def db_truncated_utcnow() -> datetime:
    """`datetime.now(timezone.utc)`のマイクロ秒をミリ秒境界(1000の倍数)へ事前に切り捨てた
    UTC日時を返す。Postgresの`TIMESTAMP(3)`(ミリ秒精度)カラムに保存してもズレない値になる。

    `src/project_mirror/db.py`の`upsert_projects_and_sweep()`・`src/relation_sync/db.py`の
    `upsert_client_names_and_sweep()`はいずれもmark-and-sweep方式(1トランザクション内で
    全件UPSERT→今回触れられなかった行を`"syncedAt" < 基準時刻`でDELETE)を使っている。

    PostgreSQLの`TIMESTAMP(3)`は値を単純に切り捨てるのではなく四捨五入(round-half-up)で
    ミリ秒精度に丸める(例: `927999`マイクロ秒は切り捨てなら`927000`だが、実際は繰り上がって
    `928000`として保存される)。ここでPython側のマイクロ秒精度の値をそのまま基準時刻に
    使うと、INSERT時にPostgresがこの丸めを適用して保存する一方、DELETEのWHERE比較には
    Python側の丸められていない元の値がそのまま使われるため、丸め方向次第で
    `保存値 < 比較用の元の値`が真になり、今まさに挿入したばかりの行まで誤って削除される
    事故が本番で発生した(実データで再現確認済み、2026-08-25)。

    この関数はマイクロ秒を事前に1000の倍数(＝ミリ秒境界)へ切り捨てて返す。1000の倍数は
    四捨五入しても切り捨てても値が変わらない不動点になるため、Postgresの丸めルールが
    切り捨てか繰り上げかに関わらず保存後の値と常に完全一致し、上記の誤削除を防げる。
    """
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)
