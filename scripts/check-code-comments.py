"""检查自有 Python 类与具名函数的中文说明，不扫描依赖和生成产物。"""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHINESE = re.compile(r"[\u4e00-\u9fff]")


def check() -> dict[str, object]:
    """遍历源码、迁移、探测和测试，报告缺少中文文档字符串的声明。"""
    roots = [
        ROOT / "backend/app",
        ROOT / "backend/probes",
        ROOT / "backend/tests",
        ROOT / "backend/migrations",
        ROOT / "backend/start.py",
        ROOT / "scripts",
    ]
    count = 0
    missing = []
    for root in roots:
        for path in [root] if root.is_file() else sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                count += 1
                if not CHINESE.search(ast.get_docstring(node) or ""):
                    missing.append(
                        {
                            "file": str(path.relative_to(ROOT)),
                            "line": node.lineno,
                            "name": node.name,
                        }
                    )
    return {"declarations": count, "missing": missing, "passed": not missing}


def main() -> None:
    """保存中文注释检查结果，缺失说明时返回非零退出码。"""
    report = check()
    target = ROOT / "docs/stages/artifacts/modules-python-comments.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
