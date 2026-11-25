"""启动入口：python -m config_center [--reseed] [--port 8000]"""
from __future__ import annotations

import argparse
from pathlib import Path

from .seed import DEFAULT_DB, build_seed_store
from .server import serve
from .store import ConfigStore


def main() -> None:
    parser = argparse.ArgumentParser(description="配置中心发布器")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reseed", action="store_true",
                        help="删除现有数据并重新写入演示数据")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()

    path = Path(args.db)
    if args.reseed:
        if path.exists():
            path.unlink()
        store = build_seed_store(path)
    elif not path.exists():
        store = build_seed_store(path)
    else:
        store = ConfigStore(path)

    httpd = serve(store, args.host, args.port)
    print(f"配置中心已启动: http://{args.host}:{args.port}/  （数据文件 {store.path}）")
    try:
        import signal
        signal.pause()
    except (KeyboardInterrupt, AttributeError):
        httpd.shutdown()


if __name__ == "__main__":
    main()
