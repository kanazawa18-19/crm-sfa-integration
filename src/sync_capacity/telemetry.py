"""本文を含めず専用stderrへ計測JSONを出す。他ライブラリの設定は変えない。"""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("sync_capacity.telemetry")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def emit(event, *, level=logging.INFO, **fields):
    try:
        values = {"event": event, **fields,
                  "timestamp": datetime.now(timezone.utc).isoformat(),
                  "severity": logging.getLevelName(level)}
        logger.log(level, "sync_capacity %s", json.dumps(values, sort_keys=True),
                   extra={"sync_capacity": values})
    except Exception:
        # ログ出力先の故障で保存済みjobを未受付扱いにしたり、処理を再送しない。
        pass
