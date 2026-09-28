"""`python -m worker_agent` 的服务启动入口。"""

from __future__ import annotations

import importlib.util
import signal
import sys

from .app import create_app
from .config import WorkerConfig
from .watchdog import ServiceWatchdog
from capture_temp_cleanup import configure_temp_directory


def main() -> None:
    """优先使用 Waitress；缺少时回退到仅供调试的 Flask 服务。"""
    config = WorkerConfig.from_env()
    config.prepare()
    configure_temp_directory(config.capture_temp_dir)
    app = create_app(config)
    manager = app.extensions["task_manager"]

    def request_shutdown(_signum, _frame) -> None:
        # 把 SIGTERM 转换为可清理的退出路径；TaskManager 会终止当前子进程，
        # 并把任务重新置为 QUEUED，供下次启动从 URL 检查点继续。
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_shutdown)

    if config.token == "dev-worker-token":
        print(
            "WARNING: 当前使用默认 WORKER_TOKEN，仅适合本机调试。",
            file=sys.stderr,
        )

    watchdog = None
    try:
        watchdog = ServiceWatchdog.from_environment(config.host, config.port)
        if watchdog is not None:
            watchdog.start()
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
    except KeyboardInterrupt:
        pass
    finally:
        if watchdog is not None:
            watchdog.stop()
        manager.shutdown(timeout=30.0)


if __name__ == "__main__":
    main()
