"""为技能浏览器验收提供真实 API 与内存数据库，始终使用离线模型。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.entities import Base  # noqa: E402
from app.identity.models import User  # noqa: E402
from app.identity.security import hasher  # noqa: E402
from app.main import create_app  # noqa: E402
from app.tasks.execution import Execution  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import QueuePool  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402


def main() -> None:
    """启动仅本机可访问的隔离验收服务，关闭后不留下用户或技能业务数据。"""
    settings = Settings(
        _env_file=None,
        tool_http_allowed_hosts=[],
        tool_secrets={},
        amap_api_key="",
        app_env="test",
        model_provider="mock",
        model_id="mock-v1",
        model_api_key="",
        model_base_url="",
        embedding_provider="mock",
        auth_trusted_origins=["http://127.0.0.1:18009"],
        log_level="WARNING",
    )
    engine = create_engine(
        "sqlite:///file:skills_ui?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
    )
    app = create_app(settings, engine=engine, limiter=lambda *args: None)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            email="skill-ui@example.test",
            name="技能验收",
            platform_admin=True,
            password_hash=hasher.hash("skill-ui-test-only"),
        )
        db.add(user)
        db.flush()
        db.commit()
    # 同步执行真实任务循环，无 Redis、Milvus 或外部模型依赖。
    app.state.dispatch_run = lambda key: Execution(engine, settings, key).execute()
    app.router.routes = [route for route in app.router.routes if getattr(route, "path", "") != "/"]
    app.mount("/", StaticFiles(directory=ROOT / "frontend/dist", html=True))
    uvicorn.run(app, host="127.0.0.1", port=18009, log_level="warning")


if __name__ == "__main__":
    main()
