# -*- coding: utf-8 -*-
"""FastAPI 应用工厂。启动：uvicorn api.app:app --reload --port 8000（或仓库根 run_api.py）。
所有路由挂在 /api 前缀下；CORS 放开本地前端端口（Vue3 dev）。"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import deps
from api.routers import all_routers


def create_app() -> FastAPI:
    app = FastAPI(title="低频波动率交易决策辅助系统 API",
                  version="0.1.0 (M5-④)",
                  description="只生成建议，绝不自动下单（铁律 10）")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                       "http://localhost:8501"],   # Vue3 dev / Streamlit 面板
        allow_methods=["*"], allow_headers=["*"],
    )
    deps.init_engine()   # 经模块属性调用，测试注入才生效
    for r in all_routers():
        app.include_router(r, prefix="/api")
    return app


app = create_app()
