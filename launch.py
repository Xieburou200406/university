# -*- coding: utf-8 -*-
"""单机打包入口（M8-③）：PyInstaller 打包后的启动器。

用法：
    开发模式  python launch.py            （直接跑仓库根 app.py）
    打包模式  VolDemo.exe [streamlit 参数] （运行 _MEIPASS 内捆绑的 app.py）

职责：
1. frozen 时把 _MEIPASS 与捆绑目录加入 sys.path（app.py 依赖 run_demo/m2_run/m3_run 同层导入）；
2. 以 headless 启动 Streamlit，并自动打开浏览器；
3. 数据/库/日志持久化在 exe 旁（m2_run.ROOT / db.session.ROOT / opslog 均已 frozen 感知）。
"""
import os
import sys


def _resource_base() -> str:
    """捆绑资源根：frozen → _MEIPASS（onedir 解包目录），开发 → 本文件所在目录。"""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    base = _resource_base()
    if base not in sys.path:
        sys.path.insert(0, base)
    app_path = os.path.join(base, "app.py")
    if not os.path.exists(app_path):
        raise SystemExit(f"找不到捆绑的 app.py：{app_path}")

    from streamlit.web import cli as stcli

    port = os.environ.get("VOLDEMO_PORT", "8501")
    os.environ.setdefault("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "false")  # 打包环境必须
    sys.argv = [
        "streamlit", "run", app_path,
        "--server.port", port,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--server.fileWatcherType", "none",   # 打包版无热重载，省资源
    ] + sys.argv[1:]

    if not os.environ.get("VOLDEMO_NO_BROWSER"):
        import threading
        import webbrowser
        threading.Timer(4.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
