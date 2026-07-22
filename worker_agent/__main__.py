"""`python -m worker_agent` 的服务启动入口。"""

from __future__ import annotations

import importlib.util
import sys

from .app import create_app
from .config import WorkerConfig


def main() -> None:
    """优先使用 Waitress；缺少时回退到仅供调试的 Flask 服务。"""
    config = WorkerConfig.from_env()
    app = create_app(config)

    if config.token == "dev-worker-token":
        print(
            "WARNING: 当前使用默认 WORKER_TOKEN，仅适合本机调试。",
            file=sys.stderr,
        )

    if importlib.util.find_spec("waitress") is not None:
        from waitress import serve

        serve(app, host=config.host, port=config.port, threads=4)
    else:
        print(
            "WARNING: 未安装 waitress，回退到 Flask 开发服务器。",
            file=sys.stderr,
        )
        app.run(
            host=config.host,
            port=config.port,
            debug=False,
            threaded=True,
            use_reloader=False,
        )


if __name__ == "__main__":
    main()
