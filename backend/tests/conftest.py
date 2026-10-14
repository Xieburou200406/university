import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# M7-①：测试日志隔离——所有 opslog 输出重定向到临时目录，绝不污染生产 logs/
import tempfile as _tempfile
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _isolated_opslog(monkeypatch):
    import os as _os
    _root = _tempfile.mkdtemp(prefix="opslog_test_")
    monkeypatch.setenv("OPSLOG_DIR", _root)
    yield
