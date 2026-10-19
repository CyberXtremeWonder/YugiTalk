"""真实队列集成测试；显式启用，避免缺少外部服务时报告虚假通过。"""

import os
from uuid import uuid4

import psycopg
import pytest
from probes.worker import advance, database_url, queue

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_PROBE_INTEGRATION") != "1",
        reason="start stage-zero Compose and set RUN_PROBE_INTEGRATION=1",
    ),
]


@pytest.mark.parametrize("decision,status", [("approve", "approved"), ("reject", "rejected")])
def test_real_worker_resumes_postgres_checkpoint(decision: str, status: str) -> None:
    """验证真实工作进程能够恢复关系数据库中的检查点。"""
    thread = f"probe-{uuid4()}"
    initial = advance.delay(thread).get(timeout=30)
    assert initial["waiting"] is True
    assert "status" not in initial["values"]
    resumed = advance.delay(thread, decision).get(timeout=30)
    assert resumed["waiting"] is False
    assert resumed["values"]["status"] == status


def test_relational_database_and_worker_ping() -> None:
    """验证真实数据库和工作队列的往返访问。"""
    with psycopg.connect(database_url()) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
    assert queue.control.ping(timeout=5)
