"""公式emulator限定の独立子。試験対象のCLIや実サービス認証へは接続しない。"""
import json
import os
from pathlib import Path
import sys


def configure(project, monkeypatch=None):
    from scripts.capacity_trial import guard, firestore, small_policy, retry_policy
    if not project.startswith("demo-retry-"):
        raise RuntimeError("架空のローカル試験project専用")
    for module in (guard, firestore, small_policy, retry_policy):
        for key, value in (("PROJECT", project),
                           ("BASE", "projects/" + project + "/databases/crm-capacity-trial-260910")):
            if hasattr(module, key):
                if monkeypatch is None:
                    setattr(module, key, value)
                else:
                    monkeypatch.setattr(module, key, value)


def local_store(scope, token, ledger, *, role="runner"):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore
    from scripts.capacity_trial.firestore import GuardedAPI
    from scripts.capacity_trial.guard import DATABASE, PROJECT
    from src.sync_capacity.firestore_store import FirestoreJobStore, Settings
    host = os.environ["FIRESTORE_EMULATOR_HOST"]
    if host != "127.0.0.1:8787":
        raise RuntimeError("公式ローカルemulator専用")
    # SDKはemulator環境変数でlocalhostのみに接続する。ガードには本来の資源名を通す。
    client = firestore.Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())
    client._firestore_api_internal = GuardedAPI(client._firestore_api, ledger, role=role)
    return FirestoreJobStore(Settings("demo-retry-local", DATABASE, scope, 3), client=client)


def main():
    from scripts.capacity_trial import retry_policy as policy
    from scripts.capacity_trial import concurrency, firestore
    from scripts.capacity_trial.guard import Ledger
    configure(sys.argv[2])
    policy.wall_time = lambda: 1788990000.0
    path = Path(sys.argv[1])
    policy.LEDGER_PATH = path
    payload = json.loads(sys.stdin.readline())
    ledger = Ledger(path)
    policy.attach(ledger, "runner", payload["ticket"], child=True)
    firestore.make_store = local_store
    concurrency.validate_environment = lambda: None
    result = concurrency.claim_child(policy.SCOPE, ledger, token=payload["token"])
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
