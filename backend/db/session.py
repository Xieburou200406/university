# -*- coding: utf-8 -*-
"""数据库会话工厂。默认库 = 仓库根 data/vol.db（正式版 schema，独立于 demo 的 vol_demo.db）。"""
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "vol.db"


def make_engine(url: str | None = None):
    """创建引擎。SQLite 打开 WAL + 外键开关；测试可传 sqlite:///:memory: 或 tmp 路径。
    内存库用 StaticPool：全进程共享同一连接（否则每线程各见一个空库）。"""
    if url == "sqlite:///:memory:":
        from sqlalchemy.pool import StaticPool
        engine = create_engine(url, connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
    else:
        engine = create_engine(url or f"sqlite:///{DEFAULT_DB}",
                               connect_args={"check_same_thread": False})
    if engine.url.drivername.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    return engine


def make_session(engine=None):
    engine = engine or make_engine()
    return sessionmaker(bind=engine, expire_on_commit=False), engine
