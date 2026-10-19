"""验证持久挂载与业务指纹，可选执行开发环境停机重启验收。"""

import argparse
import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """校验所有开发数据服务均绑定项目目录，并记录真实重启结果。"""
    parser = argparse.ArgumentParser(description="项目数据目录验收")
    parser.add_argument(
        "--restart", action="store_true", help="停机重启开发服务，验证持久化"
    )
    args = parser.parse_args()
    migration = runpy.run_path(str(ROOT / "scripts/migrate-project-data.py"))
    run = migration["run"]
    compose = migration["COMPOSE"]
    services = migration["SERVICES"]
    mounts = {}
    for service, (_, relative, destination) in services.items():
        data = json.loads(run(["docker", "inspect", f"yugitalk-dev-{service}-1"]))[0]
        mount = next(row for row in data["Mounts"] if row["Destination"] == destination)
        assert mount["Type"] == "bind", f"{service} 未使用目录挂载"
        assert Path(mount["Source"]).resolve() == (ROOT / "data" / relative).resolve()
        assert data["State"]["Health"]["Status"] == "healthy"
        mounts[service] = mount["Source"]
    before = migration["probe"]()
    if args.restart:
        active = run(
            [
                *compose,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "yugitalk",
                "-d",
                "yugitalk",
                "-At",
                "-c",
                "SELECT (SELECT count(*) FROM runs WHERE status IN ('queued','running')) + (SELECT count(*) FROM documents WHERE status IN ('queued','parsing','indexing'));",
            ]
        )
        assert active.strip() == "0", "有执行中的任务，禁止重启验收"
        try:
            run([*compose, "stop", *migration["CLIENTS"]])
            run([*compose, "stop", "milvus"])
            run([*compose, "stop", "postgres", "redis", "etcd", "storage"])
        finally:
            run([*compose, "up", "-d", "--wait", "--wait-timeout", "240"])
    after = migration["probe"]()
    assert before == after, "重启前后的业务数据指纹不一致"
    report = {
        "status": "passed",
        "mounts": mounts,
        "restart_verified": args.restart,
        "before": before,
        "after": after,
    }
    target = ROOT / "docs/stages/artifacts/project-data-check.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "持久目录与业务指纹检查通过。" + ("停机重启验收通过。" if args.restart else "")
    )


if __name__ == "__main__":
    main()
