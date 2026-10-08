# -*- coding: utf-8 -*-
"""启动 API（M5-④）：python run_api.py  →  http://127.0.0.1:8000/docs 看交互文档。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("api.app:app", host="127.0.0.1", port=8000, reload=False)
