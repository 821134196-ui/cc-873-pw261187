"""演示数据：三个应用共享部分键，各自有覆盖值、依赖与版本约束。"""
from __future__ import annotations

from pathlib import Path

from .store import AppOverride, AppSpec, ConfigStore, KeyRevision

DEFAULT_DB = Path(__file__).parent.parent / "data" / "config-center.json"


def build_seed_store(path: str | Path = DEFAULT_DB) -> ConfigStore:
    store = ConfigStore(path)

    apps = [
        AppSpec("order-service", {
            "db.pool.size": "^1.0.0",
            "feature.checkout-v2": ">=2.0.0",
            "shared.timeout-ms": "~1.4.0",
            "cache.ttl": "^1.0.0",
        }),
        AppSpec("payment-service", {
            "db.pool.size": ">=1.0.0, <3.0.0",
            "shared.timeout-ms": "^1.0.0",
            "feature.checkout-v2": "^2.0.0",
            "payment.gateway": "^1.0.0",
        }),
        AppSpec("inventory-service", {
            "db.pool.size": "^1.0.0",
            "cache.ttl": "^1.0.0",
            "shared.timeout-ms": ">=1.0.0",
        }),
    ]
    for spec in apps:
        store.register_app(spec)

    # ---- 全局共享层（含依赖链：feature.checkout-v2 -> shared.timeout-ms -> cache.ttl）
    globals_ = [
        KeyRevision("db.pool.size", "1.0.0", "20", note="共享连接池默认大小"),
        KeyRevision("cache.ttl", "1.2.0", "300", note="缓存默认 TTL（秒）"),
        KeyRevision("shared.timeout-ms", "1.4.2", "5000",
                    depends_on=["cache.ttl"], note="共享超时，依赖缓存 TTL"),
        KeyRevision("feature.checkout-v2", "2.1.0", "on",
                    depends_on=["shared.timeout-ms", "db.pool.size"],
                    note="新结算开关，依赖超时与连接池"),
        KeyRevision("payment.gateway", "1.0.3", "https://gateway.example.com",
                    depends_on=["shared.timeout-ms"], note="支付网关地址"),
    ]
    for rev in globals_:
        store.put_global(rev)

    # ---- 应用覆盖层
    overrides = [
        # 订单服务把连接池调大，并钉在 1.5.0
        AppOverride("order-service", "db.pool.size", "1.5.0", "64",
                    note="大促期间订单服务独立扩池"),
        # 支付服务用更短的超时
        AppOverride("payment-service", "shared.timeout-ms", "1.4.0", "3000",
                    depends_on=["cache.ttl"], note="支付链路更激进的超时"),
        # 库存服务缓存更短
        AppOverride("inventory-service", "cache.ttl", "1.1.0", "120",
                    note="库存数据要求更强一致性"),
    ]
    for ov in overrides:
        store.put_override(ov)

    store.save()
    return store


if __name__ == "__main__":
    s = build_seed_store()
    print(f"已写入演示数据: {s.path}（{len(s.apps)} 个应用，{len(s.all_keys())} 个键）")
