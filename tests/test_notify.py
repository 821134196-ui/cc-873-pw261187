import queue

from config_center.notify import NotificationHub


def test_subscribe_and_targeted_refresh():
    hub = NotificationHub()
    id_a, q_a = hub.subscribe("app-a")
    id_b, q_b = hub.subscribe("app-b")
    assert hub.subscriber_count() == 2

    delivered = hub.publish_refresh({
        "id": "ev1", "type": "release", "at": 0,
        "affected_apps": {"app-a": ["x"]},
        "changes": [],
    })
    assert delivered == [f"app-a/{id_a}"]
    assert q_a.get_nowait()["event_id"] == "ev1"
    assert q_b.qsize() == 0
    hub.unsubscribe("app-a", id_a)
    assert hub.subscriber_count() == 1


def test_slow_client_does_not_block_others():
    hub = NotificationHub()
    _, q_full = hub.subscribe("app-a")
    for _ in range(100):
        q_full.put_nowait({"type": "x"})
    _, q_ok = hub.subscribe("app-a")
    # 满队列客户端触发丢弃逻辑，调用不抛异常，另一个客户端正常收到
    hub.publish_refresh({"id": "ev", "type": "release", "at": 0,
                         "affected_apps": {"app-a": ["k"]}, "changes": []})
    assert q_ok.get_nowait()["event_id"] == "ev"
