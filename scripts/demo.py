#!/usr/bin/env python3
"""端到端演示（不依赖 HTTP）：影响分析 -> 阻止发布 -> 正常发布 -> 批量回滚精准刷新。

运行：python3 scripts/demo.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_center.notify import NotificationHub
from config_center.publisher import (
    Change, CycleDetectedError, IncompatibleVersionError, Publisher,
)
from config_center.seed import build_seed_store


def line(title: str) -> None:
    print(f"\n{'─' * 64}\n{title}\n{'─' * 64}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = build_seed_store(Path(tmp) / "demo.json")
        pub = Publisher(store)
        hub = NotificationHub()

        line("① 三层取值：order-service 覆盖了 db.pool.size，payment 穿透全局")
        for app in ("order-service", "payment-service"):
            rev = store.effective_revision(app, "db.pool.size")
            print(f"  {app:18s} db.pool.size = {rev.version:6s} "
                  f"来源={store.effective_source(app, 'db.pool.size')}")

        line("② 影响分析：改 cache.ttl 1.3.0，影响沿反向依赖传播")
        plan = pub.prepare([Change("global", "cache.ttl", "1.3.0", "360")])
        for app, keys in plan.affected_apps.items():
            print(f"  {app:18s} 受影响键: {', '.join(keys)}")

        line("③ 循环引用阻止发布：cache.ttl 反向依赖 shared.timeout-ms")
        try:
            pub.commit([Change("global", "cache.ttl", "1.3.0", "360",
                               depends_on=["shared.timeout-ms"])])
        except CycleDetectedError as e:
            print(f"  ⛔ 已阻止：{e}")

        line("④ 版本不相容阻止发布：cache.ttl 2.0.0 违反 ^1.0.0 约束")
        try:
            pub.commit([Change("global", "cache.ttl", "2.0.0", "360")])
        except IncompatibleVersionError as e:
            for v in e.violations:
                print(f"  ⛔ {v['app']} 要求 {v['key']} {v['constraint']}，"
                      f"目标版本 {v['version']}")

        line("⑤ 正常发布两次，并观察 SSE 精准投递")
        clients = {}
        for app in store.apps:
            cid, q = hub.subscribe(app)
            clients[app] = (cid, q)

        ev1 = pub.commit([Change("global", "cache.ttl", "1.3.0", "360")])
        print("  发布 A 刷新:", hub.publish_refresh(ev1))
        ev2 = pub.commit([Change(
            "override", "db.pool.size", "1.6.0", "96", app="order-service")])
        print("  发布 B 刷新:", hub.publish_refresh(ev2),
              "（仅 order-service，其他应用客户端无感知）")

        line("⑥ 批量回滚：只刷新原先受影响的客户端")
        rb = pub.rollback([ev1["id"], ev2["id"]])
        delivered = hub.publish_refresh(rb)
        print("  回滚事件刷新:", delivered)
        print("  回滚后 cache.ttl 全局最高版本:",
              store.global_revision("cache.ttl").version)
        print("  回滚后 order 的 db.pool.size:",
              store.effective_revision("order-service", "db.pool.size").version)

        line("⑦ 各客户端实际收到的刷新条数（无多余推送）")
        for app, (_, q) in clients.items():
            n = 0
            while not q.empty():
                q.get_nowait()
                n += 1
            print(f"  {app:18s} 收到 {n} 条刷新通知")


if __name__ == "__main__":
    main()
