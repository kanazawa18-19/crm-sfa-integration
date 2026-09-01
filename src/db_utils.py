"""複数モジュールで共有する小さなDBまわりの純粋関数群(2026-08-25)。

`src/migration/_utils.py`と同じ位置付け(特定モジュールに属さない共有ヘルパー)。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

# advisory lock専用の接続文字列を保持する環境変数名(非pooled/direct接続を想定、2026-08-28)。
# `DATABASE_URL_UNPOOLED`はVercel×Neon連携で自動的に用意されることが多い命名に合わせている。
_ADVISORY_LOCK_URL_ENV_VAR = "DATABASE_URL_UNPOOLED"
_FALLBACK_URL_ENV_VAR = "DATABASE_URL"


def db_truncated_utcnow() -> datetime:
    """`datetime.now(timezone.utc)`のマイクロ秒をミリ秒境界(1000の倍数)へ事前に切り捨てた
    UTC日時を返す。Postgresの`TIMESTAMP(3)`(ミリ秒精度)カラムに保存してもズレない値になる。

    `src/project_mirror/db.py`の`upsert_projects_and_sweep()`・`src/relation_sync/db.py`の
    `upsert_client_names_and_sweep()`はいずれもmark-and-sweep方式(1トランザクション内で
    全件UPSERT→今回触れられなかった行を`"syncedAt" < 基準時刻`でDELETE)を使っている。

    **2026-09-01に分割実行を新設し、同じmark-and-sweepのペアが増えた**:
    `upsert_projects()`/`sweep_projects()`・`upsert_client_names()`/`sweep_client_names()`。
    こちらの基準時刻は`SyncCursor.pass_started_at`（`src/sync_engine/sync_cursor.py`の
    `load_cursor()`）であり、**そこでもこの関数を通している。**
    新しくmark-and-sweepを足すときは、基準時刻を必ずここへ通すこと
    （最初この保護が漏れており、動物チーム3体が独立に同じ箇所を指摘した）。

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


def ensure_utc(dt: datetime) -> datetime:
    """psycopg経由で読んだdatetime(タイムゾーン情報を持たない`TIMESTAMP(3)`列由来 — UTC値
    として保存されている前提)に、UTCのtzinfoを付与して返す。

    **既にtz-awareな場合はUTCへの正規化はせず、そのまま返す**(UTC以外のオフセットを持つ値を
    渡した場合、戻り値のtzinfoもそのオフセットのまま)。この関数の目的はtz-naive/awareの混在
    による`TypeError`を防ぐことであり、「戻り値のtzinfoは必ずUTC」という保証はしない
    (datetime同士の演算・比較はtzinfoが異なっていても正しく計算されるため、この用途では
    正規化は不要)。戻り値を`isoformat()`等でシリアライズしてUTC表記を期待する用途には
    使わないこと(その場合は呼び出し側で`astimezone(timezone.utc)`すること)。

    `datetime.now(timezone.utc)`のようなtz-awareな値と直接演算(`-`等)すると
    `TypeError: can't subtract offset-naive and offset-aware datetimes`になるため、DBから
    読んだdatetimeを扱う箇所では演算前に必ずこれを通すこと(2026-08-26、
    `gmail_sync.watch_registration._needs_renewal()`が`watchExpiration`との差分計算で
    このエラーを起こし本番cronがクラッシュしたインシデントの再発防止。
    詳細はdocs/gmail_sync_activation_note.md参照)。
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def connect_for_advisory_lock(logger: logging.Logger) -> psycopg.Connection[dict[str, Any]]:
    """`pg_try_advisory_lock()`/`pg_advisory_unlock()`専用の接続を作る。

    `src/document_generation/approval_db.py`の`try_acquire_approval_lock()`、
    `src/project_mirror/db.py`・`src/relation_sync/db.py`の`try_acquire_refresh_lock()`が
    共有する(元は3ファイルにほぼ同じ`_connect()`実装がコピーされていたが、advisory lock専用
    の接続作成はここに集約した。通常のSELECT/INSERT等のクエリは引き続き各モジュールの
    `_connect()`(`DATABASE_URL`)を使い続けてよく、この関数の対象外)。

    **なぜ通常のクエリ接続と分けるか**: Postgresのアドバイザリロックはセッション単位の状態
    であり、ロックを取得したセッション(コネクション)自身が明示的に解放するまで保持される
    という前提に立っている。ところがPgBouncerのtransaction pooling(Neonの`-pooler`
    エンドポイント等)は、クライアントから見た1つの「接続」の裏でトランザクションごとに
    異なる物理セッションを使い回すため、ロック取得(`pg_try_advisory_lock`)と解放
    (`pg_advisory_unlock`)が同じ物理セッション上で実行される保証が無くなる。この場合
    **例外は一切出ないまま、排他制御だけが無言で機能しなくなる**(2026-08-28、本番の
    `DATABASE_URL`がNeonのpooled接続だったことが判明して発覚。詳細は
    `docs/quote_approval_note.md`・`docs/relation_sync_activation_note.md`参照)。

    通常のSELECT/INSERT等の単発クエリはtransaction poolingでも問題なく動作するため
    (`DATABASE_URL`のpooled接続の利点をそのまま活かせる)、advisory lockを取得・解放する
    接続のみ非pooledの`DATABASE_URL_UNPOOLED`を使う。

    `DATABASE_URL_UNPOOLED`が設定されている場合でも、その値自体がpooled接続らしき場合
    (ホスト名に`-pooler`を含む)は警告する。専用の環境変数を用意していても値の貼り間違い
    (Vercelでの貼り付けミス、Neon側の命名規則変更等)で無言のまま同じ問題が再現しうるため
    (shirokuma-secレビューWARN対応、2026-08-28)。

    `DATABASE_URL_UNPOOLED`が未設定の場合は`DATABASE_URL`にフォールバックするが、無言で
    今回と同じ問題に戻ってしまうため、必ずwarningログを出す。さらにフォールバック先が
    pooled接続らしき場合(ホスト名に`-pooler`を含む)は、advisory lockが機能しない可能性が
    高いことをより強く警告する。この2つの警告は文面を分けており、ログを見た人が「フォール
    バックした」のか「専用変数の値自体がpooledに見える」のかを区別できるようにしている。
    **ログには接続文字列そのものやパスワードは一切出さない**(`-pooler`を含むかどうかの
    真偽値のみ判定に使い、ログメッセージにも埋め込まない)。
    """
    url = os.environ.get(_ADVISORY_LOCK_URL_ENV_VAR)
    if url:
        if "-pooler" in url:
            logger.warning(
                "%s is set but its value looks like a pooled connection (host contains "
                "'-pooler'); pg_try_advisory_lock/pg_advisory_unlock are very likely NOT "
                "providing mutual exclusion right now. Check the value of %s (this is not the "
                "fallback-to-%s warning — %s itself holds a pooled URL).",
                _ADVISORY_LOCK_URL_ENV_VAR,
                _ADVISORY_LOCK_URL_ENV_VAR,
                _FALLBACK_URL_ENV_VAR,
                _ADVISORY_LOCK_URL_ENV_VAR,
            )
    else:
        fallback_url = os.environ.get(_FALLBACK_URL_ENV_VAR)
        if not fallback_url:
            raise ValueError(
                f"{_ADVISORY_LOCK_URL_ENV_VAR} is not set, and {_FALLBACK_URL_ENV_VAR} "
                "(fallback) is not set either"
            )
        logger.warning(
            "%s is not set; advisory lock connection falls back to %s (pooled connections "
            "can silently break advisory lock acquire/release under PgBouncer transaction "
            "pooling — set %s to a non-pooled connection string to fix this).",
            _ADVISORY_LOCK_URL_ENV_VAR,
            _FALLBACK_URL_ENV_VAR,
            _ADVISORY_LOCK_URL_ENV_VAR,
        )
        if "-pooler" in fallback_url:
            logger.warning(
                "%s (used as advisory lock fallback) looks like a pooled connection "
                "(host contains '-pooler'); pg_try_advisory_lock/pg_advisory_unlock are very "
                "likely NOT providing mutual exclusion right now. Set %s immediately.",
                _FALLBACK_URL_ENV_VAR,
                _ADVISORY_LOCK_URL_ENV_VAR,
            )
        url = fallback_url
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")
