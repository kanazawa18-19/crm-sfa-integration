"""ルーター間で共有するFastAPIの依存性（2026-08-28にsrc/api/app.pyから分割）。

`app.py`とルーター（`src/api/routes/`）の両方から同じオブジェクトを参照する必要があるため、
ここに置く。特に`wiring_dependency`は、テストが
`app.dependency_overrides[wiring_dependency]`で差し替える**キーそのもの**であり、
定義が2箇所に分かれると差し替えが効かなくなる。
"""

from __future__ import annotations

from src.sync_engine.production_wiring import ProductionSyncWiring, get_production_wiring


def wiring_dependency() -> ProductionSyncWiring:
    """本番用のDispatcher一式（`src/sync_engine/production_wiring.py`）を返すFastAPI依存性。

    プロセス内シングルトンのため毎リクエストで作り直さない
    （`dashboard_service.py`のモジュールレベルキャッシュと同じ流儀）。テストでは
    `app.dependency_overrides[wiring_dependency]`で差し替える。
    """
    return get_production_wiring()
