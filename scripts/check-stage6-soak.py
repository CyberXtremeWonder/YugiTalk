"""在隔离HTTPS环境执行原生Mock混合压测，保留数据集、耗时和一致性证据。"""

import argparse
import concurrent.futures
import json
import math
import runpy
import ssl
import statistics
import threading
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "docs/stages/artifacts"
FIELDS = [
    "验收口令",
    "负责人",
    "值班时段",
    "数据目录",
    "备份频率",
    "日志保留",
    "端口",
    "审批角色",
    "资料格式",
    "恢复目标",
]


def corpus() -> list[dict[str, object]]:
    """固定生成100份人工规则标注的中文合成资料，不冒充真实业务或语义泛化测试。"""
    documents = []
    flowers = [
        "梅花",
        "兰花",
        "竹叶",
        "菊花",
        "牡丹",
        "荷花",
        "桂花",
        "海棠",
        "水仙",
        "茉莉",
    ]
    cities = [
        "上海",
        "北京",
        "广州",
        "成都",
        "杭州",
        "南京",
        "武汉",
        "西安",
        "厦门",
        "苏州",
    ]
    for index in range(100):
        name = flowers[index // 10] + cities[index % 10] + "项目"
        values = [
            f"星河{index:03d}",
            f"负责人{index:03d}",
            f"{index % 24:02d}点至{(index + 8) % 24:02d}点",
            f"项目目录/data/项目{index:03d}",
            f"每{index % 7 + 1}天",
            f"{index % 30 + 14}天",
            str(20000 + index),
            "空间管理员",
            "UTF-8 Markdown",
            f"{index % 60 + 30}分钟",
        ]
        content = "\n".join(
            f"{name}的{field}是{value}。"
            for field, value in zip(FIELDS, values, strict=True)
        )
        questions = [
            {"query": f"{name}的{field}是什么？", "answer": value}
            for field, value in zip(FIELDS, values, strict=True)
        ]
        documents.append(
            {"filename": name + ".md", "content": content, "questions": questions}
        )
    return documents


def percentile(values: list[float], quantile: float = 0.95) -> float | None:
    """采用最近秩定义计算分位数，空样本不伪造零延迟。"""
    return sorted(values)[math.ceil(len(values) * quantile) - 1] if values else None


class Actor:
    """每个压测用户独立持有连接和Cookie，避免共享会话引起角色混淆。"""

    def __init__(
        self, values: dict[str, str], email: str, context: ssl.SSLContext
    ) -> None:
        """校验本地测试CA并登录，不打印密码或会话内容。"""
        self.client = httpx.Client(
            base_url="https://127.0.0.1:18443",
            verify=context,
            trust_env=False,
            timeout=180,
        )
        identity = (
            self.client.post(
                "/api/v1/auth/login",
                headers={"Origin": values["PUBLIC_ORIGIN"]},
                json={"email": email, "password": values["E2E_PASSWORD"]},
            )
            .raise_for_status()
            .json()
        )
        self.space = next(
            row["id"] for row in identity["workspaces"] if row["role"] == "owner"
        )
        self.shared = next(
            row["id"] for row in identity["workspaces"] if row["name"].endswith("-0")
        )
        self.agent = ""
        self.headers = {
            "Origin": values["PUBLIC_ORIGIN"],
            "X-CSRF-Token": identity["csrf_token"],
        }

    def post(
        self, route: str, payload: dict[str, object], key: str | None = None
    ) -> dict[str, object]:
        """发送已认证写请求，幂等键仅用于明确的任务提交。"""
        headers = {**self.headers, **({"Idempotency-Key": key} if key else {})}
        return (
            self.client.post(route, json=payload, headers=headers)
            .raise_for_status()
            .json()
        )

    def get(self, route: str) -> dict[str, object]:
        """读取授权数据，任何非成功响应都作为真实压测失败。"""
        return self.client.get(route).raise_for_status().json()

    def close(self) -> None:
        """关闭线程独占连接，避免压测客户端自身泄漏。"""
        self.client.close()


def seed_users(
    arguments: list[str], runner: object, namespace: str, password: str
) -> list[str]:
    """仅在隔离项目内创建20个用户/空间和共享检索权限，凭证从stdin进入。"""
    emails = [f"{namespace}-{index}@example.test" for index in range(20)]
    # 传入JSON放在脚本stdin中，不进入进程参数、日志或验收报告。
    code = """import json,redis
from app.core.config import Settings
from app.core.database import create_database
from app.identity.models import User
from app.identity.security import hasher
from app.workspace.models import Membership,Workspace
from sqlalchemy.orm import Session
payload=json.loads(INPUT)
settings=Settings(); assert settings.app_env=='test' and settings.model_provider=='mock'
engine=create_database(settings)
# 测试身份准备会重置这个隔离实例的登录计数，不改变正式配置和其他Redis键。
client=redis.Redis.from_url(settings.redis_url.get_secret_value())
keys=list(client.scan_iter(match='yugitalk:login:*'))
if keys: client.delete(*keys)
client.close()
with Session(engine) as db:
 hashed=hasher.hash(payload['password']); shared=None
 for index,email in enumerate(payload['emails']):
  space=Workspace(name='阶段六压测'+payload['namespace']+'-'+str(index)); db.add(space); db.flush()
  if index==0: shared=space.id
  user=User(email=email,name='压测用户'+str(index),password_hash=hashed)
  db.add(user); db.flush()
  db.add(Membership(workspace_id=space.id,user_id=user.id,role='owner'))
  if index: db.add(Membership(workspace_id=shared,user_id=user.id,role='member'))
 db.commit()
engine.dispose()
"""
    payload = json.dumps(
        {"namespace": namespace, "password": password, "emails": emails}
    )
    runner(
        [*arguments, "exec", "-T", "api", "python", "-"],
        "INPUT=" + repr(payload) + "\n" + code,
    )
    return emails


def prepare_corpus(owner: Actor) -> tuple[str, list[dict[str, object]]]:
    """经真实上传/索引接口准备基线，记录失败步骤，等待100个文档全部可检索。"""
    documents = corpus()
    target = ROOT / "docs/evaluation/stage6-corpus-v1.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        json.dumps(
            {"version": 1, "synthetic": True, "documents": documents},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    base = owner.post(
        f"/api/v1/workspaces/{owner.space}/knowledge-bases",
        {"name": "100份中文合成稳定性资料"},
    )["id"]
    for index, document in enumerate(documents):
        response = (
            owner.client.post(
                f"/api/v1/knowledge-bases/{base}/documents",
                params={"filename": document["filename"]},
                content=document["content"].encode(),
                headers={**owner.headers, "Content-Type": "application/octet-stream"},
            )
            .raise_for_status()
            .json()
        )
        document["document_id"] = response["id"]
        if index % 20 == 19:
            print(f"合成资料已上传：{index + 1}/100", flush=True)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        rows = owner.get(f"/api/v1/knowledge-bases/{base}/documents")["items"]
        assert not any(row["status"] == "failed" for row in rows), "合成资料索引失败"
        ready = sum(row["status"] == "ready" for row in rows)
        print(f"索引就绪：{ready}/100", flush=True)
        if len(rows) == ready == 100:
            return str(base), documents
        time.sleep(15)
    raise RuntimeError("100份资料索引超时")


def evaluate_retrieval(
    owner: Actor, base: str, documents: list[dict[str, object]]
) -> dict[str, object]:
    """核对1000个标注问题的相关文档与答案片段，召回率不等于生成答案正确率。"""
    cases = []
    for index, document in enumerate(documents):
        for question in document["questions"]:
            started = time.monotonic()
            sources = owner.post(
                f"/api/v1/knowledge-bases/{base}/search", {"query": question["query"]}
            )["items"]
            matches = [
                row
                for row in sources[:5]
                if row["document_id"] == document["document_id"]
                and question["answer"] in row["content"]
            ]
            cases.append(
                {
                    "query": question["query"],
                    "expected_filename": document["filename"],
                    "recalled": bool(matches),
                    "seconds": time.monotonic() - started,
                }
            )
        if index % 10 == 9:
            print(f"标注检索已验证：{len(cases)}/1000", flush=True)
    report = {
        "synthetic": True,
        "documents": 100,
        "questions": 1000,
        "embedding": "BAAI/bge-m3",
        "recall_at_5": sum(row["recalled"] for row in cases) / 1000,
        "cases": cases,
    }
    report["status"] = "passed" if report["recall_at_5"] >= 0.85 else "failed"
    (REPORTS / "stage6-corpus-retrieval.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def resource_sample(arguments: list[str], runner: object) -> dict[str, object]:
    """记录容器内存、队列长度与关系数据规模；不输出业务内容和认证数据。"""
    statistics = runner(["docker", "stats", "--no-stream", "--format", "{{json .}}"])
    rows = [json.loads(line) for line in statistics.splitlines() if line.strip()]
    code = """import json,redis
from app.core.config import Settings
from app.core.database import create_database
from sqlalchemy import text
s=Settings(); r=redis.Redis.from_url(s.redis_url.get_secret_value())
e=create_database(s)
with e.connect() as c:
 print(json.dumps({'queue_depth':r.llen(s.task_queue),'db_bytes':c.execute(text('SELECT pg_database_size(current_database())')).scalar_one()}))
r.close();e.dispose()
"""
    state = json.loads(runner([*arguments, "exec", "-T", "api", "python", "-"], code))
    state["containers"] = [
        {"name": row["Name"], "memory": row["MemUsage"], "cpu_percent": row["CPUPerc"]}
        for row in rows
        if row["Name"].startswith("yugitalk-stage6-")
    ]
    return state


def consistency(arguments: list[str], runner: object, space: str) -> dict[str, object]:
    """等待任务排空后核对消息、幂等账本、预算与失败状态，发现异常不能只报HTTP成功。"""
    code = """import json
from app.core.config import Settings
from app.core.database import create_database
from sqlalchemy import text
e=create_database(Settings())
with e.connect() as c:
 args={'space':SPACE+'%'}
 states=dict(c.execute(text('SELECT status,count(*) FROM runs WHERE workspace_id IN (SELECT id FROM workspaces WHERE name LIKE :space) GROUP BY status'),args).all())
 checks={
 'duplicate_tool_keys':"SELECT count(*) FROM (SELECT t.run_id,t.call_key FROM tool_executions t JOIN runs r ON r.id=t.run_id WHERE r.workspace_id IN (SELECT id FROM workspaces WHERE name LIKE :space) GROUP BY t.run_id,t.call_key HAVING count(*)>1) d",
 'duplicate_assistant_messages':"SELECT count(*) FROM (SELECT m.conversation_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.workspace_id IN (SELECT id FROM workspaces WHERE name LIKE :space) AND m.role='assistant' GROUP BY m.conversation_id HAVING count(*)>1) d",
 'succeeded_without_message':"SELECT count(*) FROM runs r LEFT JOIN messages m ON m.id=r.id WHERE r.workspace_id IN (SELECT id FROM workspaces WHERE name LIKE :space) AND r.status='succeeded' AND m.id IS NULL",
 'failed_documents':"SELECT count(*) FROM documents WHERE workspace_id IN (SELECT id FROM workspaces WHERE name LIKE :space) AND status='failed'",
 'tool_rows':"SELECT count(*) FROM tool_executions t JOIN runs r ON r.id=t.run_id WHERE r.workspace_id IN (SELECT id FROM workspaces WHERE name LIKE :space)",
 'reserved_budget':"SELECT COALESCE(sum(reserved),0) FROM budgets WHERE key IN (SELECT 'space:'||id FROM workspaces WHERE name LIKE :space)",
 }
 result={key:c.execute(text(sql),args).scalar_one() for key,sql in checks.items()}
 result['run_states']=states
 print(json.dumps(result))
e.dispose()
"""
    return json.loads(
        runner(
            [*arguments, "exec", "-T", "api", "python", "-"],
            "SPACE=" + repr(space) + "\n" + code,
        )
    )


def memory_analysis(samples: list[dict[str, object]]) -> dict[str, object]:
    """比较预热五分钟后的首尾窗口内存中位数，有界判断增长，不作永久无泄漏保证。"""
    series: dict[str, list[float]] = {}
    units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    for sample in samples:
        if sample["elapsed_seconds"] < 300:
            continue
        for row in sample["containers"]:
            raw = row["memory"].split(" / ")[0]
            unit = next(
                value for value in ("GiB", "MiB", "KiB", "B") if raw.endswith(value)
            )
            series.setdefault(row["name"], []).append(
                float(raw.removesuffix(unit)) * units[unit]
            )
    rows = []
    for name, values in series.items():
        if len(values) < 10:
            continue
        initial, final = statistics.median(values[:5]), statistics.median(values[-5:])
        threshold = max(128 * 1024**2, initial * 0.25)
        rows.append(
            {
                "name": name,
                "initial_median_bytes": initial,
                "final_median_bytes": final,
                "growth_bytes": final - initial,
                "growth_flag": final - initial > threshold,
            }
        )
    return {
        "status": "passed"
        if rows and not any(row["growth_flag"] for row in rows)
        else "insufficient_samples"
        if not rows
        else "failed",
        "warmup_seconds": 300,
        "method": "首尾各5次采样中位数；增长超过128MiB或初始25%中的较大值则失败",
        "containers": rows,
    }


def exercise(
    actor: Actor, agent: str, base: str, index: int, cycle: int
) -> dict[str, object]:
    """混合查询、聊天、两类工具、RAG、取消和幂等重发，测量首个真实文本事件。"""
    kind = ["chat", "text_stats", "format_report", "rag", "cancel", "read"][cycle % 6]
    if kind == "read":
        started = time.monotonic()
        actor.get(f"/api/v1/workspaces/{actor.space}/conversations?limit=20")
        return {"kind": kind, "ordinary_seconds": time.monotonic() - started}
    conversation_space = actor.shared if kind == "rag" else actor.space
    conversation = actor.post(
        f"/api/v1/workspaces/{conversation_space}/conversations",
        {"title": f"压测{index}-{cycle}"},
    )["id"]
    message = {
        "chat": "你好，请简短介绍自己。",
        "text_stats": "调用工具统计中文字符。",
        "format_report": "生成一份报告：任务已经完成。",
        "rag": "梅花上海项目的验收口令是什么？",
        "cancel": "请介绍任务流程。",
    }[kind]
    payload = {
        "conversation_id": conversation,
        "agent_version_id": agent if kind == "rag" else actor.agent,
        "message": message,
    }
    if kind == "rag":
        payload["knowledge_base_id"] = base
    key = str(uuid4())
    started = time.monotonic()
    task = actor.post("/api/v1/runs", payload, key)
    submitted = time.monotonic()
    if kind == "cancel":
        actor.post(f"/api/v1/runs/{task['id']}/cancel", {})
    first_text = None
    with actor.client.stream("GET", f"/api/v1/runs/{task['id']}/events") as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if line == "event: text_delta" and first_text is None:
                first_text = time.monotonic() - started
    terminal = actor.get(f"/api/v1/runs/{task['id']}")
    # 取消请求可能与成功提交竞争；两个终态均合法，失败或仍在执行不合法。
    assert terminal["status"] in (
        {"cancelled", "succeeded"} if kind == "cancel" else {"succeeded"}
    ), terminal.get("error_code")
    if kind != "cancel":
        assert first_text is not None, "成功任务没有有效文本事件"
        # 首内容测量不额外等待重发；完成后验证持久幂等，禁止再次执行。
        assert actor.post("/api/v1/runs", payload, key)["id"] == task["id"], (
            "幂等重发返回不同任务"
        )
    return {
        "kind": kind,
        "cancel_finished_before_request": kind == "cancel"
        and terminal["status"] == "succeeded",
        "first_text_seconds": first_text,
        "submission_seconds": submitted - started,
        "run_id": task["id"],
        "seconds": time.monotonic() - started,
    }


def main() -> None:
    """启动测试专用覆盖配置，执行有界压测；最后无条件恢复原生产协议验收配置。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument(
        "--skip-retrieval", action="store_true", help="仅调试压测脚本，不作为完整验收"
    )
    args = parser.parse_args()
    if not 30 <= args.seconds <= 1800:
        parser.error("测试时长须为30至1800秒；正式验收必须1800秒")
    check = runpy.run_path(str(ROOT / "scripts/check-stage6-production.py"))
    runner, original = check["run"], check["compose"]()
    # 插入额外文件，不能修改私有环境文件，更不能污染开发默认Compose。
    arguments = [*original, "-f", str(ROOT / "docker-compose.stage6-soak.yml")]
    values = {
        key: str(value)
        for key, value in dotenv_values(
            ROOT / ".cache/stage6-production.env", interpolate=False
        ).items()
        if value is not None
    }
    namespace = "soak" + str(time.time_ns())
    context = ssl.create_default_context(
        cafile=str(ROOT / values["PROD_DATA_ROOT"] / "tls/fullchain.pem")
    )
    report: dict[str, object] = {
        "status": "running",
        "duration_target_seconds": args.seconds,
        "concurrency": 20,
        "model": "native mock-v1",
        "paid_calls": 0,
        "login_counters_reset_in_isolated_setup": True,
        "worker_processes": 8,
        "space_distribution": "20个独立空间，共享其中一个空间的RAG知识库",
        "api_cpus": None,
        "worker_cpus": None,
        "failures": [],
        "operations": [],
        "resources": [],
    }
    full_test = args.seconds == 1800 and not args.skip_retrieval
    output = REPORTS / ("stage6-soak.json" if full_test else "stage6-soak-debug.json")
    if output.exists():
        previous = REPORTS / (output.stem + "-" + str(time.time_ns()) + ".json")
        previous.write_bytes(output.read_bytes())
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    actors = []
    lock = threading.Lock()
    stop = threading.Event()
    try:
        runner(
            [
                *arguments,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "180",
                "api",
                "worker",
                "scheduler",
            ]
        )
        identities = runner([*arguments, "ps", "-q"]).split()
        report["runtime_images"] = []
        for line in runner(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Name}} {{.Image}} {{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}",
                *identities,
            ]
        ).splitlines():
            name, image, cpus, memory = line.split()
            report["runtime_images"].append(
                {
                    "container": name,
                    "image_id": image,
                    "cpus": int(cpus) / 1e9,
                    "memory_limit_bytes": int(memory),
                }
            )
            for service in ("api", "worker"):
                if name.endswith("-" + service + "-1"):
                    report[service + "_cpus"] = int(cpus) / 1e9
        emails = seed_users(arguments, runner, namespace, values["E2E_PASSWORD"])
        actors = [Actor(values, email, context) for email in emails]
        owner = actors[0]
        base, documents = prepare_corpus(owner)
        report["dataset"] = {
            "base": base,
            "documents": 100,
            "questions": 1000,
            "version": "stage6-corpus-v1",
            "synthetic": True,
        }
        if not args.skip_retrieval:
            retrieval = evaluate_retrieval(owner, base, documents)
            report["retrieval_recall_at_5"] = retrieval["recall_at_5"]
        agent = owner.post(
            f"/api/v1/workspaces/{owner.space}/agents",
            {
                "name": "Mock稳定性压测",
                "max_output_tokens": 512,
                "token_budget": 30000,
                "timeout_seconds": 120,
            },
        )["version_id"]
        owner.agent = str(agent)
        for actor in actors[1:]:
            actor.agent = str(
                actor.post(
                    f"/api/v1/workspaces/{actor.space}/agents",
                    {
                        "name": "Mock稳定性压测",
                        "max_output_tokens": 512,
                        "token_budget": 30000,
                        "timeout_seconds": 120,
                    },
                )["version_id"]
            )
        hardware = json.loads(runner(["docker", "info", "--format", "{{json .NCPU}}"]))
        report["docker_cpus"] = hardware
        report["docker_memory_bytes"] = int(
            runner(["docker", "info", "--format", "{{.MemTotal}}"])
        )
        warmup = []
        for index, actor in enumerate(actors):
            warmup.append(exercise(actor, str(agent), base, index, 0))
        report["warmup"] = {
            "requests": 20,
            "method": "每个用户一次顺序Mock聊天；不计入正式负载延迟",
            "operations": warmup,
        }
        started = time.monotonic()
        deadline = started + args.seconds

        def worker(index: int) -> None:
            """各用户持续产生混合负载，保留每次失败类别，避免忙循环或无界并发。"""
            cycle = index
            while time.monotonic() < deadline and not stop.is_set():
                cycle_started = time.monotonic()
                try:
                    result = exercise(actors[index], str(agent), base, index, cycle)
                    with lock:
                        report["operations"].append(result)
                except (httpx.HTTPError, AssertionError, ValueError, KeyError) as error:
                    failure = {
                        "actor": index,
                        "cycle": cycle,
                        "error_type": type(error).__name__,
                    }
                    if isinstance(error, AssertionError):
                        failure["reason"] = str(error)[:100]
                    if isinstance(error, httpx.HTTPStatusError):
                        failure["http_status"] = error.response.status_code
                        failure["route"] = error.request.url.path
                    with lock:
                        report["failures"].append(failure)
                cycle += 1
                stop.wait(
                    max(
                        0,
                        min(
                            deadline - time.monotonic(),
                            5 - (time.monotonic() - cycle_started),
                        ),
                    )
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(worker, index) for index in range(20)]
            while time.monotonic() < deadline:
                sample = resource_sample(arguments, runner)
                sample["elapsed_seconds"] = time.monotonic() - started
                report["resources"].append(sample)
                with lock:
                    report["elapsed_seconds"] = sample["elapsed_seconds"]
                    output.write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(
                        json.dumps(
                            {
                                "elapsed_seconds": round(sample["elapsed_seconds"]),
                                "operations": len(report["operations"]),
                                "failures": len(report["failures"]),
                                "queue": sample["queue_depth"],
                            }
                        ),
                        flush=True,
                    )
                stop.wait(min(30, max(0, deadline - time.monotonic())))
            for future in futures:
                future.result()
        report["elapsed_seconds"] = time.monotonic() - started
        # 已接受任务必须排空，尤其取消与同一幂等键不能留有第二个活动任务。
        for _ in range(60):
            result = consistency(arguments, runner, "阶段六压测" + namespace)
            if not sum(
                result["run_states"].get(state, 0)
                for state in ("queued", "running", "waiting_approval")
            ):
                break
            time.sleep(1)
        report["consistency"] = result
        operations = report["operations"]
        report["operation_counts"] = dict(Counter(row["kind"] for row in operations))
        plain = [
            row["first_text_seconds"] for row in operations if row["kind"] == "chat"
        ]
        report["chat_first_text_p95_seconds"] = percentile(plain)
        report["ordinary_p95_seconds"] = percentile(
            [row["ordinary_seconds"] for row in operations if row["kind"] == "read"]
        )
        report["unexpected_error_rate"] = len(report["failures"]) / max(
            1, len(operations) + len(report["failures"])
        )
        report["memory_analysis"] = memory_analysis(report["resources"])
        report["max_sampled_queue_depth"] = max(
            sample["queue_depth"] for sample in report["resources"]
        )
        healthy = not any(
            result[key]
            for key in (
                "duplicate_tool_keys",
                "duplicate_assistant_messages",
                "succeeded_without_message",
                "failed_documents",
                "reserved_budget",
            )
        ) and not result["run_states"].get("failed", 0)
        healthy = healthy and not sum(
            result["run_states"].get(state, 0)
            for state in ("queued", "running", "waiting_approval")
        )
        healthy = healthy and report["max_sampled_queue_depth"] <= 100
        if args.seconds == 1800:
            healthy = healthy and report["memory_analysis"]["status"] == "passed"
        report["status"] = (
            "passed"
            if healthy
            and report["unexpected_error_rate"] < 0.01
            and report["chat_first_text_p95_seconds"] <= 1
            and report["ordinary_p95_seconds"] <= 0.5
            else "failed"
        )
        report["full_duration_verified"] = (
            full_test and report["elapsed_seconds"] >= 1800
        )
        report["scope"] = (
            "Mock稳定性；测试APP_ENV、并发和额度容量覆盖已明确记录；不替代生产模型质量"
        )
    except Exception as error:
        stop.set()
        report.update({"status": "failed", "error_type": type(error).__name__})
        raise
    finally:
        for actor in actors:
            actor.close()
        try:
            runner(
                [
                    *original,
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    "api",
                    "worker",
                    "scheduler",
                ]
            )
            report["configuration_restored"] = True
        finally:
            if not report.get("configuration_restored"):
                report.update(status="failed", configuration_restored=False)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(
            json.dumps(
                {key: report[key] for key in ("status", "duration_target_seconds")}
            ),
            flush=True,
        )
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
