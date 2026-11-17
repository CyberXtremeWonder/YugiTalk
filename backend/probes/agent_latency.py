"""用合成问题和隔离 SQLite 对比真实模型的原闭环与快速路径，不读写业务数据库。"""

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from sqlalchemy.orm import Session
from test_agents import setup_task
from test_identity import client as client_fixture

from app.core.config import Settings
from app.providers.client import HTTPProvider
from app.tasks.execution import Execution
from app.tasks.models import Run


def main() -> None:
    """每次新建隔离应用和会话，唯一控制变量为冻结的 direct_mode。"""
    live = Settings()
    if live.model_provider == "mock" or not live.model_api_key.get_secret_value():
        raise SystemExit("需要配置真实模型")
    logging.disable(logging.CRITICAL)
    rows = []
    for question in ("你好", "什么是 RAG？"):
        for direct in (False, True):
            fixture = client_fixture.__wrapped__()
            client = next(fixture)
            try:
                settings = client.app.state.settings
                for field in ("model_provider", "model_id", "model_api_key", "model_base_url"):
                    setattr(settings, field, getattr(live, field))
                settings.model_max_retries = 0
                _, body, task, _ = setup_task(
                    client, question, closed_loop=True, tools=[], token_budget=100000,
                    max_output_tokens=1024,
                    system_prompt="你是中文助手。正文简洁，不超过60字。概念问答无需检索。",
                )
                with Session(client.app.state.database) as db:
                    run = db.get(Run, task["id"])
                    assert run.snapshot["direct_mode"]
                    run.snapshot = {**run.snapshot, "direct_mode": direct}
                    db.commit()
                calls = []

                class MeasuredProvider(HTTPProvider):
                    def stream(self, messages, tools, max_tokens, *, measurements=calls):
                        started = perf_counter()
                        call = {"purpose": self.request_purpose}
                        measurements.append(call)
                        try:
                            yield from super().stream(messages, tools, max_tokens)
                        finally:
                            call["seconds"] = round(perf_counter() - started, 3)

                started = perf_counter()
                Execution(client.app.state.database, settings, task["id"]).execute(
                    lambda s, p, m: MeasuredProvider(s, p, m)
                )
                elapsed = perf_counter() - started
                result = client.get(f"/api/v1/runs/{task['id']}").json()
                trace_response = client.get(f"/api/v1/admin/traces/{task['id']}")
                assert trace_response.status_code == 200
                trace = trace_response.json()
                latency = trace["latency"]
                assert latency["total_ms"] is not None
                if result["status"] == "succeeded":
                    assert latency["first_text_ms"] is not None
                    assert latency["first_text_ms"] <= latency["total_ms"]
                assert trace["summary"]["model_calls"] == len(calls)
                messages = client.get(
                    f"/api/v1/conversations/{body['conversation_id']}/messages"
                ).json()["items"]
                row = {
                    "question": question, "direct": direct, "status": result["status"],
                    "error_code": result.get("error_code"), "seconds": round(elapsed, 3),
                    "model_calls": len(calls), "calls": calls,
                    "latency": latency,
                    "requests": [{"duration_ms": step["duration_ms"], **step["details"]}
                                 for step in trace["steps"] if step["kind"] == "llm"],
                    "answer": next((m["content"] for m in reversed(messages)
                                    if m["role"] == "assistant"), None),
                }
                rows.append(row)
                print(json.dumps(row, ensure_ascii=True), flush=True)
            finally:
                fixture.close()
    output = Path(__file__).resolve().parents[2] / "docs/evaluation/AGENT_LATENCY_METRICS.json"
    output.write_text(json.dumps({"model": live.model_id, "results": rows,
                                  "measured_at": datetime.now(UTC).isoformat(),
                                  "scope": "Synthetic questions in isolated SQLite; one per mode"},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    if any(row["status"] != "succeeded" for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
