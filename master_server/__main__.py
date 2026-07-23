"""`python -m master_server` 启动入口。"""

from __future__ import annotations

import importlib.util
import sys

from .app import create_app
from .config import MasterConfig


def main() -> None:
    config = MasterConfig.from_env()
    app = create_app(config)
    if not config.token and config.host not in {"127.0.0.1", "localhost", "::1"}:
        print("WARNING: 主控对外监听但未设置 MASTER_TOKEN。", file=sys.stderr)
    if (
        config.bootstrap_worker_enabled
        and config.bootstrap_worker_token == "dev-worker-token"
    ):
        print(
            "WARNING: 本机 Worker 使用默认 Token；请确保 Worker 与主控配置一致。",
            file=sys.stderr,
        )
    if importlib.util.find_spec("waitress") is not None:
        from waitress import serve

        serve(app, host=config.host, port=config.port, threads=8)
    else:
        print("WARNING: 未安装 waitress，使用 Flask 开发服务器。", file=sys.stderr)
        app.run(host=config.host, port=config.port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
