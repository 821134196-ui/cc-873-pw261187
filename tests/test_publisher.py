"""发布引擎核心行为：三层取值、影响传播、环检测、相容性、回滚精准刷新。"""
from config_center.notify import NotificationHub
from config_center.publisher import (
    Change, CycleDetectedError, IncompatibleVersionError, Publisher,
    UnknownDependencyError,
)
from config_center.store import AppOverride, AppSpec, KeyRevision


# ------------------------------------------------------------ 三层取值
def test_override_shadows_global(store):
    pub = Publisher(store)
    # order-service 覆盖了 db.pool.size 为 1.5.0
    rev = store.effective_revision("order-service", "db.pool.size")
    assert rev.version == "1.5.0"
    assert store.effective_source("order-service", "db.pool.size") == "override:order-service"
    # 没有覆盖的应用穿透到全局层 1.0.0
    rev2 = store.effective_revision("payment-service", "db.pool.size")
    assert rev2.version == "1.0.0"
    assert store.effective_source("payment-service", "db.pool.size") == "global"


def test_global_change_skips_apps_with_override(publisher, store):
    """全局层改动对已覆盖该键的应用不可见。"""
    plan = publisher.prepare([
        Change("global", "db.pool.size", "1.1.0", "24")
    ])
    # order-service 有覆盖，不应被命中
    assert "order-service" not in plan.direct_hits
    assert "payment-service" in plan.direct_hits
    assert "inventory-service" in plan.direct_hits


# ------------------------------------------------------------ 影响范围传播
def test_impact_propagates_along_reverse_dependencies(publisher):
    """改 cache.ttl：依赖它的 shared.timeout-ms 与 feature.checkout-v2 都应进闭包。"""
    plan = publisher.prepare([
        Change("global", "cache.ttl", "1.3.0", "360")
    ])
    assert plan.ok
    affected = plan.affected_apps["order-service"]
    # 种子依赖链：checkout-v2 -> timeout -> cache.ttl
    assert "cache.ttl" in affected
    assert "shared.timeout-ms" in affected
    assert "feature.checkout-v2" in affected


def test_direct_hit_records_old_and_new_version(publisher):
    plan = publisher.prepare([
        Change("global", "cache.ttl", "1.3.0", "360")
    ])
    hit = next(h for h in plan.direct_hits["order-service"] if h["key"] == "cache.ttl")
    assert hit["old_version"] == "1.2.0"
    assert hit["new_version"] == "1.3.0"


# ------------------------------------------------------------ 循环引用
def test_cycle_is_detected_and_blocks_commit(publisher):
    # cache.ttl 改为反向依赖 timeout，制造 cache.ttl <-> timeout 环
    plan = publisher.prepare([
        Change("global", "cache.ttl", "1.3.0", "360",
               depends_on=["shared.timeout-ms"])
    ])
    assert not plan.ok
    assert plan.cycles, "应报告闭环路径"
    flat = set(plan.cycles[0])
    assert {"cache.ttl", "shared.timeout-ms"} <= flat
    with __import__("pytest").raises(CycleDetectedError):
        publisher.commit([
            Change("global", "cache.ttl", "1.3.0", "360",
                   depends_on=["shared.timeout-ms"])
        ])
    # 被阻止后原数据不变
    assert store_global_version(publisher, "cache.ttl") == "1.2.0"


def test_self_dependency_is_a_cycle(publisher):
    plan = publisher.prepare([
        Change("global", "cache.ttl", "1.3.0", "360", depends_on=["cache.ttl"])
    ])
    assert plan.cycles


def test_override_level_cycle_detected(publisher, store):
    """覆盖层引入的环（只在某应用视角下出现）也要被发现。"""
    plan = publisher.prepare([
        Change("override", "cache.ttl", "1.3.0", "90",
               depends_on=["shared.timeout-ms"], app="payment-service")
    ])
    assert not plan.ok
    assert plan.cycles


# ------------------------------------------------------------ 版本不相容
def test_incompatible_constraint_blocks_commit(publisher):
    # order-service 覆盖了 db.pool.size，全局 2.0.0 对它不可见；
    # inventory-service 无覆盖且要求 ^1.0.0，应违规；
    # payment-service 允许 <3.0.0，不违规。
    plan = publisher.prepare([
        Change("global", "db.pool.size", "2.0.0", "32")
    ])
    assert not plan.ok
    assert any(v["key"] == "db.pool.size" and v["app"] == "inventory-service"
               for v in plan.violations)
    assert not any(v["app"] == "payment-service" for v in plan.violations)
    # 有覆盖的 order-service 根本不进受影响集合
    assert "order-service" not in plan.affected_apps
    with __import__("pytest").raises(IncompatibleVersionError):
        publisher.commit([Change("global", "db.pool.size", "2.0.0", "32")])


def test_incompatible_override_version_blocks(publisher, store):
    """覆盖层新版本违反应用自身约束时同样阻止。"""
    plan = publisher.prepare([
        Change("override", "db.pool.size", "2.0.0", "96",
               app="order-service")
    ])
    assert not plan.ok
    assert any(v["app"] == "order-service" and v["key"] == "db.pool.size"
               for v in plan.violations)


def test_compatible_release_passes(publisher):
    # 1.x 的小版本满足所有 ^1.0.0 / <3.0.0 约束
    plan = publisher.prepare([
        Change("global", "db.pool.size", "1.9.0", "48")
    ])
    assert plan.ok, plan.violations


def test_tilde_constraint_blocks_minor_bump(publisher):
    # order-service 对 shared.timeout-ms 是 ~1.4.0；1.5.0 不相容
    plan = publisher.prepare([
        Change("global", "shared.timeout-ms", "1.5.0", "6000",
               depends_on=["cache.ttl"])
    ])
    assert any(v["app"] == "order-service" and v["key"] == "shared.timeout-ms"
               for v in plan.violations)


# ------------------------------------------------------------ 缺失依赖
def test_missing_dependency_blocks(publisher):
    plan = publisher.prepare([
        Change("global", "cache.ttl", "1.3.0", "360", depends_on=["ghost.key"])
    ])
    assert not plan.ok
    assert plan.missing_deps


# ------------------------------------------------------------ 发布 + 回滚
def test_commit_writes_and_appends_event(publisher, store):
    event = publisher.commit([
        Change("global", "cache.ttl", "1.3.0", "360")
    ])
    assert event["type"] == "release"
    assert store.global_revision("cache.ttl").version == "1.3.0"
    assert len(store.events) == 1


def test_rollback_restores_previous_state(publisher, store):
    publisher.commit([Change("global", "cache.ttl", "1.3.0", "360")])
    release_id = store.events[0]["id"]
    assert store.global_revision("cache.ttl").version == "1.3.0"

    rb = publisher.rollback([release_id])
    assert rb["type"] == "rollback"
    assert store.global_revision("cache.ttl").version == "1.2.0"
    # 回滚事件只携带原发布的受影响应用
    assert set(rb["affected_apps"]) == set(store.events[0]["affected_apps"])


def test_batch_rollback_only_refreshes_originally_affected_clients(publisher, store):
    """需求核心：批量回滚后只刷新原先受影响的客户端。"""
    # 发布 A：改 cache.ttl（影响全部三个应用）
    ev_a = publisher.commit([Change("global", "cache.ttl", "1.3.0", "360")])
    # 发布 B：改 payment.gateway（只影响 payment-service）
    ev_b = publisher.commit([
        Change("global", "payment.gateway", "1.1.0", "https://gw2.example.com",
               depends_on=["shared.timeout-ms"])
    ])

    hub = NotificationHub()
    _, q_order = hub.subscribe("order-service")
    _, q_pay = hub.subscribe("payment-service")
    _, q_inv = hub.subscribe("inventory-service")

    rb = publisher.rollback([ev_a["id"], ev_b["id"]])
    delivered = hub.publish_refresh(rb)

    # 通知目标 = 两次发布 affected_apps 的并集（去重），且不含任何多余应用
    expected_apps = set(ev_a["affected_apps"]) | set(ev_b["affected_apps"])
    assert set(rb["affected_apps"]) == expected_apps
    notified_apps = {d.split("/")[0] for d in delivered}
    assert notified_apps == expected_apps

    # 每个在线客户端恰好收到一条刷新
    for q in (q_order, q_pay, q_inv):
        msgs = drain(q)
        assert len(msgs) == 1
        assert msgs[0]["type"] == "refresh"
        assert msgs[0]["event_type"] == "rollback"

    # 数据已恢复到两次发布之前
    assert store.global_revision("cache.ttl").version == "1.2.0"
    assert store.global_revision("payment.gateway").version == "1.0.3"


def test_unaffected_client_gets_no_refresh(publisher, store):
    """单应用覆盖改动发布再回滚，其他应用客户端不应被打扰。"""
    hub = NotificationHub()
    _, q_order = hub.subscribe("order-service")
    _, q_pay = hub.subscribe("payment-service")

    ev = publisher.commit([
        Change("override", "db.pool.size", "1.6.0", "96", app="order-service")
    ])
    # 发布通知只到 order-service
    delivered = hub.publish_refresh(ev)
    assert {d.split("/")[0] for d in delivered} == {"order-service"}
    assert drain(q_pay) == []

    rb = publisher.rollback([ev["id"]])
    delivered_rb = hub.publish_refresh(rb)
    assert {d.split("/")[0] for d in delivered_rb} == {"order-service"}
    assert drain(q_pay) == []
    assert store.effective_revision("order-service", "db.pool.size").version == "1.5.0"


def test_rollback_of_newly_added_key_deletes_it(publisher, store):
    """回滚一个「发布前不存在」的新键，应把它清理掉。"""
    ev = publisher.commit([
        Change("global", "brand.new.key", "1.0.0", "hello")
    ])
    assert "brand.new.key" in store.global_keys
    publisher.rollback([ev["id"]])
    assert "brand.new.key" not in store.global_keys


def test_rollback_blocked_when_it_would_create_cycle(publisher, store):
    """回滚本身也必须安全：若恢复旧状态会成环，则阻止。"""
    # 用独立的探针键链，避免种子覆盖数据干扰
    store.register_app(AppSpec("rollback-probe", {
        "probe.a": "^1.0.0", "probe.b": "^1.0.0"}))
    # 初始：b -> a，无环（一次发布同时引入两个键）
    base = publisher.commit([
        Change("global", "probe.a", "1.0.0", "a", depends_on=[]),
        Change("global", "probe.b", "1.0.0", "b", depends_on=["probe.a"]),
    ])
    # 发布 1：a 改为依赖 b，此时 b->a 仍在 -> 不能这么发。
    # 因此先发布 2：解除 b->a（无环）
    ev2 = publisher.commit([
        Change("global", "probe.b", "1.1.0", "b", depends_on=[])
    ])
    # 再发布 3：a -> b，当前 b 无出边，无环
    ev3 = publisher.commit([
        Change("global", "probe.a", "1.1.0", "a", depends_on=["probe.b"])
    ])
    # 回滚发布 2 会恢复 b -> a，而现状是 a -> b => 成环，必须阻止
    import pytest
    with pytest.raises(CycleDetectedError):
        publisher.rollback([ev2["id"]])
    # 数据保持回滚前状态
    assert store.global_revision("probe.b").version == "1.1.0"
    # 回滚发布 3 则是安全的：恢复 a 无依赖（b 1.1.0 保留）
    publisher.rollback([ev3["id"]])
    assert store.global_revision("probe.a").version == "1.0.0"
    assert store.global_revision("probe.b").version == "1.1.0"


def test_rollback_unknown_event_raises(publisher):
    import pytest
    with pytest.raises(KeyError):
        publisher.rollback(["nope"])


# ------------------------------------------------------------ 批量改动
def test_batch_changes_validated_together(publisher):
    """一次提交多个改动时，任一不相容则整体阻止（原子性）。"""
    import pytest
    with pytest.raises(IncompatibleVersionError):
        publisher.commit([
            Change("global", "cache.ttl", "1.3.0", "360"),
            Change("global", "db.pool.size", "2.0.0", "32"),  # 违反 ^1.0.0
        ])
    # 第一项也不应落库
    assert store_global_version(publisher, "cache.ttl") == "1.2.0"


# ------------------------------------------------------------ 辅助
def store_global_version(publisher, key):
    return publisher.store.global_revision(key).version


def drain(q):
    import queue
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except __import__("queue").Empty:
            return out
