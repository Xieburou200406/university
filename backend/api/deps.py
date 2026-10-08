# -*- coding: utf-8 -*-
"""依赖注入：DB 会话 + 配置。每个请求一个会话，请求结束自动关闭。"""
from collections.abc import Generator
from pathlib import Path

from sqlalchemy.orm import Session

from db.session import make_engine, make_session
from vol.config import Settings

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
_SessionFactory = None


def init_engine():
    """进程内惰性初始化引擎 + 会话工厂（壳层兜底建表；正规迁移走 Alembic）。"""
    global _SessionFactory
    if _SessionFactory is None:
        engine = make_engine()
        from db.models import Base
        Base.metadata.create_all(engine)
        _SessionFactory, _ = make_session(engine)
    return _SessionFactory


def get_db() -> Generator[Session, None, None]:
    sf = init_engine()
    s = sf()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_settings() -> Settings:
    return Settings.load(str(_CONFIG_PATH))
