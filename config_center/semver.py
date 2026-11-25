"""语义化版本与版本约束（SemVer 子集，纯标准库实现）。

支持的约束操作符：>=, <=, >, <, ==, !=, ^, ~
多个约束用逗号连接表示 AND，例如 ">=1.2.0, <2.0.0"。
^1.2.3 等价于 >=1.2.3, <2.0.0；~1.2.3 等价于 >=1.2.3, <1.3.0。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: str = ""

    @classmethod
    def parse(cls, text: str) -> "SemVer":
        text = text.strip().lstrip("vV")
        m = _VERSION_RE.match(text)
        if not m:
            raise ValueError(f"非法版本号: {text!r}")
        return cls(int(m[1]), int(m[2]), int(m[3]), m[4] or "")

    def _sort_key(self):
        # 无预发布标识的正式版本排序最高；有预发布则按点分段（数值优先于字符串）
        if not self.prerelease:
            return (self.major, self.minor, self.patch, 1, ())
        parts = tuple(
            (0, int(p)) if p.isdigit() else (1, p)
            for p in self.prerelease.split(".")
        )
        return (self.major, self.minor, self.patch, 0, parts)

    def __lt__(self, other: "SemVer") -> bool:
        return self._sort_key() < other._sort_key()

    def __le__(self, other: "SemVer") -> bool:
        return self._sort_key() <= other._sort_key()

    def __gt__(self, other: "SemVer") -> bool:
        return self._sort_key() > other._sort_key()

    def __ge__(self, other: "SemVer") -> bool:
        return self._sort_key() >= other._sort_key()

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{self.prerelease}" if self.prerelease else base


_OP_RE = re.compile(r"\s*(\^|~|>=|<=|==|!=|>|<)\s*v?([0-9][0-9A-Za-z.-]*)")


@dataclass(frozen=True)
class Constraint:
    op: str
    version: SemVer

    def satisfies(self, v: SemVer) -> bool:
        op, target = self.op, self.version
        if op == ">=":
            return v >= target
        if op == "<=":
            return v <= target
        if op == ">":
            return v > target
        if op == "<":
            return v < target
        if op == "==":
            return v == target
        if op == "!=":
            return v != target
        if op == "^":
            return v >= target and v.major == target.major
        if op == "~":
            return v >= target and v.major == target.major and v.minor == target.minor
        raise ValueError(f"未知操作符: {op}")


def parse_constraint(spec: str) -> list[Constraint]:
    """把 ">=1.2.0, <2.0.0" 解析为约束列表；空串表示无约束。"""
    spec = (spec or "").strip()
    if not spec or spec == "*":
        return []
    constraints: list[Constraint] = []
    pos = 0
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = _OP_RE.fullmatch(part)
        if not m:
            raise ValueError(f"非法版本约束: {part!r}")
        constraints.append(Constraint(m[1], SemVer.parse(m[2])))
        pos += len(part)
    return constraints


def compatible(spec: str, version: str) -> bool:
    """便捷判断：version 是否满足 spec。"""
    v = SemVer.parse(version)
    return all(c.satisfies(v) for c in parse_constraint(spec))
