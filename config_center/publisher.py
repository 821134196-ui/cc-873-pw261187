"""发布引擎：影响分析、环检测、版本相容性、原子发布与批量回滚。"""
from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .semver import SemVer, parse_constraint
from .store import AppOverride, AppSpec, ConfigStore, KeyRevision

# 快照中用于标记「发布前该键不存在」，回滚时据此删除
MISSING = "__missing__"


class CycleDetectedError(Exception):
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(f"检测到循环引用: {' -> '.join(cycle)}")


class IncompatibleVersionError(Exception):
    def __init__(self, violations: list[dict]):
        self.violations = violations
        super().__init__(f"版本不相容: {violations}")


class UnknownDependencyError(Exception):
    pass


@dataclass
class Change:
    """一次改动。scope='global' 改共享层；scope='override' 改应用覆盖层。"""
    scope: str               # 'global' | 'override'
    key: str
    version: str
    value: str
    depends_on: list[str] = field(default_factory=list)
    app: Optional[str] = None
    note: str = ""

    def __post_init__(self):
        if self.scope not in ("global", "override"):
            raise ValueError(f"非法 scope: {self.scope}")
        if self.scope == "override" and not self.app:
            raise ValueError("override 改动必须指定 app")

    @property
    def target_app(self) -> Optional[str]:
        return self.app if self.scope == "override" else None

    def to_dict(self) -> dict:
        return {
            "scope": self.scope, "key": self.key, "version": self.version,
            "value": self.value, "depends_on": list(self.depends_on),
            "app": self.app, "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Change":
        return cls(**d)


@dataclass
class ImpactPlan:
    """发布前的影响分析结果（dry-run）。"""
    ok: bool
    changes: list[dict]
    # 直接命中的 app -> 命中的键及来源
    direct_hits: dict[str, list[dict]]
    # 沿反向依赖传播后的完整影响：app -> [key, ...]
    affected_apps: dict[str, list[str]]
    cycles: list[list[str]]
    violations: list[dict]
    missing_deps: list[dict]
    # 发布前每个受影响 (app,key) 的生效版本，供回滚使用
    before_snapshot: dict[str, dict]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "changes": self.changes,
            "direct_hits": self.direct_hits,
            "affected_apps": self.affected_apps,
            "cycles": self.cycles,
            "violations": self.violations,
            "missing_deps": self.missing_deps,
            "before_snapshot": self.before_snapshot,
        }


class Publisher:
    def __init__(self, store: ConfigStore):
        self.store = store

    # ------------------------------------------------------------ 图工具
    def effective_graph(self, app: str) -> dict[str, list[str]]:
        """某应用视角下的完整生效依赖图：key -> 依赖的 key 列表。"""
        graph: dict[str, list[str]] = {}
        stack = list(self.store.overrides.get(app, {}).keys())
        # 应用 requirements 中声明消费的键也要入图
        stack.extend(self.store.apps.get(app, AppSpec(app)).requirements)
        seen: set[str] = set()
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            try:
                rev = self.store.effective_revision(app, key)
            except KeyError:
                graph.setdefault(key, [])
                continue
            graph[key] = list(rev.depends_on)
            stack.extend(rev.depends_on)
        return graph

    @staticmethod
    def find_cycle(graph: dict[str, list[str]]) -> Optional[list[str]]:
        """DFS 三色标记，返回一条闭环路径（含回到起点的重复节点），无环返回 None。"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {k: WHITE for k in graph}
        parent: dict[str, Optional[str]] = {k: None for k in graph}

        def dfs(node: str) -> Optional[list[str]]:
            color[node] = GRAY
            for nxt in graph.get(node, []):
                color.setdefault(nxt, WHITE)
                if color[nxt] == WHITE:
                    parent[nxt] = node
                    found = dfs(nxt)
                    if found:
                        return found
                elif color[nxt] == GRAY:
                    # 回溯出环：nxt ... node -> nxt
                    cycle = [nxt]
                    cur = node
                    while cur is not None and cur != nxt:
                        cycle.append(cur)
                        cur = parent.get(cur)
                    cycle.append(nxt)
                    cycle.reverse()
                    return cycle
            color[node] = BLACK
            return None

        for k in list(graph):
            if color[k] == WHITE:
                found = dfs(k)
                if found:
                    return found
        return None

    def reverse_closure(self, app: str, seeds: list[str]) -> list[str]:
        """从 seeds 出发沿「反向依赖边」求闭包：谁依赖了这些键。"""
        graph = self.effective_graph(app)
        reverse: dict[str, set[str]] = {}
        for k, deps in graph.items():
            for d in deps:
                reverse.setdefault(d, set()).add(k)
        result: set[str] = set()
        stack = list(seeds)
        while stack:
            cur = stack.pop()
            if cur in result:
                continue
            result.add(cur)
            stack.extend(reverse.get(cur, set()))
        return sorted(result)

    # ------------------------------------------------------------ 影响分析
    def _simulate(self, changes: list[Change]):
        """在内存副本上预演改动，返回 (模拟后的覆盖表, 模拟后的全局表, 缺失依赖, 目标版本)。

        不直接改 store：环检测与相容性必须基于「假设改动已生效」的未来状态。
        target_versions 记录每个全局键本次改动指定的版本号——新增 revision 后
        「最高版本」未必等于目标版本（例如补一个旧分支版本），因此不能靠 max 推断。
        """
        sim_overrides = {
            app: {k: copy.copy(o) for k, o in m.items()}
            for app, m in self.store.overrides.items()
        }
        sim_global: dict[str, dict[str, KeyRevision]] = {
            k: dict(v) for k, v in self.store.global_keys.items()
        }
        target_versions: dict[str, str] = {}
        missing: list[dict] = []
        for ch in changes:
            if ch.scope == "global":
                rev = KeyRevision(ch.key, ch.version, ch.value,
                                  list(ch.depends_on), time.time(), ch.note)
                sim_global.setdefault(ch.key, {})[ch.version] = rev
                target_versions[ch.key] = ch.version
            else:
                ov = AppOverride(ch.app, ch.key, ch.version, ch.value,
                                 list(ch.depends_on), time.time(), ch.note)
                sim_overrides.setdefault(ch.app, {})[ch.key] = ov
            for dep in ch.depends_on:
                exists = dep in sim_global or dep in sim_overrides.get(
                    ch.app or "", {})
                if not exists:
                    missing.append({"key": ch.key, "missing": dep,
                                    "app": ch.app})
        return sim_overrides, sim_global, missing, target_versions

    def _graph_in_simulation(self, app: str, sim_overrides, sim_global,
                             target_versions=None) -> dict[str, list[str]]:
        target_versions = target_versions or {}
        graph: dict[str, list[str]] = {}
        seeds = list(sim_overrides.get(app, {}).keys())
        seeds.extend(self.store.apps.get(app, AppSpec(app)).requirements)
        seen: set[str] = set()
        stack = list(seeds)
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            if key in sim_overrides.get(app, {}):
                deps = list(sim_overrides[app][key].depends_on)
            elif key in sim_global:
                if key in target_versions:
                    deps = list(sim_global[key][target_versions[key]].depends_on)
                else:
                    latest = max(sim_global[key].values(),
                                 key=lambda r: SemVer.parse(r.version))
                    deps = list(latest.depends_on)
            else:
                deps = []
            graph[key] = deps
            stack.extend(deps)
        return graph

    def prepare(self, changes: list[Change]) -> ImpactPlan:
        """计算一次改动的完整影响范围，不写入任何数据。"""
        if not changes:
            raise ValueError("改动列表为空")
        sim_overrides, sim_global, missing, target_versions = self._simulate(changes)

        # 1) 直接命中：哪些应用对该键的生效值会发生变化
        direct_hits: dict[str, list[dict]] = {}
        changed_keys: dict[str, set[str]] = {}  # app -> seeds
        for ch in changes:
            targets = [ch.app] if ch.scope == "override" else list(self.store.apps)
            for app in targets:
                # 全局改动对已有覆盖的应用不可见
                if ch.scope == "global" and ch.key in self.store.overrides.get(app, {}):
                    continue
                try:
                    before = self.store.effective_revision(app, ch.key)
                    before_ver = before.version
                except KeyError:
                    before_ver = None
                if before_ver != ch.version:
                    direct_hits.setdefault(app, []).append({
                        "key": ch.key,
                        "scope": ch.scope,
                        "old_version": before_ver,
                        "new_version": ch.version,
                        "source": self.store.effective_source(app, ch.key)
                        if before_ver else "new",
                    })
                    changed_keys.setdefault(app, set()).add(ch.key)

        # 2) 模拟态环检测（每个应用的生效图都要无环）
        cycles: list[list[str]] = []
        for app in self.store.apps:
            graph = self._graph_in_simulation(
                app, sim_overrides, sim_global, target_versions)
            cycle = self.find_cycle(graph)
            if cycle:
                cycles.append(cycle)

        # 3) 反向依赖闭包 + 版本相容性
        affected: dict[str, list[str]] = {}
        violations: list[dict] = []
        before_snapshot: dict[str, dict] = {}
        for app, seeds in changed_keys.items():
            graph = self._graph_in_simulation(
                app, sim_overrides, sim_global, target_versions)
            reverse: dict[str, set[str]] = {}
            for k, deps in graph.items():
                for d in deps:
                    reverse.setdefault(d, set()).add(k)
            closure: set[str] = set()
            stack = list(seeds)
            while stack:
                cur = stack.pop()
                if cur in closure:
                    continue
                closure.add(cur)
                stack.extend(reverse.get(cur, set()))
            affected[app] = sorted(closure)

            spec = self.store.apps.get(app)
            for key in sorted(closure):
                # 模拟态下该键的生效版本：覆盖 > 本次目标版本 > 全局最高版本
                if key in sim_overrides.get(app, {}):
                    new_ver = sim_overrides[app][key].version
                elif key in sim_global:
                    new_ver = target_versions.get(
                        key,
                        max(sim_global[key].values(),
                            key=lambda r: SemVer.parse(r.version)).version)
                else:
                    continue
                if spec and key in spec.requirements:
                    cons = parse_constraint(spec.requirements[key])
                    v = SemVer.parse(new_ver)
                    if not all(c.satisfies(v) for c in cons):
                        violations.append({
                            "app": app, "key": key,
                            "version": new_ver,
                            "constraint": spec.requirements[key],
                        })

                # 记录发布前快照（回滚依据），并保留来源层级
                app_overrides = self.store.overrides.get(app, {})
                if key in app_overrides:
                    before_snapshot.setdefault(app, {})[key] = {
                        "source": "override",
                        "revision": app_overrides[key].to_dict(),
                    }
                else:
                    try:
                        old = self.store.global_revision(key)
                        before_snapshot.setdefault(app, {})[key] = {
                            "source": "global",
                            "revision": old.to_dict(),
                        }
                    except KeyError:
                        before_snapshot.setdefault(app, {})[key] = {
                            "source": MISSING, "revision": None,
                        }

        ok = not cycles and not violations and not missing
        return ImpactPlan(
            ok=ok,
            changes=[c.to_dict() for c in changes],
            direct_hits=direct_hits,
            affected_apps=affected,
            cycles=cycles,
            violations=violations,
            missing_deps=missing,
            before_snapshot=before_snapshot,
        )

    # ------------------------------------------------------------ 发布
    def commit(self, changes: list[Change]) -> dict:
        """原子发布：校验通过才写入；否则抛出对应异常阻止发布。"""
        plan = self.prepare(changes)
        if plan.cycles:
            raise CycleDetectedError(plan.cycles[0])
        if plan.missing_deps:
            raise UnknownDependencyError(str(plan.missing_deps))
        if plan.violations:
            raise IncompatibleVersionError(plan.violations)

        event = {
            "type": "release",
            "id": uuid.uuid4().hex[:12],
            "at": time.time(),
            "changes": [c.to_dict() for c in changes],
            "affected_apps": plan.affected_apps,
            "before_snapshot": plan.before_snapshot,
            "status": "committed",
        }
        for ch in changes:
            if ch.scope == "global":
                self.store.put_global(KeyRevision(
                    ch.key, ch.version, ch.value, list(ch.depends_on),
                    time.time(), ch.note))
            else:
                self.store.put_override(AppOverride(
                    ch.app, ch.key, ch.version, ch.value,
                    list(ch.depends_on), time.time(), ch.note))
        self.store.append_event(event)
        self.store.save()
        return event

    # ------------------------------------------------------------ 实时态校验
    def _validate_live(self, store: "ConfigStore") -> tuple[list, list, list]:
        """对一个真实 store 做环检测、缺失依赖、版本相容性校验。"""
        cycles, violations, missing = [], [], []
        known = store.all_keys()
        for app in store.apps:
            graph = self.effective_graph(app) if store is self.store \
                else Publisher(store).effective_graph(app)
            cycle = self.find_cycle(graph)
            if cycle:
                cycles.append(cycle)
            for key, deps in graph.items():
                for dep in deps:
                    if dep not in known:
                        missing.append({"app": app, "key": key, "missing": dep})
            spec = store.apps[app]
            for key, spec_text in spec.requirements.items():
                try:
                    ver = store.effective_revision(app, key).version
                    v = SemVer.parse(ver)
                    if not all(c.satisfies(v) for c in parse_constraint(spec_text)):
                        violations.append({"app": app, "key": key,
                                           "version": ver, "constraint": spec_text})
                except KeyError:
                    missing.append({"app": app, "key": key, "missing": key})
        return cycles, violations, missing

    # ------------------------------------------------------------ 批量回滚
    def rollback(self, release_ids: list[str]) -> dict:
        """批量回滚：把若干次发布整体撤销。

        - 按发布时间正序取每个 (app,key) 的**最早**快照（即这批发布之前的状态）；
        - 在内存临时存储上预演反向操作并做环检测/相容性校验，不安全则阻止；
        - 仅刷新这些发布原先影响到的客户端（affected_apps 的并集，去重）。
        """
        if not release_ids:
            raise ValueError("回滚事件列表为空")
        id_set = set(release_ids)
        releases = [e for e in self.store.events
                    if e["id"] in id_set and e["type"] == "release"]
        if len(releases) != len(id_set):
            found = {e["id"] for e in releases}
            raise KeyError(f"发布事件不存在: {sorted(id_set - found)}")

        # 原始受影响应用（去重并集）——回滚只通知这些应用的客户端
        original_affected: dict[str, set[str]] = {}
        for ev in releases:
            for app, keys in ev.get("affected_apps", {}).items():
                original_affected.setdefault(app, set()).update(keys)

        # 这批发布在全局层引入过哪些 (key, version)，用于 MISSING 场景的清理
        introduced_global: set[tuple[str, str]] = set()
        for ev in releases:
            for ch in ev.get("changes", []):
                if ch["scope"] == "global":
                    introduced_global.add((ch["key"], ch["version"]))

        # 正序遍历，先到的快照即「这批发布之前」的状态
        targets: dict[tuple[str, str], dict] = {}
        for ev in sorted(releases, key=lambda e: e["at"]):
            for app, key_map in ev.get("before_snapshot", {}).items():
                for key, snap in key_map.items():
                    targets.setdefault((app, key), snap)

        # 快照要求恢复为全局值的版本——这些即使落在 introduced 集合里也必须保留
        restored_global: set[tuple[str, str]] = {
            (snap["revision"]["key"], snap["revision"]["version"])
            for snap in targets.values() if snap["source"] == "global"
        }

        def apply_inverse(target_store: "ConfigStore") -> list[dict]:
            """把反向操作应用到指定存储（预演用临时存储与真实存储共用此序列）。"""
            actions: list[dict] = []
            for (app, key), snap in sorted(targets.items()):
                source = snap["source"]
                if source == MISSING:
                    target_store.delete_override(app, key)
                    actions.append({"app": app, "key": key, "action": "delete"})
                elif source == "override":
                    r = KeyRevision.from_dict({
                        k: snap["revision"][k] for k in
                        ("key", "version", "value", "depends_on",
                         "updated_at", "note")})
                    target_store.put_override(AppOverride(
                        app, r.key, r.version, r.value,
                        list(r.depends_on), r.updated_at, r.note))
                    actions.append({"app": app, "key": key,
                                    "action": "restore-override",
                                    "version": r.version})
                else:  # global：移除覆盖，恢复穿透
                    target_store.delete_override(app, key)
                    r = KeyRevision.from_dict({
                        k: snap["revision"][k] for k in
                        ("key", "version", "value", "depends_on",
                         "updated_at", "note")})
                    target_store.put_global(r)
                    actions.append({"app": app, "key": key,
                                    "action": "restore-global",
                                    "version": r.version})

            # 清理这批发布引入的全局 revision，使「最高版本」真正回到发布前；
            # 快照要求保留的版本不删。
            for gkey, gver in sorted(introduced_global - restored_global):
                target_store.delete_global_revision(gkey, gver)
                actions.append({"key": gkey, "version": gver,
                                "action": "delete-global"})
            return actions

        # 在临时存储上预演全部反向操作并做安全校验
        tmp = ConfigStore.from_snapshot(self.store.snapshot())
        apply_inverse(tmp)
        cycles, violations, missing = self._validate_live(tmp)
        if cycles:
            raise CycleDetectedError(cycles[0])
        if violations:
            raise IncompatibleVersionError(violations)
        if missing:
            raise UnknownDependencyError(str(missing))

        # 校验通过，对真实 store 落地同样的反向操作
        inverse_changes = apply_inverse(self.store)

        event = {
            "type": "rollback",
            "id": uuid.uuid4().hex[:12],
            "at": time.time(),
            "rolled_back_release_ids": sorted(id_set),
            "inverse_changes": inverse_changes,
            # 关键：只刷新原先受影响的客户端
            "affected_apps": {app: sorted(keys)
                              for app, keys in original_affected.items()},
            "status": "committed",
        }
        self.store.append_event(event)
        self.store.save()
        return event
