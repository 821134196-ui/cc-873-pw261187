"""HTTP 服务：REST API + SSE 推送，纯标准库实现。"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .notify import NotificationHub
from .publisher import (
    Change, CycleDetectedError, IncompatibleVersionError, Publisher,
    UnknownDependencyError,
)
from .store import AppSpec, ConfigStore

STATIC_DIR = Path(__file__).parent / "static"


def make_handler(store: ConfigStore, hub: NotificationHub):
    publisher = Publisher(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ConfigCenter/1.0"

        def log_message(self, fmt, *args):  # 静默默认访问日志
            pass

        # ---------------------------------------------------------- 工具
        def _json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str, extra=None):
            payload = {"error": code, "message": message}
            if extra:
                payload.update(extra)
            self._json(payload, status)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))

        # ---------------------------------------------------------- 路由
        def do_GET(self):
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            try:
                if path in ("/", "/index.html"):
                    return self._serve_static("index.html", "text/html")
                if path.startswith("/static/"):
                    name = path.split("/static/", 1)[1]
                    ctype = {
                        "js": "application/javascript",
                        "css": "text/css",
                        "html": "text/html",
                        "svg": "image/svg+xml",
                    }.get(name.rsplit(".", 1)[-1], "application/octet-stream")
                    return self._serve_static(name, ctype)
                if path == "/api/state":
                    return self._state()
                if path == "/api/key-sources":
                    return self._key_sources(qs.get("app", [None])[0])
                if path == "/api/events":
                    return self._events()
                if path == "/api/subscribe":
                    app = qs.get("app", [None])[0]
                    if not app or app not in store.apps:
                        return self._error(400, "bad_app", "未知应用")
                    return self._sse(app)
                self._error(404, "not_found", f"无此路径: {path}")
            except Exception as exc:  # noqa: BLE001
                self._error(500, "internal", str(exc))

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/apps":
                    body = self._read_json()
                    spec = AppSpec(body["name"], body.get("requirements", {}))
                    store.register_app(spec)
                    store.save()
                    return self._json(spec.to_dict(), 201)
                if path == "/api/prepare":
                    body = self._read_json()
                    changes = [Change.from_dict(c) for c in body["changes"]]
                    return self._json(publisher.prepare(changes).to_dict())
                if path == "/api/release":
                    body = self._read_json()
                    changes = [Change.from_dict(c) for c in body["changes"]]
                    try:
                        event = publisher.commit(changes)
                    except CycleDetectedError as e:
                        return self._error(422, "cycle", str(e),
                                           {"cycle": e.cycle})
                    except IncompatibleVersionError as e:
                        return self._error(422, "incompatible", str(e),
                                           {"violations": e.violations})
                    except UnknownDependencyError as e:
                        return self._error(422, "missing_dependency", str(e))
                    delivered = hub.publish_refresh(event)
                    return self._json({"event": event, "refreshed_clients": delivered},
                                      201)
                if path == "/api/rollback":
                    body = self._read_json()
                    try:
                        event = publisher.rollback(body["release_ids"])
                    except CycleDetectedError as e:
                        return self._error(422, "cycle", str(e),
                                           {"cycle": e.cycle})
                    except IncompatibleVersionError as e:
                        return self._error(422, "incompatible", str(e),
                                           {"violations": e.violations})
                    except UnknownDependencyError as e:
                        return self._error(422, "missing_dependency", str(e))
                    delivered = hub.publish_refresh(event)
                    return self._json({"event": event, "refreshed_clients": delivered},
                                      201)
                self._error(404, "not_found", f"无此路径: {path}")
            except json.JSONDecodeError as e:
                self._error(400, "bad_json", f"请求体不是合法 JSON: {e}")
            except (KeyError, ValueError) as e:
                self._error(400, "bad_request", str(e))
            except Exception as exc:  # noqa: BLE001
                self._error(500, "internal", str(exc))

        # ---------------------------------------------------------- 视图数据
        def _state(self):
            data = store.snapshot()
            # 附加每个应用的生效视图与图
            views = {}
            for app in store.apps:
                keys = {}
                for key in sorted(
                        set(store.overrides.get(app, {})) |
                        set(store.apps[app].requirements) |
                        set(store.global_keys)):
                    try:
                        rev = store.effective_revision(app, key)
                        keys[key] = {
                            "version": rev.version, "value": rev.value,
                            "depends_on": rev.depends_on,
                            "source": store.effective_source(app, key),
                            "constraint": store.apps[app].requirements.get(key, ""),
                        }
                    except KeyError:
                        keys[key] = {"version": None, "source": "unresolvable"}
                views[app] = {
                    "requirements": store.apps[app].requirements,
                    "keys": keys,
                    "graph": publisher.effective_graph(app),
                    "online_clients": hub.subscriber_count(app),
                }
            data["views"] = views
            data["online_clients_total"] = hub.subscriber_count()
            self._json(data)

        def _key_sources(self, app):
            if not app:
                return self._error(400, "bad_app", "需要 app 参数")
            result = {}
            for key in sorted(store.all_keys()):
                try:
                    source = store.effective_source(app, key)
                    rev = store.effective_revision(app, key)
                    result[key] = {
                        "source": source,
                        "effective": rev.to_dict(),
                        "global_versions": sorted(
                            (r.to_dict() for r in
                             store.global_keys.get(key, {}).values()),
                            key=lambda r: r["version"]),
                        "override": store.overrides.get(app, {})
                            .get(key).to_dict()
                        if key in store.overrides.get(app, {}) else None,
                    }
                except KeyError:
                    result[key] = {"source": "unresolvable"}
            self._json(result)

        def _events(self):
            self._json(store.events)

        # ---------------------------------------------------------- SSE
        def _sse(self, app: str):
            client_id, q = hub.subscribe(app)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.wfile.write(f"event: hello\ndata: {json.dumps({'client_id': client_id, 'app': app})}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                    except Exception:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(
                        f"event: {msg.get('type', 'message')}\n"
                        f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                hub.unsubscribe(app, client_id)

        def _serve_static(self, name: str, ctype: str = "application/javascript"):
            file_path = STATIC_DIR / name
            if not file_path.is_file():
                return self._error(404, "not_found", name)
            body = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(store: ConfigStore, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    hub = NotificationHub()
    httpd = ThreadingHTTPServer((host, port), make_handler(store, hub))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd
