"""验证生产冷备恢复后的关系数据、原文、向量与业务接口，不修改开发服务。"""

import json
import runpy
import shutil
import ssl
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def probe(arguments: list[str], runner: object) -> dict[str, object]:
    """只读采集业务指纹，原文和密钥不进入验收报告。"""
    code = (ROOT / "scripts/probe-persistent-data.py").read_text(encoding="utf-8")
    return json.loads(runner([*arguments, "exec", "-T", "api", "python", "-"], code))


def main() -> None:
    """向新目录和新项目恢复，验证后保留源环境，关闭恢复演练实例。"""
    check = runpy.run_path(str(ROOT / "scripts/check-stage6-production.py"))
    data = runpy.run_path(str(ROOT / "scripts/production-data.py"))
    runner = check["run"]
    source_env = ROOT / ".cache/stage6-production.env"
    original = check["compose"]()
    target_report = ROOT / "docs/stages/artifacts/stage6-restore.json"
    if target_report.exists():
        shutil.copyfile(
            target_report,
            target_report.with_name(f"stage6-restore-{time.time_ns()}.json"),
        )
    before = probe(original, runner)
    started = time.monotonic()
    # 演练每次使用独立目标，拒绝复用或覆盖任何已有目录。
    relative = "data/stage6-restore-" + str(time.time_ns())
    snapshot = data["backup"](
        source_env, "yugitalk-stage6", ROOT / "docker-compose.stage6-test.yml"
    )
    restored = data["restore"](snapshot, relative)
    values = {
        key: str(value)
        for key, value in dotenv_values(source_env, interpolate=False).items()
        if value is not None
    }
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    frozen = manifest.get("runtime_images", {})
    # 本轮快照冻结停机前镜像，恢复期间不会误用被重建的可变标签。
    if frozen:
        values["BACKEND_IMAGE"] = frozen["backend"]
        values["FRONTEND_IMAGE"] = frozen["frontend"]
    shutil.copytree(
        ROOT / values["PROD_DATA_ROOT"] / "fixture-tls", restored / "fixture-tls"
    )
    # 此目录是协议替身测试证书，不属于正式业务备份。
    initializer = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    initializer["configure_permissions"](
        restored, frozen.get("backend", "yugitalk-backend:0.2.0")
    )
    values["PROD_DATA_ROOT"] = relative
    target_env = ROOT / ".cache/stage6-restore.env"
    target_env.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    restored_compose = check["compose"](target_env, "yugitalk-stage6-restored")
    report = {
        "status": "running",
        "snapshot": str(snapshot.relative_to(ROOT)),
        "target": relative,
        "runtime_images": frozen,
        "configuration_restored": False,
    }
    try:
        runner([*original, "down"])
        runner([*restored_compose, "up", "-d", "--wait", "--wait-timeout", "300"])
        after = probe(restored_compose, runner)
        report.update({"before": before, "after": after})
        assert before == after, "恢复前后业务指纹不一致"
        functional = json.loads(
            (ROOT / "docs/stages/artifacts/stage6-production.json").read_text(
                encoding="utf-8"
            )
        )["functional"]
        context = ssl.create_default_context(cafile=str(restored / "tls/fullchain.pem"))
        with httpx.Client(
            base_url=values["PUBLIC_ORIGIN"],
            verify=context,
            trust_env=False,
            timeout=240,
        ) as client:
            check["wait_ready"](client)
            identity = (
                client.post(
                    "/api/v1/auth/login",
                    headers={"Origin": values["PUBLIC_ORIGIN"]},
                    json={
                        "email": functional.get("email", "stage6@example.test"),
                        "password": values["E2E_PASSWORD"],
                    },
                )
                .raise_for_status()
                .json()
            )
            results = (
                client.post(
                    f"/api/v1/knowledge-bases/{functional['base']}/search",
                    headers={
                        "Origin": values["PUBLIC_ORIGIN"],
                        "X-CSRF-Token": identity["csrf_token"],
                    },
                    json={"query": "验收口令是什么？"},
                )
                .raise_for_status()
                .json()
            )
            assert "紫色星河" in json.dumps(results, ensure_ascii=False)
        report.update(
            {
                "status": "passed",
                "business_fingerprint_equal": True,
                "restored_rag": True,
                "fingerprint": after,
                "seconds": time.monotonic() - started,
            }
        )
    except Exception as error:
        report.update({"status": "failed", "error_type": type(error).__name__})
        raise
    finally:
        try:
            try:
                runner([*restored_compose, "down"])
            finally:
                # 恢复实例清理失败也必须尝试启动源环境，并保留真实收尾状态。
                runner([*original, "up", "-d", "--wait", "--wait-timeout", "300"])
                report["configuration_restored"] = True
        except Exception as error:
            report.update(
                {"status": "failed", "cleanup_error_type": type(error).__name__}
            )
            raise
        finally:
            target_report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    print("生产冷备恢复业务指纹与真实BGE/Milvus检索通过。")


if __name__ == "__main__":
    main()
