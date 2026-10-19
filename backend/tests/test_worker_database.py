"""验证Worker连接复用、fork隔离与退出释放，不连接真实数据库。"""

from unittest.mock import Mock

import pytest
from app.core.config import Settings
from app.tasks import database as module
from sqlalchemy import Engine


def test_pool_reused_and_released_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """多次任务只建立一次池，退出关闭资源，再次使用重新创建。"""
    settings = Settings(app_env="test")
    first, second = Mock(spec=Engine), Mock(spec=Engine)
    factory = Mock(side_effect=[first, second])
    monkeypatch.setattr(module, "create_database", factory)
    monkeypatch.setattr(module, "load_settings", lambda: settings)
    resource = module.ProcessDatabase()
    assert resource.engine is None
    assert resource.get() == (first, settings)
    assert resource.get() == (first, settings)
    factory.assert_called_once_with(settings)
    first.dispose.assert_not_called()
    resource.close()
    resource.close()
    first.dispose.assert_called_once_with(close=True)
    assert resource.get() == (second, settings)


@pytest.mark.parametrize("action", ["get", "close"])
def test_fork_never_closes_or_reuses_parent_connections(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模拟子进程身份变化；继承池只能以close=False替换，不能共用连接。"""
    settings = Settings(app_env="test")
    parent, child = Mock(spec=Engine), Mock(spec=Engine)
    monkeypatch.setattr(module, "create_database", Mock(side_effect=[parent, child]))
    monkeypatch.setattr(module, "load_settings", lambda: settings)
    pid = [100]
    monkeypatch.setattr(module.os, "getpid", lambda: pid[0])
    resource = module.ProcessDatabase()
    resource.get()
    pid[0] = 101
    if action == "get":
        assert resource.get()[0] is child
        resource.close()
        child.dispose.assert_called_once_with(close=True)
    else:
        resource.close()
        assert resource.engine is None
    parent.dispose.assert_called_once_with(close=False)


def test_failed_initialization_can_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置或连接池建立失败不得缓存半初始化状态，后续任务仍可重试。"""
    settings = Settings(app_env="test")
    engine = Mock(spec=Engine)
    monkeypatch.setattr(module, "load_settings", lambda: settings)
    monkeypatch.setattr(module, "create_database", Mock(side_effect=[RuntimeError(), engine]))
    resource = module.ProcessDatabase()
    with pytest.raises(RuntimeError):
        resource.get()
    assert resource.engine is None
    assert resource.get() == (engine, settings)
