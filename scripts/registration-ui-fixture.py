"""仅本机隔离的注册与审核浏览器验收服务，邮件只进入测试内存。"""

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
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import QueuePool  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402


def main() -> None:
    """使用隔离SQLite及虚构SMTP，在固定回环地址提供真实API操作链路。"""
    settings = Settings(
        _env_file=None,
        app_env="test",
        model_provider="mock",
        model_id="mock-v1",
        model_api_key="",
        embedding_provider="mock",
        auth_trusted_origins=["http://127.0.0.1:18010"],
        smtp_host="smtp.example.test",
        smtp_username="sender@example.test",
        smtp_password="test-mail-only",
        smtp_sender="sender@example.test",
        tool_http_allowed_hosts=[],
        tool_secrets={},
        amap_api_key="",
        log_level="WARNING",
    )

    async def healthy() -> dict[str, bool]:
        """明确模拟基础服务状态，不将其作为实际Docker或存储验收证据。"""
        return {"postgres": True, "redis": True, "storage": True, "milvus": True}

    engine = create_engine(
        "sqlite:///file:registration_ui?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
    )
    app = create_app(settings, checker=healthy, engine=engine, limiter=lambda *args: None)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            email="admin-ui@example.test",
            username="uiadmin",
            name="验收管理员",
            platform_admin=True,
            password_hash=hasher.hash("test-ui-only"),
        )
        db.add(user)
        db.flush()
        db.commit()
    outbox: dict[str, str] = {}

    def deliver(settings: Settings, email: str, code: str) -> None:
        """测试发送器仅保存内存验证码，不连接任何SMTP服务。"""
        outbox[email] = code

    app.state.send_verification_code = deliver

    @app.get("/test-only/code")
    def code(email: str) -> dict[str, str]:
        """仅隔离测试fixture读取验证码，此路由从不注册到业务应用。"""
        return {"code": outbox.get(email, "")}

    app.router.routes = [route for route in app.router.routes if getattr(route, "path", "") != "/"]
    app.mount("/", StaticFiles(directory=ROOT / "frontend/dist", html=True))
    uvicorn.run(app, host="127.0.0.1", port=18010, log_level="warning")


if __name__ == "__main__":
    main()
