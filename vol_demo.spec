# -*- mode: python ; coding: utf-8 -*-
# M8-③ 单机打包 spec（onedir）：Streamlit 面板 + 后端库全部捆绑，数据库/缓存/日志落 exe 旁。
# 构建：pyinstaller vol_demo.spec --noconfirm
# 产物：dist/VolDemo/VolDemo.exe（双击启动，自动开浏览器；数据写入 exe 旁 data/ 与 logs/）

import os

block_cipher = None
ROOT = os.path.abspath(".")

from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

datas = [
    ("app.py", "."),
    ("m2_run.py", "."),
    ("m3_run.py", "."),
    ("run_demo.py", "."),
    ("backend/vol", "backend/vol"),
    ("backend/db", "backend/db"),
    ("backend/api", "backend/api"),
    ("backend/config.yaml", "backend"),
]
binaries = []
# 缓存种子（可选）：有则离线首启即有历史面板
if os.path.isdir(os.path.join(ROOT, "data", "cache")):
    datas.append((os.path.join(ROOT, "data", "cache"), "data/cache"))

hiddenimports = [
    "streamlit", "plotly", "akshare", "pandas", "numpy",
    "sqlalchemy", "requests", "bs4", "lxml", "py_mini_racer",
    "vol.pricing.curve", "vol.pricing.bs", "vol.signals", "vol.calendar",
    "vol.contracts", "vol.opslog", "vol.position_risk",
    "db.session", "db.models",
]

# Streamlit 前端静态资源 + plotly.js 必须整包收集（须在 Analysis 之前算好）
for pkg in ("streamlit", "plotly"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
datas += collect_data_files("akshare")
# streamlit.version / plotly 等运行时用 importlib.metadata 读自身版本
# —— dist-info 元数据必须显式打包（否则 PackageNotFoundError）
for pkg in ("streamlit", "plotly", "akshare", "pandas", "numpy",
            "altair", "pillow", "pyarrow", "sqlalchemy"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass  # 未安装的包静默跳过

a = Analysis(
    ["launch.py"],
    pathex=[ROOT, os.path.join(ROOT, "backend")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "torch", "tensorflow",
              "IPython", "jupyter", "pytest"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="VolDemo",
    console=False,          # 双击无黑窗；日志看 exe 旁 logs/
    icon=None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False,
    name="VolDemo",
)
