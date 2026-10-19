"""验证开发与生产模板的工具配置传递，不输出私有凭证或合并后的环境内容。"""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(arguments: list[str], named_secret: bool) -> dict[str, object]:
    """用合成密钥检查 Compose JSON 插值，避免空默认值正确而真实值带多余括号。"""
    environment = dict(os.environ)
    environment["TOOL_SECRETS"] = '{"LOOKUP":"fixture-only"}' if named_secret else "{}"
    environment["TOOL_HTTP_ALLOWED_HOSTS"] = '["restapi.amap.com"]'
    environment["AMAP_API_KEY"] = "fixture-only"
    environment["TAVILY_API_KEY"] = "tavily-fixture-only"
    result = subprocess.run(
        ["docker", "compose", *arguments, "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Compose 模板校验失败，未打印含环境配置的原始输出")
    services = json.loads(result.stdout)["services"]
    for name in ("api", "worker", "scheduler", "migrate"):
        config = services[name]["environment"]
        assert json.loads(config["TOOL_SECRETS"]) == (
            {"LOOKUP": "fixture-only"} if named_secret else {}
        )
        assert json.loads(config["TOOL_HTTP_ALLOWED_HOSTS"]) == ["restapi.amap.com"]
        assert config["AMAP_API_KEY"] == "fixture-only"
        assert config["TAVILY_API_KEY"] == "tavily-fixture-only"
    return {
        "passed": True,
        "named_secret": named_secret,
        "services": ["api", "worker", "scheduler", "migrate"],
    }


def main() -> None:
    """核验两套部署环境和空/非空密钥映射，写入不含秘密的证据。"""
    cases = []
    for named in (False, True):
        cases.append({"environment": "development", **check([], named)})
        template = ROOT / ".cache/compose-production-validation.env"
        if not template.is_file():
            raise RuntimeError("缺少隔离生产模板，不能声明生产配置已验证")
        cases.append(
            {
                "environment": "production template",
                **check(
                    [
                        "--env-file",
                        str(template),
                        "-f",
                        "docker-compose.yml",
                        "-f",
                        "docker-compose.prod.yml",
                    ],
                    named,
                ),
            }
        )
    (ROOT / "docs/stages/artifacts/tools-compose-validation.json").write_text(
        json.dumps({"passed": True, "cases": cases}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("工具环境配置验证通过，未打印凭证。")


if __name__ == "__main__":
    main()
