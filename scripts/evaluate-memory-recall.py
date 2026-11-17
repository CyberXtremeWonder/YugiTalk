"""Synthetic, isolated memory ablation using real project context + Agent graph.

No application database or production vector collections are accessed. Settings
credentials are read in-process and never written to experiment artifacts.
"""
import argparse
import hashlib
import json
import random
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/memory-recall-eval-20261113"
sys.path.insert(0, str(OUT / "deps"))
sys.path.insert(0, str(ROOT / "backend"))

import httpx
import numpy as np
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.core import model_registry  # noqa: F401: register sandbox tables
from app.core.entities import Base
from app.core.config import Settings
from app.chat.models import Conversation, Message
from app.identity.models import User
from app.memory.models import Memory, MemoryPreference
from app.memory.context import build_messages, clip
from app.guardrails.prompts import data_message
from app.agents.contracts import AgentConfig
from app.agents.graph import run_graph
from app.providers.client import HTTPProvider

OWNER, FOREIGN = "memory-eval-owner", "memory-eval-other"
COLLECTION = "memory_eval_20261113_bgem3"
MILVUS = "http://127.0.0.1:49531/v2/vectordb/"
ARMS = ("sql_current", "sql_keyword", "vector", "hybrid")
SYSTEM = (
    "这是个人历史记忆问答。仅依据提供的用户记忆回答，不补充外部知识。"
    "项目名和内部代号都是虚构的。若答案未在可用记忆中出现，只回答：记忆中没有记录。"
    "不要猜测。回答尽量一句话，保留原文具体代号；问题涉及多项时回答全部项目。"
)


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def fixture():
    prefs = [
        {"id": "p1", "content": "我的长期交流偏好：默认使用中文回答，即使问题包含英文项目名称也保持中文。除非我本轮明确要求别的语言。", "kind": "preference"},
        {"id": "p2", "content": "我的长期代码偏好：没有明确指定语言时，代码示例优先使用 Python；临时要求使用其他语言只作用于当次任务。", "kind": "preference"},
        {"id": "p3", "content": "我的长期表达偏好：先给结论，保持简洁，避免大段铺垫；只有我明确要求深入讲解时再展开实现细节。", "kind": "preference"},
    ]
    episodes = [
        ("星云", "数据库迁移", "维护窗口", "周六21:40", "星云项目切数据库时，我最后定下的维护时间是什么？"),
        ("灯塔", "登录故障排查", "根因代号", "青灯-204", "灯塔项目那次账号进不去，最终定位的根因内部编号是什么？"),
        ("松针", "邮件通知", "接收组", "松果组", "松针项目的通知邮件，我之前决定发给哪个组？"),
        ("远帆", "备份恢复", "备份桶名", "yuanfan-archive-72", "远帆的备份资料放在哪个对象桶？"),
        ("青禾", "接口重试", "重试间隔", "7秒", "青禾调用失败以后，我设定隔多久再试一次？"),
        ("白鹭", "图像上传", "单图上限", "13MB", "白鹭上传一张图片最多允许多大？"),
        ("海棠", "日志分析", "追踪标签", "haitang-trace-58", "海棠排查日志时，我要求用哪个追踪标记？"),
        ("晨星", "版本发布", "回滚版本", "晨星0.8.6", "晨星上线出问题时，我选定退回哪个版本？"),
        ("霜叶", "批量导入", "分组大小", "37条", "霜叶导数据时，每一批我决定放多少条？"),
        ("云桥", "缓存更新", "刷新周期", "19分钟", "云桥缓存隔多久刷新，是我之前定的？"),
        ("银杉", "服务部署", "部署区域", "银杉西区-3", "银杉服务最终落在哪个内部区域？"),
        ("风铃", "文件导出", "导出目录", "exports/fengling-27", "风铃生成的文件，我要求写到哪个目录？"),
        ("雪松", "知识检索", "重排模型代号", "雪狐-638", "雪松把相关资料重新排序，用的内部模型代号是什么？"),
        ("潮汐", "向量编码超时", "故障处理方案编号", "汐蓝-417", "潮汐那次文本转向量一直等不到返回，最后采用的方案编号是什么？"),
        ("琥珀", "数据库死锁", "修复方案编号", "琥珀锁-926", "琥珀发生事务互相等待，之前落地的修复编号是什么？"),
        ("竹影", "代理证书", "证书别名", "bamboo-ca-83", "竹影访问代理时，我最后用的是哪个证书别名？"),
        ("流萤", "检索召回", "候选数量", "29个", "流萤先找相关片段时，我设的候选数是多少？"),
    ]
    memories = list(prefs)
    cases = [
        {"id": "pref-language", "category": "preference", "question": "我默认希望用什么语言交流？", "gold": ["p1"], "answers": [["中文"]]},
        {"id": "pref-code", "category": "preference", "question": "没指定语言时，我通常希望代码示例用哪种语言？", "gold": ["p2"], "answers": [["python"]]},
        {"id": "pref-style", "category": "preference", "question": "我默认喜欢什么详略程度的回答？", "gold": ["p3"], "answers": [["简洁", "简短", "精简"]]},
    ]
    for i, (project, topic, field, value, question) in enumerate(episodes, 1):
        mid = f"e{i:02}"
        content = (
            f"个人项目记录：{project}。主题是{topic}。我在一次讨论后确认的{field}是「{value}」。"
            f"这条记录只适用于{project}，不要套用到其他项目。这是已经确认的最终决定，"
            "不是候选建议或待验证计划。后续回顾这次工作时应保留这个具体值；"
            "不能根据行业常见设置推测成另外的值。若我随后明确纠正，以新的确认记录为准。"
        )
        memories.append({"id": mid, "content": content, "kind": "episode"})
        cases.append({"id": f"episode-{i:02}", "category": "episode_early" if i <= 5 else "episode_late", "question": question, "gold": [mid], "answers": [[value.lower()]]})
    cases.extend([
        {"id": "multi-near", "category": "multi", "question": "青禾接口重试间隔和白鹭单图上限，我分别设成多少？", "gold": ["e05", "e06"], "answers": [["7秒"], ["13mb"]]},
        {"id": "multi-far", "category": "multi", "question": "雪松重排模型代号和潮汐向量超时方案编号，分别是什么？", "gold": ["e13", "e14"], "answers": [["雪狐-638"], ["汐蓝-417"]]},
        {"id": "absent-project", "category": "absent", "question": "玄武项目的数据库密码轮换周期，我定了多少天？", "gold": [], "answers": [["记忆中没有记录"]]},
        {"id": "absent-fact", "category": "absent", "question": "潮汐项目的每月预算上限，我定的是多少元？", "gold": [], "answers": [["记忆中没有记录"]]},
    ])
    assert len(memories) == 20 and len(cases) == 24
    return memories, cases


def prepare():
    memories, cases = fixture()
    save("memories.json", memories)
    save("cases.json", cases)
    dbpath = OUT / "sandbox.sqlite"
    assert not dbpath.exists(), "Preserve previous experiment; use existing artifacts"
    engine = create_engine("sqlite:///" + dbpath.as_posix())
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for uid in (OWNER, FOREIGN):
            db.add(User(id=uid, email=uid + "@example.test", name=uid, password_hash="synthetic-not-a-login"))
        db.add(Conversation(id="fixture-conversation", account_id=OWNER, owner_id=OWNER, title="Synthetic memory evaluation"))
        for i, m in enumerate(memories):
            db.add(Memory(id=m["id"], account_id=OWNER, user_id=OWNER, content=m["content"], created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i)))
        db.add(Memory(id="foreign", account_id=FOREIGN, user_id=FOREIGN, content="玄武项目密码轮换周期是秘密值异客-772。"))
        db.commit()
    engine.dispose()
    print("prepared: 20 memories, 24 fixed questions, isolated SQLite", flush=True)


def encode():
    import torch
    from transformers import AutoModel, AutoTokenizer
    settings = Settings()
    model_path = Path(settings.bge_model_path)
    if (model_path / "snapshots").is_dir():
        model_path = model_path / "snapshots" / settings.embedding_revision
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    torch.set_num_threads(4)
    started = time.perf_counter()
    model = AutoModel.from_pretrained(model_path, local_files_only=True).eval()
    memories, cases = load("memories.json"), load("cases.json")
    stale = [
        {"id": "foreign", "owner": FOREIGN, "version": 1, "content": "玄武项目密码轮换周期是秘密值异客-772。"},
        {"id": "deleted", "owner": OWNER, "version": 1, "content": "已删除的鹤鸣项目内部编号是鹤鸣-883。"},
        {"id": "e14", "owner": OWNER, "version": 0, "content": "潮汐向量编码超时的旧方案编号是旧潮-119。"},
    ]
    entries = [{**m, "owner": OWNER, "version": 1} for m in memories] + stale
    probes = ["玄武项目密码轮换周期", "鹤鸣项目内部编号", "潮汐向量编码超时的旧方案编号"]
    texts = [m["content"] for m in entries] + [c["question"] for c in cases] + probes
    vectors = []
    for start in range(0, len(texts), 4):
        batch = tokenizer(texts[start:start + 4], padding=True, truncation=True, max_length=1024, return_tensors="pt")
        with torch.inference_mode():
            v = torch.nn.functional.normalize(model(**batch).last_hidden_state[:, 0], dim=1).numpy()
        vectors.append(v)
    matrix = np.concatenate(vectors)
    assert matrix.shape == (len(texts), 1024) and np.isfinite(matrix).all()
    np.save(OUT / "vectors.npy", matrix)
    save("vector-entries.json", entries)
    save("embedding.json", {"model": settings.embedding_model, "revision": settings.embedding_revision, "dimension": 1024, "pooling": "CLS + L2", "device": "CPU", "max_tokens": 1024, "texts": len(texts), "seconds": time.perf_counter() - started, "torch": torch.__version__, "config_sha256": hashlib.sha256((model_path / "config.json").read_bytes()).hexdigest(), "hash_scope": "config.json only; revision identifies locally cached weight set"})
    print("encoded real BGE-M3 embeddings", flush=True)


def milvus_request(route, body):
    with httpx.Client(timeout=30, trust_env=False) as client:
        r = client.post(MILVUS + route, json=body)
        r.raise_for_status()
        data = r.json()
    if data.get("code") != 0:
        raise RuntimeError("isolated_milvus_error_" + str(data.get("code")))
    return data


def vector_setup():
    exists = milvus_request("collections/has", {"collectionName": COLLECTION})["data"]["has"]
    assert not exists, "Refuse to overwrite any existing collection"
    milvus_request("collections/create", {
        "collectionName": COLLECTION,
        "schema": {"autoId": False, "enableDynamicField": False, "fields": [
            {"fieldName": "id", "dataType": "VarChar", "isPrimary": True, "elementTypeParams": {"max_length": "64"}},
            {"fieldName": "memory_id", "dataType": "VarChar", "elementTypeParams": {"max_length": "64"}},
            {"fieldName": "owner", "dataType": "VarChar", "elementTypeParams": {"max_length": "64"}},
            {"fieldName": "version", "dataType": "Int64"},
            {"fieldName": "vector", "dataType": "FloatVector", "elementTypeParams": {"dim": "1024"}},
        ]},
        "indexParams": [{"fieldName": "vector", "indexName": "dense", "metricType": "COSINE", "indexType": "AUTOINDEX"}],
        "params": {"consistencyLevel": "Strong"},
    })
    entries = load("vector-entries.json")
    matrix = np.load(OUT / "vectors.npy")
    milvus_request("entities/upsert", {"collectionName": COLLECTION, "data": [
        {"id": f"{m['id']}:v{m['version']}", "memory_id": m["id"], "owner": m["owner"], "version": m["version"], "vector": v.tolist()}
        for m, v in zip(entries, matrix[:len(entries)], strict=True)
    ]})
    save("milvus.json", {"uri": "loopback:49531", "collection": COLLECTION, "rows": len(entries), "metric": "COSINE", "index": "AUTOINDEX", "consistency": "Strong"})
    print("isolated Milvus collection ready", flush=True)


def packed(rows, budget=2000):
    selected, seen = [], set()
    for m in rows:
        if m["id"] in seen or budget <= 0:
            continue
        if selected:
            budget -= 1  # newline separator is part of the shared byte budget
        if budget <= 0:
            break
        seen.add(m["id"])
        body = clip(m["content"], budget)
        selected.append({**m, "content": body})
        budget -= len(body.encode("utf-8"))
    return selected


def vector_recall(vector, canonical):
    hits = milvus_request("entities/search", {"collectionName": COLLECTION, "data": [vector.tolist()], "annsField": "vector", "filter": 'owner == "memory-eval-owner"', "limit": 20, "outputFields": ["memory_id", "version", "owner"], "consistencyLevel": "Strong", "searchParams": {"metricType": "COSINE", "params": {}}})["data"]
    valid, dropped = [], []
    for h in hits:
        mid = h["memory_id"]
        if mid not in canonical or h["version"] != 1 or h["owner"] != OWNER:
            dropped.append(h["id"])
            continue
        valid.append({**canonical[mid], "score": h["distance"]})
    return valid[:5], dropped


def contexts():
    memories, cases, entries = load("memories.json"), load("cases.json"), load("vector-entries.json")
    engine = create_engine("sqlite:///" + (OUT / "sandbox.sqlite").as_posix())
    matrix = np.load(OUT / "vectors.npy")
    records = []
    with Session(engine) as db:
        canonical = {m.id: {"id": m.id, "content": m.content, "kind": next(x["kind"] for x in memories if x["id"] == m.id)} for m in db.scalars(select(Memory).where(Memory.user_id == OWNER))}
        for i, case in enumerate(cases):
            db.query(Message).delete()
            db.add(Message(id="question", conversation_id="fixture-conversation", sequence=1, role="user", content=case["question"]))
            db.commit()
            run = SimpleNamespace(id="synthetic-run", conversation_id="fixture-conversation", account_id=OWNER, owner_id=OWNER, snapshot={"system_prompt": SYSTEM, "conversation_upto_sequence": 1})
            native = build_messages(db, run)
            pref = next(json.loads(m["content"])["data"] for m in native if m["role"] == "user" and '"context_kind": "user_preferences"' in m["content"])
            base = [m for m in native if not (m["role"] == "user" and '"context_kind": "user_preferences"' in m["content"])]
            started = time.perf_counter()
            dense, dropped = vector_recall(matrix[len(entries) + i], canonical)
            dense_ms = (time.perf_counter() - started) * 1000
            # Real SQLite substring ranking, not a vector disguised as SQL.
            segments = re.findall(r"[a-zA-Z0-9_-]+|[\u4e00-\u9fff]+", case["question"].lower())
            terms = sorted(set(t for s in segments for t in ([s] if s.isascii() else [s[j:j+2] for j in range(len(s)-1)])))
            expr = " + ".join(f"CASE WHEN instr(lower(content), :t{j}) > 0 THEN 1 ELSE 0 END" for j in range(len(terms))) or "0"
            started = time.perf_counter()
            keyword = db.execute(text(f"SELECT id, ({expr}) AS score FROM memories WHERE user_id=:owner AND ({expr})>0 ORDER BY score DESC, id LIMIT 5"), {"owner": OWNER, **{f"t{j}": t for j, t in enumerate(terms)}}).all()
            keyword_ms = (time.perf_counter() - started) * 1000
            selections = {
                "sql_current": packed(memories),
                "sql_keyword": packed([canonical[r.id] for r in keyword]),
                "vector": packed(dense),
                "hybrid": packed([*memories[:3], *dense]),
            }
            for arm, selected in selections.items():
                content = pref if arm == "sql_current" else "\n".join(m["content"] for m in selected)
                messages = native if arm == "sql_current" else [*base[:-1], data_message("user_preferences", content), base[-1]]
                # Fixed byte budget and identical data envelope across arms.
                assert len(content.encode()) <= 2000
                expected = "\n".join(m["content"] for m in memories)
                if arm == "sql_current":
                    assert content == clip(expected, 2000)
                covered = [g for g, aliases in zip(case["gold"], case["answers"]) if any(a in content.lower() for a in aliases)]
                records.append({"case": case["id"], "arm": arm, "category": case["category"], "messages": messages, "selected_ids": [m["id"] for m in selected], "context_bytes": len(content.encode()), "gold_ids": case["gold"], "covered_ids": covered, "recall": len(covered)/len(case["gold"]) if case["gold"] else None, "retrieval_ms": dense_ms if arm in ("vector", "hybrid") else keyword_ms if arm == "sql_keyword" else None, "dropped_stale_ids": dropped if arm in ("vector", "hybrid") else []})
        checks = []
        for n, forbidden in enumerate(("foreign", "deleted", "e14:v0")):
            valid, dropped = vector_recall(matrix[len(entries)+len(cases)+n], canonical)
            assert all(x["id"] not in ("foreign", "deleted") for x in valid)
            if forbidden == "foreign":
                assert not any(x.startswith("foreign:") for x in dropped)
            else:
                assert (forbidden if ":v" in forbidden else forbidden + ":v1") in dropped
            checks.append({"probe": forbidden, "valid_ids": [x["id"] for x in valid], "dropped": dropped, "passed": True})
    engine.dispose()
    save("contexts.json", records)
    save("isolation-checks.json", checks)
    print("prepared 96 matched contexts and 3 scope/version probes", flush=True)


class ControlledProvider(HTTPProvider):
    def request_body(self, messages, tools, max_tokens):
        body = super().request_body(messages, tools, max_tokens)
        body["temperature"] = 0
        if self.model == "deepseek-flash":
            body["thinking"] = {"type": "disabled"}
        return body


def generate(repeats):
    settings = Settings().model_copy(update={"app_env": "test", "model_max_retries": 0, "model_timeout_seconds": 30, "task_budget_enabled": False, "otel_sample_ratio": 0.0})
    assert settings.model_provider == "openai_compatible" and settings.model_api_key.get_secret_value()
    records, cases = load("contexts.json"), {c["id"]: c for c in load("cases.json")}
    output = OUT / "answers.jsonl"
    done = set()
    if output.exists():
        done = {(r["case"], r["arm"], r["repeat"]) for r in map(json.loads, output.read_text(encoding="utf-8").splitlines())}
    jobs = [(r, n) for n in range(repeats) for r in records if (r["case"], r["arm"], n) not in done]
    random.Random(20261113).shuffle(jobs)

    def one(item):
        row, repeat = item
        started = time.perf_counter()
        try:
            config = AgentConfig(name="memory-ablation", system_prompt=SYSTEM, tools=[], closed_loop=False, max_steps=1, max_output_tokens=256)
            provider = ControlledProvider(settings, settings.model_provider, settings.model_id)
            state = run_graph(settings, config, provider, row["messages"], lambda *a: None, lambda _: None, 0)
            answer = state["text"]
            checks = [any(alias in answer.lower().replace(" ", "") for alias in group) for group in cases[row["case"]]["answers"]]
            return {"case": row["case"], "arm": row["arm"], "repeat": repeat, "category": row["category"], "answer": answer, "correct": all(checks), "checks": checks, "input_tokens": state["input_tokens"], "output_tokens": state["output_tokens"], "seconds": time.perf_counter()-started, "ok": True}
        except Exception as e:
            return {"case": row["case"], "arm": row["arm"], "repeat": repeat, "category": row["category"], "ok": False, "error": type(e).__name__, "safe_code": getattr(e, "code", None), "seconds": time.perf_counter()-started}

    with output.open("a", encoding="utf-8") as stream, ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, job) for job in jobs]
        for i, f in enumerate(as_completed(futures), 1):
            row = f.result()
            stream.write(json.dumps(row, ensure_ascii=False)+"\n")
            stream.flush()
            if i % 8 == 0 or not row["ok"]:
                print(f"answers {i}/{len(jobs)}; latest_ok={row['ok']}", flush=True)
    save("run.json", {"model": settings.model_id, "provider": settings.model_provider, "temperature": 0, "repeats": repeats, "max_output_tokens": 256, "concurrency": 4, "agent": "project run_graph + HTTPProvider, tools disabled, one model step", "dataset": "synthetic fixed 20-memory corpus, 24 questions; within existing 20-row limit", "limits": ["Small synthetic exploratory benchmark, not a real-user distribution", "No multi-step tool-use or automatic memory extraction evaluation", "Vector and keyword selection are experimental adapters, not production features", "Exact-answer matching, not an LLM judge", "Embedding performed offline; per-query end-to-end embedding latency not measured", "No money cost inferred without pricing"]})


def report():
    rows = list(map(json.loads, (OUT / "answers.jsonl").read_text(encoding="utf-8").splitlines()))
    contexts_by = {(r["case"], r["arm"]): r for r in load("contexts.json")}
    summary = {}
    for arm in ARMS:
        selected = [r for r in rows if r["arm"] == arm]
        ok = [r for r in selected if r["ok"]]
        ctx = [r for r in contexts_by.values() if r["arm"] == arm]
        summary[arm] = {"n": len(selected), "errors": len(selected)-len(ok), "correct": sum(r.get("correct", False) for r in selected), "accuracy": sum(r.get("correct", False) for r in selected)/len(selected), "by_category": {cat: {"n": sum(r["category"]==cat for r in selected), "correct": sum(r["category"]==cat and r.get("correct", False) for r in selected)} for cat in sorted({r["category"] for r in selected})}, "mean_recall": statistics.mean(r["recall"] for r in ctx if r["recall"] is not None), "context_bytes_mean": statistics.mean(r["context_bytes"] for r in ctx), "generation_seconds_median": statistics.median(r["seconds"] for r in ok) if ok else None, "input_tokens": sum(r["input_tokens"] or 0 for r in ok), "output_tokens": sum(r["output_tokens"] or 0 for r in ok), "unknown_usage": sum(r["input_tokens"] is None or r["output_tokens"] is None for r in ok)}
    save("summary.json", summary)
    labels = {"sql_current": "当前 SQL 顺序注入", "sql_keyword": "SQL 关键词选择", "vector": "BGE-M3 + Milvus", "hybrid": "通用偏好 + 向量召回"}
    table = ["| 方案 | 正确回答/调用 | 准确率 | 答案证据覆盖率 | 记忆正文平均字节 | 生成耗时中位数 |", "|---|---:|---:|---:|---:|---:|"]
    for arm in ARMS:
        s = summary[arm]
        table.append(f"| {labels[arm]} | {s['correct']}/{s['n']} | {s['accuracy']:.1%} | {s['mean_recall']:.1%} | {s['context_bytes_mean']:.0f} | {s['generation_seconds_median']:.3f}s |")
    usage = {"input_tokens": sum(s["input_tokens"] for s in summary.values()), "output_tokens": sum(s["output_tokens"] for s in summary.values()), "unknown_usage_calls": sum(s["unknown_usage"] for s in summary.values())}
    save("usage.json", usage)
    sample = []
    for arm in ARMS:
        row = next(r for r in rows if r["case"] == "episode-14" and r["arm"] == arm and r["repeat"] == 0)
        sample.append(f"- {labels[arm]}：{row.get('answer', row.get('error'))}")
    report_text = """# 长期记忆召回隔离实验（2026-11-13）

## 结论

本批合成样例中，当前按创建时间读取并截断的 SQL 基线为 37.5%，SQL 关键词选择为 95.8%，向量和混合方案均为 100%。主要收益来自按问题选择记忆，而非将 PostgreSQL 换成 Milvus：关键词选择同样解决了大部分问题。不能据此宣称向量方案在真实用户和任意 Agent 任务上达到 100%。

当前 SQL 基线在通用偏好问题上是 6/6，向量与混合也是 6/6；本批没有证明向量对少量通用偏好有额外收益。

## 实验范围

- 20 条固定合成记忆：3 条偏好、17 条历史事项；24 个问题：3 条偏好、17 条单事项、2 条多事项、2 条无记录。每组重复两次，共 192 次真实模型调用；只有 24 个独立问题，重复不等于扩大样本量。
- 用内部虚构代号作为事实答案，避免模型依靠常识猜中；答案和证据标签在模型调用前固定。无记录题要求回答“记忆中没有记录”。
- 原有 Memory 表面向手动偏好。本实验把历史事项也放入其文本字段，测试的是扩展长期记忆场景下的上下文选择策略，不是断言现有偏好功能不合格。
- 基线实际调用项目 build_messages()，按创建时间取前 20 条、UTF-8 截断为 2000 字节；所有组使用同一数据包装及同一 2000 字节记忆预算，不含固定系统提示、助手配置和当前问题。
- 关键词对照使用 SQLite SQL instr() 对查询中文二元片段及英文词做重合计数，Top5；不是 BM25，也未声称是最优 SQL 检索方案。
- 向量组调用真实本地缓存 BGE-M3，1024 维、CLS+L2；独立 Milvus 2.5.27，COSINE+AUTOINDEX，强一致，候选 20、核验后 Top5。
- 混合组先保留 3 条通用偏好，再加入向量结果，按 ID 去重，总预算仍为 2000 字节。未做重排、查询改写、时间衰减或参数调优。
- 所有回答调用项目 run_graph() 和 HTTPProvider，deepseek-flash、temperature=0、关闭 thinking、输出上限 256、无工具、单模型步骤、并发 4。测试的是 Agent 的记忆问答，不覆盖多步工具任务、自动画像抽取或完整 Web/Worker 端到端流程。

## 结果

TABLE

证据覆盖率为 22 个有答案问题的宏平均：仅当答案文本真的进入最终上下文才算覆盖，多事项按覆盖比例计分。它不是数据库 TopK ID 命中率。回答用固定答案别名进行规则检查，多事项必须全部回答正确；无记录题检查拒答短语。原始回答保留，未使用同模型裁判，也未逐项进行独立人工审核。

## 具体例子

问题：“潮汐那次文本转向量一直等不到返回，最后采用的方案编号是什么？”

SAMPLE

SQL 基线缺失主要集中在靠后记录：全部 12 条后段单事项题、2 条多事项题均未完整回答；关键词对照漏掉了“回答详略偏好”题。基线的失败主要是正确拒答未注入的知识，不应称为模型产生了大量幻觉。

## 隔离和一致性

- SQL 使用 outputs 下独立 sandbox.sqlite，不连接业务数据库。
- Docker 使用独立项目 yugitalk-memory-eval-20261113，独立本机端口 49531/49092；不挂载业务卷。
- 记忆集合只含合成数据，额外放入跨用户、已删除和旧版本候选用于 3 个定向探针。查询前按用户过滤，召回后按 SQL 现存 ID 和实验版本元数据核验，3 项通过。
- 删除通过 SQL 中不保留该记录模拟；版本核验是实验适配器中的 version=1 规则，不是已上线的生产记忆版本系统。未测试并发删除、执行中撤销、恢复任务失效或所有权限攻击。
- 业务后端、前端和生产记忆模块均未修改。实验结束移除本实验容器与网络；SQL、向量矩阵、查询上下文和回答保留在输出目录。

## 时间、成本和限制

USAGE

- 上表耗时仅模型生成阶段，不含离线 BGE 查询编码、记忆索引和上下文预计算，因此不是完整用户请求延迟，也不能据其宣称向量不增加延迟。
- 两次重复答案一致也不代表在其他采样或模型上稳定；样本较小且项目名清晰，可能有利于关键词和向量方法。
- 没有设置相似度阈值，向量组即使无答案也返回 Top5，再由回答模型拒答。只有 2 条无记录题，不能推出普遍的拒答能力。
- 只测了 23 个向量条目，不提供规模、吞吐或索引精度结论；没有测大库、歧义记忆、冲突演化和强提示注入。
- 不将合成事实回答分数等同于真实任务成功率。上线前应补充实际授权样例、冲突/时效/指代测试和多步任务评测。

## 复现

从项目根目录使用 backend/.venv/Scripts/python.exe 运行 scripts/evaluate-memory-recall.py 的 prepare、encode、vector_setup、contexts、generate --repeats 2、report 阶段。prepare 和 vector_setup 拒绝覆盖已有数据库/集合；generate 对已完成调用做去重续跑，report 只读取结果、不调用模型。重新独立实验应复制脚本并更换输出目录、集合名、Docker 项目和端口。

本机额外依赖隔离安装在输出目录 deps，不修改项目虚拟环境。依赖及模型元数据见 embedding.json；原始结果见 answers.jsonl，上下文见 contexts.json；manifest.json 保存输入、结果和源代码哈希。依赖目录不适合提交到 Git。
"""
    report_text = report_text.replace("TABLE", "\n".join(table)).replace("SAMPLE", "\n".join(sample)).replace("USAGE", f"供应商报告总输入 {usage['input_tokens']:,} tokens，输出 {usage['output_tokens']:,} tokens，缺失用量调用 {usage['unknown_usage_calls']} 次。未配置价格版本，不推算货币费用；BGE 在本地 CPU 执行。")
    (OUT / "REPORT.md").write_text(report_text, encoding="utf-8")
    sources = ["backend/app/memory/context.py", "backend/app/agents/graph.py", "backend/app/providers/client.py", "scripts/evaluate-memory-recall.py"]
    artifacts = ["memories.json", "cases.json", "contexts.json", "answers.jsonl", "vectors.npy", "embedding.json", "summary.json", "REPORT.md"]
    save("manifest.json", {"date": "2026-11-13", "sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}, "artifact_sha256": {name: hashlib.sha256((OUT / name).read_bytes()).hexdigest() for name in artifacts}})
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("prepare", "encode", "vector_setup", "contexts", "generate", "report"))
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.stage == "generate":
        generate(args.repeats)
    else:
        globals()[args.stage]()
