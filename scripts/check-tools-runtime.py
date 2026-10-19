"""只读验证已部署开发容器的工具目录、配置和页面资源，不创建任务或调用模型。"""

import json
import subprocess
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PROBE = """
import json
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from app.core.config import Settings
from app.identity.models import User
from app.tools.registry import TOOL_SCHEMAS
from app.tools.service import descriptor, readiness
settings = Settings(_env_file=None)
engine = create_engine(settings.database_url.get_secret_value())
with Session(engine) as db:
    member = db.scalar(select(User).where(User.active.is_(True)).limit(1))
    assert member is not None
    items = [descriptor(db, member.id, name) for name in TOOL_SCHEMAS]
    schema = db.scalar(text('SELECT version_num FROM alembic_version'))
    assert schema == '0015_user_ownership'
    print(json.dumps({'schema': schema, 'tools': [
        {'name': row['name'], 'title': row['title'], 'enabled': row['enabled'],
         'ready': not readiness(row['config'], settings) if row['kind'] == 'http' else True}
        for row in items],
        'amap_key_configured': bool(settings.amap_api_key.get_secret_value()),
        'paid_model_requests': 0, 'external_tool_requests': 0}))
engine.dispose()
"""


def main() -> None:
    """核对运行镜像实际声明和健康状态，保存不含凭证或身份的证据。"""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "python", "-c", PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("容器工具只读探测失败，未输出私有环境")
    record = json.loads(result.stdout)
    # 本机验收直连回环地址，防止系统代理转发本地请求导致虚假的部署失败。
    health = httpx.get("http://127.0.0.1:18000/health/ready", timeout=10, trust_env=False)
    assert health.status_code == 200
    home = httpx.get("http://127.0.0.1:8080/", timeout=10, trust_env=False)
    assert home.status_code == 200
    record.update({"api_ready": True, "frontend_http_ok": True, "business_mutations": 0})
    (ROOT / "docs/stages/artifacts/tools-runtime-validation.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("已部署工具目录、迁移版本、API 与前端检查通过，无外部查询。")


if __name__ == "__main__":
    main()
