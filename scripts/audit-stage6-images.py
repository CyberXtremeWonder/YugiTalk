"""从本地生产镜像导出归档，再使用已下载漏洞库离线扫描全部运行服务。"""

import argparse
import json
import re
import runpy
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRIVY = (
    "aquasec/trivy:0.75.0@sha256:"
    "af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa"
)


def run(arguments: list[str]) -> str:
    """有界运行命令，只返回镜像操作输出，不读取或打印应用凭证。"""
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=1200,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] or "镜像检查失败")
    return result.stdout


def main() -> None:
    """逐个扫描实际生产配置的镜像；保留原始报告和未修复项，不忽略漏洞。"""
    parser = argparse.ArgumentParser()
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--service", action="append", help="仅扫描指定服务；不会覆盖完整扫描报告"
    )
    selection.add_argument(
        "--candidate-image",
        action="append",
        help="离线评估候选镜像，不启动或替换任何服务",
    )
    args = parser.parse_args()
    grouped: dict[str, list[str]] = {}
    if args.candidate_image:
        for image in args.candidate_image:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]+", image):
                raise ValueError("候选镜像名称格式无效")
            grouped[image] = ["candidate"]
    else:
        check = runpy.run_path(str(ROOT / "scripts/check-stage6-production.py"))
        config = json.loads(run([*check["compose"](), "config", "--format", "json"]))
        if args.service and any(
            name not in config["services"] for name in args.service
        ):
            raise ValueError("指定服务不属于阶段6配置")
        for name, service in config["services"].items():
            if name != "model-fixture" and (not args.service or name in args.service):
                grouped.setdefault(service["image"], []).append(name)
    if not grouped:
        raise ValueError("没有可扫描的运行组件，不能生成通过报告")
    cache = ROOT / ".cache"
    target = ROOT / "docs/stages/artifacts"
    report: dict[str, object] = {
        "status": "running",
        "network": "none",
        "scope": "candidate_images_not_deployed"
        if args.candidate_image
        else "production_components_and_local_storage_fixture",
        "images": [],
    }
    output = target / (
        "stage6-candidate-images-audit.json"
        if args.candidate_image
        else "stage6-selected-images-audit.json"
        if args.service
        else "stage6-all-images-audit.json"
    )
    report["selected_services"] = args.service or []
    try:
        for image, services in grouped.items():
            identity = run(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
            short_id = identity.strip().split(":")[-1][:12]
            archive = cache / f"stage6-audit-{short_id}.tar"
            raw_name = f"stage6-image-{short_id}.json"
            if not archive.exists():
                run(["docker", "image", "save", "--output", str(archive), image])
            # 容器无网络、无Docker socket，仅能读归档和漏洞库，不能上传源码/SBOM。
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--mount",
                    f"type=bind,source={cache},target=/cache",
                    "--mount",
                    f"type=bind,source={target},target=/reports",
                    TRIVY,
                    "image",
                    "--cache-dir",
                    "/cache/trivy",
                    "--skip-db-update",
                    "--skip-java-db-update",
                    "--scanners",
                    "vuln",
                    "--input",
                    f"/cache/{archive.name}",
                    "--format",
                    "json",
                    "--output",
                    f"/reports/{raw_name}",
                ]
            )
            raw = json.loads((target / raw_name).read_text(encoding="utf-8"))
            vulnerabilities = [
                row
                for result in raw.get("Results", [])
                for row in result.get("Vulnerabilities", [])
            ]
            counts = dict(Counter(row["Severity"] for row in vulnerabilities))
            item = {
                "scope": "candidate_not_deployed"
                if args.candidate_image
                else "local_storage_fixture"
                if services == ["storage"]
                else "production_component",
                "services": services,
                "image_id": identity.strip(),
                "report": raw_name,
                "severity_counts": counts,
                "high_or_critical_with_fix": sum(
                    row["Severity"] in {"HIGH", "CRITICAL"}
                    and bool(row.get("FixedVersion"))
                    for row in vulnerabilities
                ),
            }
            report["images"].append(item)
            output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(
                json.dumps({"services": services, "severity_counts": counts}),
                flush=True,
            )
        report["status"] = (
            "blocked_findings"
            if any(
                row["severity_counts"].get("HIGH", 0)
                or row["severity_counts"].get("CRITICAL", 0)
                for row in report["images"]
            )
            else "passed"
        )
    except Exception as error:
        report.update({"status": "failed", "error_type": type(error).__name__})
        raise
    finally:
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
