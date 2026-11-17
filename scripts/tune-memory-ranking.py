"""隔离的合成记忆排序实验：真实本地 BGE-M3，按主题划分调优/留出验证。

从根目录运行 backend/.venv/Scripts/python.exe scripts/tune-memory-ranking.py。
只写本实验目录，不读取业务记忆、不连接业务数据库、不调用聊天模型。
"""

import hashlib
import json
import math
import random
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 复用已安装的隔离推理依赖，不安装或修改项目依赖。
sys.path.insert(0, str(ROOT / "outputs/memory-recall-eval-20261113/deps"))
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.memory.models import Memory
from app.memory.ranking import rank_memories, select_memories

OUT = ROOT / "outputs/memory-ranking-tuning-20261113"
REFERENCE = datetime(2026, 11, 13, tzinfo=UTC)
# 划分在计算向量、搜索权重前固定；同一主题的四种意图属于同一集合。
DOMAINS = [
    ("tune", "星港", "数据库迁移", "PostgreSQL", "MySQL", "备份并验证回滚脚本"),
    ("tune", "松林", "接口重试", "7秒", "2秒", "区分幂等请求和写入请求"),
    ("tune", "白鹭", "图片上传", "13MB", "5MB", "检查文件真实类型和尺寸"),
    ("tune", "远帆", "服务部署", "西区三号机房", "东区一号机房", "保留上一版镜像用于回滚"),
    ("tune", "流萤", "知识检索", "29个候选", "10个候选", "回答必须保留证据来源"),
    ("tune", "云桥", "缓存刷新", "19分钟", "5分钟", "缓存未命中时读取权威数据库"),
    ("tune", "青禾", "邮件通知", "松果组", "青苗组", "发送前检查接收范围"),
    ("tune", "竹影", "文件导出", "PDF", "HTML", "导出结果应附生成时间"),
    ("tune", "雪松", "日志保留", "37天", "7天", "不把凭证写入日志"),
    ("tune", "海棠", "备份恢复", "每周六", "每周一", "恢复后核对记录数和校验和"),
    ("holdout", "琥珀", "音频转写", "普通话模型", "多语模型", "保留说话人和时间戳"),
    ("holdout", "晨雾", "订单导入", "每批43条", "每批12条", "重复订单不得重复创建"),
    ("holdout", "银杉", "报表生成", "每月第三天", "每月第一天", "金额统一显示人民币"),
    ("holdout", "潮汐", "告警推送", "值班二组", "全部成员", "按故障严重程度分级"),
    ("holdout", "风铃", "视频处理", "1080p", "720p", "保留原始帧率和音轨"),
    ("holdout", "霜叶", "搜索索引", "每天凌晨三点", "每小时", "切换前核对索引文档数量"),
]


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fixture():
    """等级依据任务用途标注，绝不通过待搜索的打分公式生成标签。"""
    cases = []
    for index, (split, project, topic, latest, previous, guidance) in enumerate(DOMAINS):
        questions = [
            f"{project}项目的{topic}，我目前最终确认的设置是什么？请按我的表达偏好回答。",
            f"帮我为{project}的{topic}写实施示例，遵守我对代码和说明的长期偏好。",
            f"{project}的{topic}最近改成了什么？不要沿用之前的旧设置。",
            f"回顾{project}的{topic}，我半年前首次确认的设置是什么？问的是历史值。",
        ]
        for mode, question in enumerate(questions):
            rows = []

            def add(key, content, importance, age, grade, rows=rows):
                rows.append({"id": key, "content": content, "importance": importance,
                             "age_days": age, "grade": grade})

            add("latest", f"{project}项目{topic}目前最终确认的设置为{latest}。这是本月更新后的决定，"
                "只对这个项目有效；复述时保留具体名称和数值，不套用行业默认值。",
                2 + index % 3, 2 + index % 8, 0 if mode == 3 else 3)
            add("historical", f"{project}项目{topic}半年前首次确认的设置为{previous}。这是历史记录，"
                "仅在回顾当时决策时使用，不代表目前仍然采用这个设置。",
                2 + index % 3, 190 + index, 3 if mode == 3 else 0)
            pref = ("代码示例没有指定语言时优先 Python，并附简短中文解释。"
                    if mode == 1 else "默认使用中文回答，先给结论，保持简洁，避免大段铺垫。")
            add("preference", "我的长期表达偏好：" + pref + "这一偏好适用于各项目，除非本轮明确另有要求。",
                5, 300 + index * 11, 2)
            add("support", f"{project}项目{topic}的实施约束：{guidance}。"
                "这条约束针对实施和操作，不包含最终配置值；只在相关任务中应用。",
                3, 50 + index, 2 if mode == 1 else 0)
            add("near", f"我认为{topic}是重要学习主题，今后有空时想读一本介绍它的书。"
                "这是个人学习兴趣，不是任何项目的配置决定，也不构成实施约束。",
                5, 0, 0)
            for offset in range(15):
                _, other, other_topic, value, _, _ = DOMAINS[(index + offset + 1) % len(DOMAINS)]
                add(f"other-{offset:02}", f"{other}项目{other_topic}的设置为{value}。"
                    f"这条记录只适用于{other}，不能用于推断{project}的设置。"
                    "这是独立事项，具体名称与数值只在讨论所属项目时使用，不跨项目套用。",
                    1 + (index + offset) % 5, (index * 37 + offset * 29) % 500, 0)
            random.Random(20261113 + index * 10 + mode).shuffle(rows)
            cases.append({"id": f"{index:02}-{mode}", "split": split, "topic": project,
                          "intent": mode, "query": question, "candidates": rows})
    return cases


def encode(cases, settings):
    """与项目本地服务一致：1024 tokens、CLS pooling、L2 归一化。"""
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    texts = list(dict.fromkeys(text for case in cases for text in
                              [case["query"], *(r["content"] for r in case["candidates"])]))
    digest = hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()
    model_path = Path(settings.bge_model_path)
    if (model_path / "snapshots").is_dir():
        model_path = model_path / "snapshots" / settings.embedding_revision
    if (OUT / "embedding.json").exists():
        metadata = json.loads((OUT / "embedding.json").read_text(encoding="utf-8"))
        assert metadata["texts_sha256"] == digest
        assert metadata["revision"] == settings.embedding_revision
        return dict(zip(texts, np.load(OUT / "vectors.npy"), strict=True))
    torch.set_num_threads(4)
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = AutoModel.from_pretrained(model_path, local_files_only=True).eval()
    started = time.perf_counter()
    vectors = []
    for start in range(0, len(texts), 8):
        batch = tokenizer(texts[start:start + 8], padding=True, truncation=True,
                          max_length=1024, return_tensors="pt")
        with torch.inference_mode():
            vector = torch.nn.functional.normalize(model(**batch).last_hidden_state[:, 0], dim=1)
        vectors.append(vector.numpy())
    matrix = np.concatenate(vectors)
    assert matrix.shape == (len(texts), 1024) and np.isfinite(matrix).all()
    np.save(OUT / "vectors.npy", matrix)
    save("embedding.json", {"model": settings.embedding_model, "revision": settings.embedding_revision,
                           "pooling": "CLS + L2", "max_tokens": 1024, "device": "CPU",
                           "texts_sha256": digest, "texts": len(texts),
                           "seconds": time.perf_counter() - started,
                           "config_sha256": hashlib.sha256(
                               (model_path / "config.json").read_bytes()).hexdigest()})
    print(f"Encoded {len(texts)} texts with real BGE-M3", flush=True)
    return dict(zip(texts, matrix, strict=True))


def features(cases, vectors):
    prepared = []
    feature_records = []
    for case in cases:
        memories = [Memory(id=r["id"], content=r["content"], importance=r["importance"],
                           created_at=REFERENCE - timedelta(days=r["age_days"]))
                    for r in case["candidates"]]
        relevance = [float(vectors[case["query"]] @ vectors[m.content]) for m in memories]
        prepared.append((case, memories, relevance))
        feature_records.append({"case": case["id"], "relevance": dict(zip(
            [m.id for m in memories], relevance, strict=True))})
    save("features.json", feature_records)
    return prepared


def evaluate(prepared, weights, half_life=90, baseline=None):
    results = []
    for case, memories, relevance in prepared:
        ranked = rank_memories(memories, relevance, REFERENCE, weights, half_life)
        if baseline == "oldest":
            ranked.sort(key=lambda r: (r.memory.created_at, r.memory.id))
        selected = select_memories(ranked)
        grades = {r["id"]: r["grade"] for r in case["candidates"]}
        dcg = sum((2 ** grades[r.memory.id] - 1) / math.log2(i + 2)
                  for i, r in enumerate(ranked[:5]))
        ideal = sum((2 ** grade - 1) / math.log2(i + 2)
                    for i, grade in enumerate(sorted(grades.values(), reverse=True)[:5]))
        required = {key for key, grade in grades.items() if grade >= 2}
        selected_ids = {r.memory.id for r in selected}
        used = len("\n".join(r.memory.content for r in selected).encode())
        irrelevant_bytes = sum(len(r.memory.content.encode()) for r in selected
                               if grades[r.memory.id] == 0)
        results.append({"case": case["id"], "ndcg5": dcg / ideal,
                        "recall": len(required & selected_ids) / len(required),
                        "irrelevant_byte_ratio": irrelevant_bytes / used if used else 0,
                        "selected_ids": [r.memory.id for r in selected], "context_bytes": used,
                        "top5": [r.memory.id for r in ranked[:5]]})
    return {"ndcg5": statistics.mean(r["ndcg5"] for r in results),
            "recall": statistics.mean(r["recall"] for r in results),
            "irrelevant_byte_ratio": statistics.mean(r["irrelevant_byte_ratio"] for r in results),
            "cases": results}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cases = fixture()
    # 标注与划分先落盘，之后才编码和搜索；不依据留出结果调整权重。
    save("dataset.json", cases)
    settings = Settings()
    prepared = features(cases, encode(cases, settings))
    tuning = [r for r in prepared if r[0]["split"] == "tune"]
    holdout = [r for r in prepared if r[0]["split"] == "holdout"]
    trials = []
    for r in range(21):
        for i in range(21 - r):
            weights = (r / 20, i / 20, (20 - r - i) / 20)
            metrics = evaluate(tuning, weights)
            trials.append({"weights": weights, **{k: v for k, v in metrics.items() if k != "cases"}})
    # 事先规定：NDCG@5 优先，预算内召回其次，无关字节占比再次；最后偏向相关性。
    trials.sort(key=lambda t: (-t["ndcg5"], -t["recall"], t["irrelevant_byte_ratio"],
                               -t["weights"][0], t["weights"][2]))
    ndcg_winner = trials[0]["weights"]
    # 探索性第二阶段：最高 NDCG 取消了重要性；保留三项作用，并用小幅排序损失换召回。
    # 此规则在首轮实验后提出，选择仍仅访问调优指标，不宣称事先注册的唯一选择规则。
    practical = [t for t in trials if min(t["weights"]) > 0
                 and t["ndcg5"] >= trials[0]["ndcg5"] - 0.01]
    practical.sort(key=lambda t: (-t["recall"], t["irrelevant_byte_ratio"], -t["ndcg5"]))
    winner = practical[0]["weights"]
    save("trials.json", trials)
    comparisons = {}
    arms = {"selected": winner, "ndcg_best": ndcg_winner,
            "equal": (1 / 3,) * 3, "relevance_only": (1, 0, 0),
            "importance_only": (0, 1, 0), "recency_only": (0, 0, 1), "oldest": (0, 0, 1)}
    for name, weights in arms.items():
        comparisons[name] = {"weights": weights, "tune": evaluate(tuning, weights,
            baseline="oldest" if name == "oldest" else None), "holdout": evaluate(holdout, weights,
            baseline="oldest" if name == "oldest" else None)}
    # 固定已选权重后，只做时间半衰期敏感度报告，不据留出集再次选参数。
    sensitivity = {str(days): {split: {k: v for k, v in evaluate(rows, winner, days).items()
                                     if k != "cases"}
                             for split, rows in (("tune", tuning), ("holdout", holdout))}
                   for days in (30, 90, 180)}
    save("results.json", {"selected_weights": winner, "ndcg_best_weights": ndcg_winner,
                          "selection": "exploratory positive weights within 0.01 tune NDCG of best; "
                          "maximize tune recall, then minimize irrelevant bytes, then NDCG",
                          "half_life_days": 90,
                          "trials": len(trials), "tune_cases": len(tuning),
                          "holdout_cases": len(holdout), "comparisons": comparisons,
                          "half_life_sensitivity": sensitivity})
    lines = ["# 候选记忆加权排序实验", "", "日期：2026-11-13", "",
             "## 数据与方法", "",
             "64 个合成问题，每题 20 条候选；由实现者按任务用途标注 0/2/3 级，未经独立人工复核。",
             "10 个主题 / 40 题用于调优，另 6 个主题 / 24 题留出验证；同主题不跨集合。",
             "各主题共享问题模板，留出集不代表真实用户分布或独立语言表达分布。",
             "候选干扰记忆可来自其他主题，因此不是候选文档完全隔离的冷启动测试。",
             "标签和切分在向量计算及搜索前固定。标签不通过加权公式生成。",
             "使用真实本地 BGE-M3（CLS + L2），无 mock 向量；不读取业务记忆或调用聊天模型。",
             "候选覆盖直接事实、历史/最新值、全局偏好、实施约束、高重要性干扰及跨项目干扰。",
             "", "## 参数选择", "",
             f"网格步长 0.05，共 {len(trials)} 组非负且和为 1 的权重；半衰期预先固定 90 天。",
             f"第一阶段只按调优 NDCG@5 优先搜索，最优为 {ndcg_winner}，重要性系数为零。",
             "第二阶段为实用取舍：要求三项权重为正，调优 NDCG 距最优不超过 0.01，",
             "再优先预算内必要记忆召回率，其次无关字节占比和 NDCG。",
             "此规则是在查看首轮结果后提出，属于探索性选择，不能当作预注册结论。",
             f"最终选择（相关性、重要性、时间）：{winner}；未按留出集指标挑选第二阶段权重。",
             "第一阶段原报告及结果保留为 REPORT.ndcg-only.md / results.ndcg-only.json。", "",
             "## 比较结果", "",
             "| 方法 | 调优 NDCG@5 | 留出 NDCG@5 | 留出必要记忆召回率 | 留出无关字节占比 |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for name, result in comparisons.items():
        train, test = result["tune"], result["holdout"]
        lines.append(f"| {name} | {train['ndcg5']:.4f} | {test['ndcg5']:.4f} | "
                     f"{test['recall']:.2%} | {test['irrelevant_byte_ratio']:.2%} |")
    lines.extend(["", "oldest 对照只隔离排序因素：与其他组共用完整条目预算策略，",
                  "不声称复现旧版整段截断行为。预算为正文加换行 2000 UTF-8 字节；不设相关性阈值。",
                  "必要记忆定义为标注等级至少 2；无关字节占比的分子仅统计无关正文，分母含换行。",
                  "", "## 限制与复现", "",
                  "这是排序与预算选择实验，不测最终回答准确率、用户满意度或在线嵌入延迟。",
                  "重要等级和时间均为合成设定；所有历史值/最新值均为虚构记录。",
                  "权重仅是在这个数据和候选来源上的较优值，需真实标注数据进一步校准。",
                  "半衰期 30/90/180 天敏感度见 results.json，不使用留出指标反向调参。",
                  "完整数据、特征、231 组调优结果、逐题留出结果和向量均保留在本目录。",
                  "", "运行：`backend/.venv/Scripts/python.exe scripts/tune-memory-ranking.py`。",
                  "依赖复用 outputs/memory-recall-eval-20261113/deps；只加载本地模型，不下载。"])
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    sources = ["backend/app/memory/ranking.py", "scripts/tune-memory-ranking.py"]
    save("manifest.json", {"sources": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                                      for p in sources}, "artifacts": {
        name: hashlib.sha256((OUT / name).read_bytes()).hexdigest() for name in
        ("dataset.json", "features.json", "trials.json", "results.json", "embedding.json",
         "vectors.npy", "REPORT.md")}})
    print(json.dumps({"weights": winner, "holdout": {k: v for k, v in
          comparisons["selected"]["holdout"].items() if k != "cases"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
