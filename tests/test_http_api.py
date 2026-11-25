"""HTTP 端到端：真实起 ThreadingHTTPServer，验证 REST 与 SSE 精准推送。"""
import json
import threading
import time
import urllib.request
import urllib.error

import pytest

from config_center.seed import build_seed_store
from config_center.server import serve


@pytest.fixture
def httpd(tmp_path):
    store = build_seed_store(tmp_path / "http.json")
    srv = serve(store, "127.0.0.1", 0)
    host, port = srv.server_address
    yield f"http://{host}:{port}", store
    srv.shutdown()


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_state_and_static_page(httpd):
    base, _ = httpd
    status, data = call(base, "GET", "/api/state")
    assert status == 200
    assert set(data["apps"]) == {"order-service", "payment-service", "inventory-service"}
    assert data["views"]["order-service"]["keys"]["db.pool.size"]["version"] == "1.5.0"

    with urllib.request.urlopen(base + "/", timeout=5) as resp:
        assert "text/html" in resp.headers["Content-Type"]
        assert "配置中心".encode() in resp.read()


def test_prepare_then_release_and_sse_refresh(httpd):
    base, store = httpd

    # 先挂一个 SSE 客户端（order-service），在线程里读首条事件
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", base.rsplit(":", 1)[1])
    conn.request("GET", "/api/subscribe?app=order-service")
    resp = conn.getresponse()
    assert resp.status == 200

    def read_event():
        buf = b""
        while b"\n\n" not in buf:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk
        return buf.decode()

    assert "hello" in read_event()

    # dry-run
    status, plan = call(base, "POST", "/api/prepare",
                        {"changes": [{"scope": "global", "key": "cache.ttl",
                                      "version": "1.3.0", "value": "360",
                                      "depends_on": []}]})
    assert status == 200 and plan["ok"]

    # 发布
    status, result = call(base, "POST", "/api/release",
                          {"changes": [{"scope": "global", "key": "cache.ttl",
                                        "version": "1.3.0", "value": "360",
                                        "depends_on": []}]})
    assert status == 201
    assert any(c.startswith("order-service/") for c in result["refreshed_clients"])

    # SSE 应收到 refresh
    deadline = time.time() + 3
    event = ""
    while time.time() < deadline and "refresh" not in event:
        event = read_event()
    assert "refresh" in event and "rollback" not in event
    conn.close()


def test_release_rejected_for_cycle(httpd):
    base, _ = httpd
    status, err = call(base, "POST", "/api/release", {
        "changes": [{"scope": "global", "key": "cache.ttl", "version": "1.3.0",
                     "value": "360", "depends_on": ["shared.timeout-ms"]}]})
    assert status == 422
    assert err["error"] == "cycle"
    assert err["cycle"]


def test_release_rejected_for_incompatibility(httpd):
    base, _ = httpd
    status, err = call(base, "POST", "/api/release", {
        "changes": [{"scope": "global", "key": "db.pool.size", "version": "2.0.0",
                     "value": "32", "depends_on": []}]})
    assert status == 422
    assert err["error"] == "incompatible"
    assert err["violations"]


def test_batch_rollback_endpoint(httpd):
    base, _ = httpd
    _, r1 = call(base, "POST", "/api/release", {
        "changes": [{"scope": "global", "key": "cache.ttl", "version": "1.3.0",
                     "value": "360", "depends_on": []}]})
    _, r2 = call(base, "POST", "/api/release", {
        "changes": [{"scope": "global", "key": "cache.ttl", "version": "1.4.0",
                     "value": "420", "depends_on": []}]})
    ids = [r1["event"]["id"], r2["event"]["id"]]
    status, result = call(base, "POST", "/api/rollback", {"release_ids": ids})
    assert status == 201
    assert result["event"]["rolled_back_release_ids"] == sorted(ids)
    _, state = call(base, "GET", "/api/state")
    assert max(state["global_keys"]["cache.ttl"]) == "1.2.0"


def test_key_sources_endpoint(httpd):
    base, _ = httpd
    status, data = call(base, "GET", "/api/key-sources?app=order-service")
    assert status == 200
    info = data["db.pool.size"]
    assert info["source"] == "override:order-service"
    assert info["override"]["version"] == "1.5.0"
    assert info["global_versions"][0]["version"] == "1.0.0"
