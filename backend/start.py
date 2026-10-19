"""统一启动 API 或 Linux Worker；不安装依赖、不迁移数据库、不创建管理员。"""

import argparse
import json
import os
import re
import sys

from app.core.config import Settings, load_settings
from pydantic import ValidationError
from pydantic_settings import SettingsError


def main() -> int:
    """配置错误输出脱敏提示并返回非零退出码，支持独立预检。"""
    parser = argparse.ArgumentParser(description="YugiTalk 后端启动入口")
    parser.add_argument(
        "--mode",
        choices=["api", "worker", "scheduler"],
        default="api",
        help="运行 API、Worker 或恢复调度器",
    )
    parser.add_argument("--env", choices=["development", "test", "production"], help="覆盖运行环境")
    parser.add_argument("--host", help="覆盖 API 监听地址")
    parser.add_argument("--port", type=int, help="覆盖 API 端口")
    parser.add_argument("--reload", action="store_true", help="开发 API 热重载，生产禁止")
    parser.add_argument("--check", action="store_true", help="只校验配置，不启动服务")
    args = parser.parse_args()
    try:
        settings = load_settings(args.env)
        overrides = {
            key: value
            for key, value in {"app_host": args.host, "app_port": args.port}.items()
            if value is not None
        }
        if args.reload:
            overrides["app_reload"] = True
        settings = type(settings).model_validate(settings.model_dump() | overrides)
    except (ValidationError, SettingsError, ValueError) as error:
        # Pydantic 的原始错误包含输入值，只输出字段位置和自定义说明。
        if isinstance(error, ValidationError):
            messages = [
                f"{'.'.join(map(str, item['loc'])) or '配置'}: {item['msg']}"
                for item in error.errors(include_input=False, include_url=False)
            ]
        elif isinstance(error, SettingsError):
            # 只提取已声明的字段名，不能输出含配置原值的异常或异常链。
            field_match = re.search(r'field "([a-zA-Z0-9_]+)"', str(error))
            field = field_match.group(1) if field_match else ""
            if field in Settings.model_fields and isinstance(error.__cause__, json.JSONDecodeError):
                messages = [f"{field.upper()}: JSON 格式无效，字符串必须使用英文双引号"]
                if field == "tool_http_allowed_hosts":
                    messages.append('正确示例：TOOL_HTTP_ALLOWED_HOSTS=["restapi.amap.com"]')
            else:
                messages = ["配置解析失败，请检查环境变量和 .env 的格式"]
        else:
            messages = ["配置解析失败"]
        print("配置无效：" + "；".join(messages), file=sys.stderr)
        return 2
    if args.check:
        print(
            json.dumps(
                {
                    "status": "valid",
                    "environment": settings.app_env,
                    "mode": args.mode,
                    "model_provider": settings.model_provider,
                },
                ensure_ascii=False,
            )
        )
        return 0
    os.environ["APP_ENV"] = settings.app_env
    if args.mode == "scheduler":
        from app.tasks.scheduler import serve

        serve(settings)
    elif args.mode == "worker":
        if sys.platform == "win32":
            print("Worker 需要 Linux 容器，请使用 Docker Compose 启动。", file=sys.stderr)
            return 2
        from app.tasks.celery_app import celery_app

        celery_app.worker_main(
            [
                "worker",
                "--loglevel",
                settings.log_level,
                "--concurrency=2",
                "--queues=" + settings.task_queue,
                "--hostname=yugitalk@%h",
            ]
        )
    else:
        import uvicorn

        uvicorn.run(
            "app.main:create_app",
            factory=True,
            host=settings.app_host,
            port=settings.app_port,
            reload=settings.app_reload,
            loop="app.core.event_loop:selector_loop_factory" if sys.platform == "win32" else "auto",
            timeout_graceful_shutdown=20,
            access_log=False,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
