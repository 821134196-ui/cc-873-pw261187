"""客户端通知中枢：基于内存队列 + SSE 的按应用订阅推送。

发布/回滚事件携带 affected_apps；中枢只把刷新通知投递给这些应用的在线客户端，
因此批量回滚时未受影响的客户端不会收到任何推送。
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import defaultdict
from typing import Optional


class NotificationHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # app -> {client_id: Queue}
        self._subscribers: dict[str, dict[str, queue.Queue]] = defaultdict(dict)

    def subscribe(self, app: str) -> tuple[str, queue.Queue]:
        client_id = uuid.uuid4().hex[:10]
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers[app][client_id] = q
        return client_id, q

    def unsubscribe(self, app: str, client_id: str) -> None:
        with self._lock:
            self._subscribers.get(app, {}).pop(client_id, None)

    def subscriber_count(self, app: Optional[str] = None) -> int:
        with self._lock:
            if app:
                return len(self._subscribers.get(app, {}))
            return sum(len(m) for m in self._subscribers.values())

    def publish_refresh(self, event: dict) -> list[str]:
        """按事件的 affected_apps 精准投递给在线客户端，返回收到推送的客户端 id。"""
        payload = {
            "type": "refresh",
            "event_type": event["type"],
            "event_id": event["id"],
            "at": event["at"],
            "affected_apps": event.get("affected_apps", {}),
            "changes": event.get("changes") or event.get("inverse_changes", []),
        }
        delivered: list[str] = []
        with self._lock:
            for app in event.get("affected_apps", {}):
                for client_id, q in list(self._subscribers.get(app, {}).items()):
                    try:
                        q.put_nowait(payload)
                        delivered.append(f"{app}/{client_id}")
                    except queue.Full:
                        # 慢客户端丢弃最旧消息后重试
                        try:
                            q.get_nowait()
                            q.put_nowait(payload)
                            delivered.append(f"{app}/{client_id}")
                        except (queue.Empty, queue.Full):
                            pass
        return delivered

    def heartbeat(self) -> None:
        """SSE 保活：给所有在线连接发送注释行。"""
        with self._lock:
            for app_map in self._subscribers.values():
                for q in app_map.values():
                    try:
                        q.put_nowait({"type": "heartbeat", "at": time.time()})
                    except queue.Full:
                        pass
