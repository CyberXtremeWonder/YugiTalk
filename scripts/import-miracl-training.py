"""从本地 Arrow 缓存分包导入 MIRACL 训练或验证段落，保留原始 ID 与评测证据。"""

import argparse
import hashlib
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
from datasets import Dataset
from dotenv import dotenv_values


def save(path: Path, value: dict | list) -> None:
    """保存可恢复清单及来源映射，文件中不写入账号和密码。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare(
    rows: list[dict], root: Path, batch_size: int = 100, split: str = "train"
) -> dict:
    """每包包含多份有原始 ID 的段落，同时保留正负例，不导入问题文本。"""
    passages = {}
    for row in rows:
        for kind in ("positive_passages", "negative_passages"):
            for passage in row[kind]:
                if (
                    passage["docid"] in passages
                    and passages[passage["docid"]] != passage
                ):
                    raise ValueError("相同 MIRACL 段落 ID 对应不同内容")
                passages[passage["docid"]] = passage
    files = root / "documents"
    files.mkdir(parents=True, exist_ok=True)
    items = sorted(passages.items())
    mapping, documents = {}, []
    for start in range(0, len(items), batch_size):
        filename = f"miracl-zh-{split}-{start // batch_size + 1:03d}.md"
        selected = items[start : start + batch_size]
        content = "\n\n".join(
            f"## {passage['title']}\nMIRACL_DOCID: {key}\n{passage['text'].strip()}"
            for key, passage in selected
        )
        raw = content.encode("utf-8")
        (files / filename).write_bytes(raw)
        documents.append(
            {
                "filename": filename,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "passages": len(selected),
                "characters": len(content),
            }
        )
        mapping.update({key: filename for key, _ in selected})
    if len(documents) > 200:
        raise ValueError("分包超过知识库 200 文件上限，请增加每包段落数")
    cases = [
        {
            "question": row["query"],
            "query_id": row["query_id"],
            "positive_passage_ids": [
                item["docid"] for item in row["positive_passages"]
            ],
            "relevant": [
                {"filename": mapping[item["docid"]], "quote": item["text"].strip()}
                for item in row["positive_passages"]
            ],
        }
        for row in rows
    ]
    for case, row in zip(cases, rows, strict=True):
        identities = Counter(
            (item["filename"], item["quote"]) for item in case["relevant"]
        )
        for target, passage in zip(
            case["relevant"], row["positive_passages"], strict=True
        ):
            if identities[(target["filename"], target["quote"])] > 1:
                # 同一文件中的同文不同原始段落不能丢掉标注，用已有段落标记消歧。
                target["quote"] = f"MIRACL_DOCID: {passage['docid']}\n{target['quote']}"
    if any(not case["relevant"] for case in cases):
        raise ValueError("数据集中有缺少正例的问题，须显式处理后再评测")
    save(root / "cases.json", {"cases": cases})
    save(root / "passage-files.json", mapping)
    return {"questions": len(cases), "passages": len(passages), "documents": documents}


def all_documents(client: httpx.Client, base: str) -> list[dict]:
    """遍历指定知识库的分页文档列表，不读取其他知识库。"""
    result, seen = [], set()
    cursor = None
    while True:
        response = client.get(
            f"/api/v1/knowledge-bases/{base}/documents",
            params={"limit": 100, **({"cursor": cursor} if cursor else {})},
        )
        response.raise_for_status()
        page = response.json()
        result.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            return result
        if cursor in seen:
            raise ValueError("资料分页游标重复")
        seen.add(cursor)


def main() -> None:
    """读取已下载的指定划分，登录后创建独立知识库并等待所有资料就绪。"""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--arrow", type=Path, required=True)
    parser.add_argument("--split", choices=["train", "dev"], default="train")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--chunk-strategy", choices=["fixed", "markdown_sections"], default="fixed"
    )
    args = parser.parse_args()
    if args.output is None:
        args.output = root / f"outputs/miracl-zh-{args.split}"
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "import.json"
    rows = list(Dataset.from_file(str(args.arrow)))
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else prepare(rows, args.output, split=args.split)
    )
    fingerprint = hashlib.sha256(args.arrow.read_bytes()).hexdigest()
    if manifest.get("arrow_sha256") not in (None, fingerprint):
        raise ValueError("本地缓存与既有导入清单不一致，禁止覆盖")
    manifest["arrow_sha256"] = fingerprint
    dataset = f"miracl/miracl zh {args.split}"
    if manifest.get("dataset") not in (None, dataset):
        raise ValueError("数据划分与既有清单不同，请使用新的输出目录")
    manifest["dataset"] = dataset
    if manifest.get("chunk_strategy", "fixed") != args.chunk_strategy and manifest.get(
        "knowledge_base_id"
    ):
        raise ValueError("既有知识库的分块策略不同，请使用新的输出目录")
    manifest["chunk_strategy"] = args.chunk_strategy
    save(manifest_path, manifest)
    print(
        f"准备完成：{manifest['questions']} 题，{manifest['passages']} 个段落，"
        f"{len(manifest['documents'])} 个文件",
        flush=True,
    )
    if args.prepare_only:
        return
    values = dotenv_values(root / "backend/.env.rag-test")
    api = values["RAG_TEST_API_URL"]
    origin = values["RAG_TEST_ORIGIN"]
    with httpx.Client(base_url=api, timeout=90, trust_env=False) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": values["RAG_TEST_ACCOUNT"],
                "password": values["RAG_TEST_PASSWORD"],
            },
            headers={"Origin": origin},
        )
        response.raise_for_status()
        identity = response.json()
        if manifest.get("owner_id") not in (None, identity["user"]["id"]):
            raise ValueError("账号与已有导入清单不一致")
        manifest["owner_id"] = identity["user"]["id"]
        headers = {"Origin": origin, "X-CSRF-Token": identity["csrf_token"]}
        if not manifest.get("knowledge_base_id"):
            name = (
                "MIRACL 中文验证集固定方案 "
                if args.split == "dev"
                else "MIRACL 中文训练集段落边界对照 "
                if args.chunk_strategy == "markdown_sections"
                else "MIRACL 中文训练集检索评测 "
            ) + datetime.now().astimezone().strftime("%Y%m%d")
            response = client.post(
                "/api/v1/me/knowledge-bases", json={"name": name}, headers=headers
            )
            response.raise_for_status()
            manifest.update(
                knowledge_base_id=response.json()["id"], knowledge_base_name=name
            )
            save(manifest_path, manifest)
        base = manifest["knowledge_base_id"]
        for number, document in enumerate(manifest["documents"], 1):
            if document.get("id"):
                continue
            response = client.post(
                f"/api/v1/knowledge-bases/{base}/documents",
                params={
                    "filename": document["filename"],
                    "chunk_strategy": args.chunk_strategy,
                },
                content=(args.output / "documents" / document["filename"]).read_bytes(),
                headers={**headers, "Content-Type": "application/octet-stream"},
            )
            response.raise_for_status()
            document["id"] = response.json()["id"]
            save(manifest_path, manifest)
            print(
                f"上传 {number}/{len(manifest['documents'])}：{document['filename']}",
                flush=True,
            )
        deadline = time.monotonic() + 3600
        expected = {item["id"] for item in manifest["documents"]}
        while True:
            documents = {item["id"]: item for item in all_documents(client, base)}
            counts = Counter(
                documents[key]["status"] for key in expected if key in documents
            )
            print(
                "索引状态：" + json.dumps(dict(counts), ensure_ascii=False), flush=True
            )
            manifest["index_status"] = dict(counts)
            save(manifest_path, manifest)
            failed = [
                documents[key]
                for key in expected
                if key in documents and documents[key]["status"] == "failed"
            ]
            if failed:
                manifest["failed"] = [
                    {"id": item["id"], "error_code": item["error_code"]}
                    for item in failed
                ]
                save(manifest_path, manifest)
                raise RuntimeError("资料索引失败，清单已保留失败原因")
            if counts.get("ready") == len(expected):
                break
            if time.monotonic() > deadline:
                raise TimeoutError("等待索引超时，清单已保存，可继续运行")
            time.sleep(15)
        case_path = args.output / "cases.json"
        dataset = json.loads(case_path.read_text(encoding="utf-8"))
        by_filename = {item["filename"]: item["id"] for item in manifest["documents"]}
        for case in dataset["cases"]:
            for target in case["relevant"]:
                target["document_id"] = by_filename[target["filename"]]
        save(case_path, dataset)
        manifest["completed_at"] = datetime.now().astimezone().isoformat()
        save(manifest_path, manifest)
        print(f"导入完成，知识库 ID：{base}", flush=True)


if __name__ == "__main__":
    main()
