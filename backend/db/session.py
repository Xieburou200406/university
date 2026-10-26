# -*- coding: utf-8 -*-
"""数据库会话工厂。默认库 = 仓库根 data/vol.db（正式版 schema，独立于 demo 的 vol_demo.db）。"""
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 运行根：打包（PyInstaller frozen）→ exe 目录（持久化），开发 → 仓库根
ROOT = (Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2])
DEFAULT_DB = ROOT / "data" / "vol.db"

# §17.2 快照只追加（制度变机制）：position_snapshot 禁 UPDATE —— 快照是 PnL 归因的
# 唯一输入，改历史 = 篡改账本。INSERT OR REPLACE（demo 重放幂等用）不受影响：
# SQLite 的 REPLACE 走 DELETE+INSERT，不触发 UPDATE 触发器。
_SNAP_APPEND_ONLY_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_snap_no_update
BEFORE UPDATE ON position_snapshot
BEGIN
    SELECT RAISE(ABORT, 'position_snapshot is append-only (§17.2 快照差分铁律)');
END
"""


def _ensure_snap_append_only(conn) -> None:
    """给 SQLite 连接装上只追加触发器（表存在才装——内存库 create_all 前无表）。"""
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='position_snapshot'")
        if cur.fetchone():
            cur.execute(_SNAP_APPEND_ONLY_SQL)
    finally:
        cur.close()


_after_create_registered = False


def _register_after_create_hook() -> None:
    """create_all 建表后补装触发器（新建库/内存库路径——connect 时表还不存在）。"""
    global _after_create_registered
    if _after_create_registered:
        return
    from sqlalchemy import event as _event
    from db.models import Base as _Base

    @_event.listens_for(_Base.metadata, "after_create")
    def _install(target, connection, **kw):
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql(_SNAP_APPEND_ONLY_SQL)
    _after_create_registered = True


_register_after_create_hook()


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
            _ensure_snap_append_only(dbapi_conn)
    return engine


def make_session(engine=None):
    engine = engine or make_engine()
    return sessionmaker(bind=engine, expire_on_commit=False), engine
