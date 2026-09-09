"""接続前の許可一覧と、プロセス間で共有する停止台帳。"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import time

LEGACY_PROJECT = "cnctor-crm-cap-trial-260910"
PROJECT = "actionpoint-autocalc"
DATABASE = "crm-capacity-trial-260910"
BASE = f"projects/{PROJECT}/databases/{DATABASE}"
ACCOUNTS = {role: f"capacity-trial-{role}@{PROJECT}.iam.gserviceaccount.com"
            for role in ("runner", "observer")}
SCOPES = frozenset(["trial-smoke", "trial-permission", "trial-invalid", "trial-stop"]
    + [f"trial-concurrency-{n}" for n in range(1, 6)]
    + [f"trial-loss-{name}" for name in ("initialize", "enqueue", "claim", "finish", "recover")]
    + [f"trial-scan-{n}" for n in (1103, 10000, 100000)])
ALLOWED_ENV = frozenset({"HOME", "PATH", "LANG", "LC_ALL", "PYTHONNOUSERSITE",
                         "CAPACITY_TRIAL_CHILD", "__CF_USER_TEXT_ENCODING"})
LIMITS = {"reads": 3_000_000, "writes": 500_000, "deletes": 200_000,
          "runtime_seconds": 86_400, "sql_connections": 1000, "sql_statements": 10000}


ERROR_CODES = frozenset({"guard_refused", "environment_rejected", "target_rejected",
    "ledger_missing", "budget_stopped", "limit_exceeded", "cost_evidence_invalid",
    "credential_unavailable", "trial_timeout", "child_failed", "unexpected_error"})
_REASON_CODES = {
    "許可外の環境変数または専用子プロセスでない起動": "environment_rejected",
    "HOMEは空の専用ディレクトリが必要": "environment_rejected",
    "ユーザーのPython設定を継承できません": "environment_rejected",
    "承認一覧にないFirestore接続先": "target_rejected",
    "専用Neon以外の接続先または接続上書き設定": "target_rejected",
    "Neon接続文字列が不正": "target_rejected",
    "予算台帳が未初期化": "ledger_missing",
    "期限・費用停止値・費用取得途絶のため新規実行停止": "budget_stopped",
    "操作回数または稼働時間の上限": "limit_exceeded",
    "費用実測値・時刻・証跡参照が必要": "cost_evidence_invalid",
    "費用根拠が不正": "cost_evidence_invalid",
    "累計費用または観測時刻を戻せません": "cost_evidence_invalid",
    "専用SAのキーチェーン短命tokenが取得できません": "credential_unavailable",
    "専用キーチェーン認証情報が取得できません": "credential_unavailable",
    "短命SA tokenが未設定または不正": "credential_unavailable",
    "60秒で子を停止。結果不明の枠は自動回収しません": "trial_timeout",
    "走査60秒超過。部分結果を破棄": "trial_timeout",
    "隔離子プロセス失敗。部分結果は返しません": "child_failed",
}


class Refused(RuntimeError):
    """説明文は内部だけで保持し、公開出力は固定コードに限定する。"""
    def __init__(self, message, *, code=None):
        super().__init__(message)
        candidate = code if code is not None else _REASON_CODES.get(message, "guard_refused")
        self.code = candidate if candidate in ERROR_CODES else "guard_refused"


def validate_environment(env=None):
    env = dict(os.environ if env is None else env)
    if set(env) - ALLOWED_ENV or env.get("CAPACITY_TRIAL_CHILD") != "1":
        raise Refused("許可外の環境変数または専用子プロセスでない起動")
    home = Path(env.get("HOME", ""))
    if not home.is_absolute() or not home.is_dir() or any(home.iterdir()):
        raise Refused("HOMEは空の専用ディレクトリが必要")
    if env.get("PYTHONNOUSERSITE") != "1":
        raise Refused("ユーザーのPython設定を継承できません")


def validate_target(project, database, scope, slots=3, role="runner"):
    if (project != PROJECT or database != DATABASE or scope not in SCOPES
            or slots != 3 or role not in ACCOUNTS):
        raise Refused("承認一覧にないFirestore接続先")


NEON_PROJECT = "fragrant-silence-24771784"
NEON_BRANCH = "br-silent-king-avubki1s"
NEON_ENDPOINT = "ep-shiny-silence-avof39pi"
NEON_HOST = "ep-shiny-silence-avof39pi.c-11.us-east-1.aws.neon.tech"


def validate_neon(dsn):
    from psycopg.conninfo import conninfo_to_dict
    try:
        values = conninfo_to_dict(dsn)
    except Exception:
        raise Refused("Neon接続文字列が不正") from None
    allowed = {"host", "port", "dbname", "user", "password", "sslmode", "channel_binding"}
    if (set(values)-allowed or values.get("host") != NEON_HOST
            or values.get("dbname") != "neondb" or values.get("user") != "neondb_owner"
            or values.get("port", "5432") != "5432" or not values.get("password")
            or values.get("sslmode") not in {"require", "verify-full"}
            or values.get("channel_binding", "require") != "require"):
        raise Refused("専用Neon以外の接続先または接続上書き設定")
    # この仮想環境の既存certifi CAだけを使い、環境変数から証明書を継承しない。
    try:
        import certifi
    except ImportError:
        raise Refused("信頼済みCA bundleを読み込めません", code="credential_unavailable") from None
    ca_bundle = Path(certifi.where())
    if not ca_bundle.is_absolute() or not ca_bundle.is_file():
        raise Refused("信頼済みCA bundleが存在しません")
    values.update(sslmode="verify-full", sslrootcert=str(ca_bundle), channel_binding="require",
                  connect_timeout="10", application_name="capacity_trial_probe",
                  options="-c statement_timeout=10000 -c lock_timeout=2000")
    return values


def validate_resource(value):
    if not isinstance(value, str):
        raise Refused("資源名が不正")
    if value == BASE:
        return
    prefix = BASE + "/documents/sync_capacity_scopes/"
    if not value.startswith(prefix):
        raise Refused("許可外のFirestore資源")
    parts = value[len(prefix):].split("/")
    if parts[0] not in SCOPES or not (len(parts) == 1 or
            (len(parts) == 3 and parts[1] == "jobs" and parts[2])):
        raise Refused("許可外のscopeまたは文書パス")


class Ledger:
    """実行前に上限を予約する。中断時の予約は戻さず、課金実績とは分ける。"""
    def __init__(self, path):
        self.path = Path(path)

    def transact(self, operation):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.path.exists():
                raise Refused("予算台帳が未初期化")
            data = json.loads(self.path.read_text())
            result = operation(data)
            tmp = self.path.with_suffix(".new")
            with tmp.open("w") as output:
                json.dump(data, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, self.path)
            return result

    @classmethod
    def initialize(cls, path, created_at, now=None):
        now = time.time() if now is None else now
        if not math.isfinite(created_at) or not 0 <= now-created_at < 14*86400:
            raise Refused("資源作成日時が不正または14日超過")
        data = {"project": PROJECT, "database": DATABASE, "created_at": created_at,
                "cost": None, "reserved": dict.fromkeys(LIMITS, 0),
                "rpc_calls": {}, "returned_documents": 0}
        with Path(path).open("x") as output:
            json.dump(data, output)
        return cls(path)

    def migrate_target(self, now=None):
        """未使用の旧Firestore接続先だけ移す。期限・予約・費用履歴は保持する。"""
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise Refused("移行日時が不正")
        def update(data):
            if (data.get("project") != LEGACY_PROJECT
                    or data.get("database", "(default)") != "(default)"
                    or any(data["reserved"].get(kind) != 0 for kind in ("reads", "writes", "deletes"))
                    or data.get("rpc_calls") != {} or data.get("returned_documents") != 0):
                raise Refused("未使用の旧Firestore台帳だけ移行できます")
            data.update(project=PROJECT, database=DATABASE,
                        target_migration={"from_project": LEGACY_PROJECT,
                                          "from_database": "(default)", "at": now},
                        cost_refresh_required=True)
        self.transact(update)

    def cost(self, amount, observed_at, evidence, now=None, basis="metered"):
        now = time.time() if now is None else now
        if basis not in {"metered", "free-plan-verified"} or (basis == "free-plan-verified" and amount != 0):
            raise Refused("費用根拠が不正")
        if (not math.isfinite(amount) or amount < 0 or not math.isfinite(observed_at)
                or not 0 <= now-observed_at <= 3600 or not evidence.strip()):
            raise Refused("費用実測値・時刻・証跡参照が必要")
        def update(data):
            if data.get("project") == PROJECT and basis != "metered":
                raise Refused("費用根拠が不正")
            if data.get("cost_refresh_required") and (
                    basis != "metered" or observed_at < data["target_migration"]["at"]):
                raise Refused("費用根拠が不正")
            old = data.get("cost")
            if old and (amount < old["usd"] or observed_at < old["observed_at"]):
                raise Refused("累計費用または観測時刻を戻せません")
            data["cost"] = {"usd": amount, "observed_at": observed_at, "evidence": evidence, "basis": basis}
            data["cost_refresh_required"] = False
        self.transact(update)

    def reserve(self, *, now=None, **amounts):
        now = time.time() if now is None else now
        def update(data):
            cost = data.get("cost")
            if (data.get("project") != PROJECT or data.get("database") != DATABASE
                    or data.get("cost_refresh_required") or not 0 <= now-data["created_at"] < 14*86400
                    or not cost or not 0 <= now-cost["observed_at"] <= 3600
                    or cost.get("basis") != "metered" or cost["usd"] >= 10):
                raise Refused("期限・費用停止値・費用取得途絶のため新規実行停止")
            for kind, amount in amounts.items():
                if (kind not in LIMITS or not isinstance(amount, (int, float))
                        or not math.isfinite(amount) or amount < 0
                        or data["reserved"].get(kind, 0)+amount > LIMITS[kind]):
                    raise Refused("操作回数または稼働時間の上限")
            for kind, amount in amounts.items():
                data["reserved"][kind] = data["reserved"].get(kind, 0)+amount
        self.transact(update)

    def record(self, method, documents=0):
        def update(data):
            data["rpc_calls"][method] = data["rpc_calls"].get(method, 0)+1
            data["returned_documents"] += documents
        self.transact(update)
