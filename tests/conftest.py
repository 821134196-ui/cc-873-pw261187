"""测试夹具：构造与 seed 一致但隔离的内存存储。"""
import pytest

from config_center.publisher import Change, Publisher
from config_center.seed import build_seed_store
from config_center.store import AppOverride, AppSpec, ConfigStore, KeyRevision


@pytest.fixture
def store(tmp_path):
    return build_seed_store(tmp_path / "test-config.json")


@pytest.fixture
def publisher(store):
    return Publisher(store)


@pytest.fixture
def empty_store(tmp_path):
    s = ConfigStore(tmp_path / "empty.json")
    s.register_app(AppSpec("app-a", {"x": "^1.0.0"}))
    s.register_app(AppSpec("app-b", {"y": ">=1.0.0, <2.0.0"}))
    return s
