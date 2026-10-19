"""提供两步命令，验证真实 Worker 重启前后的检查点恢复。"""

import argparse
import json

from probes.worker import advance


def main() -> None:
    """先暂停、再由新 Worker 恢复；任何断言失败均返回非零退出码。"""
    parser = argparse.ArgumentParser(description="阶段 0 Worker 重启恢复验证")
    parser.add_argument("action", choices=["begin", "resume"])
    parser.add_argument("--thread", required=True)
    args = parser.parse_args()
    if args.action == "begin":
        result = advance.delay(args.thread).get(timeout=30)
        assert result["waiting"] is True
        assert "status" not in result["values"]
    else:
        result = advance.delay(args.thread, "approve").get(timeout=30)
        assert result["waiting"] is False
        assert result["values"]["status"] == "approved"
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
