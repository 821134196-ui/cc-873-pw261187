"""领域模型与配置存储。

三层取值模型
------------
1. 全局共享层：键的默认版本/值/依赖，所有应用可见。
2. 应用覆盖层：应用可对同一个键钉住自己的版本（不同的值/依赖/版本号）。
3. 应用依赖约束：应用声明自己消费哪些键，以及可接受的版本范围。

每个键的具体版本（revision）自带依赖列表 ``depends_on``，因此依赖图是
**按版本** 的；覆盖层替换依赖时，等于在该应用视角下替换了出边。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .semver import SemVer, parse_constraint


@dataclass
class KeyRevision:
    """键的一个不可变版本。"""
    key: str
    version: str
    value: str
    depends_on: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "KeyRevision":
        return cls(**d)


@dataclass
class AppOverride:
    """应用对某个键的覆盖（钉住的具体版本）。"""
    app: str
    key: str
    version: str
    value: str
    depends_on: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AppOverride":
        return cls(**d)


@dataclass
class AppSpec:
    """应用声明：消费的键与版本约束。"""
    name: str
    # key -> 约束表达式，如 ">=1.2.0, <2.0.0"；空串表示接受任意版本
    requirements: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AppSpec":
        return cls(**d)


class ConfigStore:
    """线程安全的配置存储，可整体快照到 JSON 文件。"""

    def __init__(self, path: Optional[str | Path] = None):
        self._lock = threading.RLock()
        self.path = Path(path) if path else None
        # key -> {version: KeyRevision}，全局共享层的全部历史版本
        self.global_keys: dict[str, dict[str, KeyRevision]] = {}
        # app -> {key: AppOverride}，应用覆盖层（只保留当前钉住版本）
        self.overrides: dict[str, dict[str, AppOverride]] = {}
        self.apps: dict[str, AppSpec] = {}
        # 发布与回滚事件（append-only）
        self.events: list[dict] = []
        if self.path and self.path.exists():
            self._load()

    # ------------------------------------------------------------------ 基础查询
    def global_revision(self, key: str, version: Optional[str] = None) -> KeyRevision:
        """取全局层某键的指定版本；version 为 None 时取最高版本。"""
        with self._lock:
            versions = self.global_keys.get(key)
            if not versions:
                raise KeyError(f"全局键不存在: {key}")
            if version is None:
                return max(versions.values(), key=lambda r: SemVer.parse(r.version))
            if version not in versions:
                raise KeyError(f"全局键 {key} 无版本 {version}")
            return versions[version]

    def effective_revision(self, app: str, key: str) -> KeyRevision:
        """应用视角下某键的生效版本：覆盖优先，否则穿透到全局层。

        返回的对象统一为 KeyRevision（覆盖会被投影成等价 revision），
        这样依赖图构建只需要面对一种结构。
        """
        with self._lock:
            app_map = self.overrides.get(app, {})
            if key in app_map:
                o = app_map[key]
                return KeyRevision(o.key, o.version, o.value,
                                   list(o.depends_on), o.updated_at, o.note)
            return self.global_revision(key)

    def effective_source(self, app: str, key: str) -> str:
        """返回键的来源描述：'override:<app>' 或 'global'，未知键抛 KeyError。"""
        with self._lock:
            if key in self.overrides.get(app, {}):
                return f"override:{app}"
            if key in self.global_keys:
                return "global"
            raise KeyError(f"应用 {app} 无法解析键 {key}")

    def effective_deps(self, app: str, key: str) -> list[str]:
        return list(self.effective_revision(app, key).depends_on)

    def all_keys(self) -> set[str]:
        with self._lock:
            keys = set(self.global_keys)
            for app_map in self.overrides.values():
                keys.update(app_map)
            return keys

    # ------------------------------------------------------------------ 变更写入
    def put_global(self, rev: KeyRevision) -> None:
        """新增/更新全局键的一个版本（同版本号即覆盖该 revision 内容）。"""
        with self._lock:
            self.global_keys.setdefault(rev.key, {})[rev.version] = rev

    def put_override(self, ov: AppOverride) -> None:
        with self._lock:
            if ov.app not in self.apps:
                raise KeyError(f"应用不存在: {ov.app}")
            self.overrides.setdefault(ov.app, {})[ov.key] = ov

    def register_app(self, spec: AppSpec) -> None:
        with self._lock:
            self.apps.setdefault(spec.name, spec)
            self.overrides.setdefault(spec.name, {})
            # 校验约束表达式可解析
            for spec_text in spec.requirements.values():
                parse_constraint(spec_text)

    def delete_global_revision(self, key: str, version: str) -> None:
        with self._lock:
            if key in self.global_keys:
                self.global_keys[key].pop(version, None)
                if not self.global_keys[key]:
                    del self.global_keys[key]

    def delete_override(self, app: str, key: str) -> None:
        with self._lock:
            self.overrides.get(app, {}).pop(key, None)

    def append_event(self, event: dict) -> None:
        with self._lock:
            event.setdefault("id", uuid.uuid4().hex[:12])
            event.setdefault("at", time.time())
            self.events.append(event)
            return event

    # ------------------------------------------------------------------ 持久化
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "global_keys": {
                    k: {v: r.to_dict() for v, r in vers.items()}
                    for k, vers in self.global_keys.items()
                },
                "overrides": {
                    app: {k: o.to_dict() for k, o in m.items()}
                    for app, m in self.overrides.items()
                },
                "apps": {name: s.to_dict() for name, s in self.apps.items()},
                "events": list(self.events),
            }

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._load_dict(data)

    @classmethod
    def from_snapshot(cls, data: dict) -> "ConfigStore":
        """从 snapshot() 产出的字典构造一个不落盘的临时存储。"""
        s = cls(path=None)
        s._load_dict(data)
        return s

    def _load_dict(self, data: dict) -> None:
        self.global_keys = {
            k: {v: KeyRevision.from_dict(r) for v, r in vers.items()}
            for k, vers in data.get("global_keys", {}).items()
        }
        self.overrides = {
            app: {k: AppOverride.from_dict(o) for k, o in m.items()}
            for app, m in data.get("overrides", {}).items()
        }
        self.apps = {name: AppSpec.from_dict(s)
                     for name, s in data.get("apps", {}).items()}
        self.events = data.get("events", [])
