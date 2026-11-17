"""通过正常登录与知识库 API 导入用户提供的完整萌娘百科 JSONL。"""

import argparse
import json
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=ROOT / "outputs/acg_simpleqa/import.json")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    config = dotenv_values(ROOT / "backend/.env.rag-test")
    origin = config["RAG_TEST_ORIGIN"]
    with httpx.Client(
        base_url=config["RAG_TEST_API_URL"], trust_env=False,
        timeout=httpx.Timeout(1800, connect=20, write=300),
    ) as client:
        identity = client.post(
            "/api/v1/auth/login", headers={"Origin": origin},
            json={"email": config["RAG_TEST_ACCOUNT"], "password": config["RAG_TEST_PASSWORD"]},
        ).raise_for_status().json()
        headers = {"Origin": origin, "X-CSRF-Token": identity["csrf_token"]}
        bases = client.get("/api/v1/me/knowledge-bases").raise_for_status().json()["items"]
        base = next((row for row in bases if row["name"] == "ACG知识库"), None)
        if base is None:
            base = client.post(
                "/api/v1/me/knowledge-bases", json={"name": "ACG知识库"}, headers=headers,
            ).raise_for_status().json()
        print(json.dumps({"stage": "uploading", "base_id": base["id"], "bytes": source.stat().st_size}), flush=True)
        with source.open("rb") as content:
            document = client.post(
                f"/api/v1/knowledge-bases/{base['id']}/documents",
                params={"filename": source.name, "chunk_strategy": "auto"},
                content=content,
                headers={**headers, "Content-Type": "application/octet-stream",
                         "Content-Length": str(source.stat().st_size)},
            ).raise_for_status().json()
        state = {
            "source": str(source), "knowledge_base_id": base["id"], "document_id": document["id"],
            "agent_version_id": config["RAG_TEST_AGENT_VERSION_ID"], "upload": document,
        }
        args.state.parent.mkdir(parents=True, exist_ok=True)
        args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"stage": "uploaded", "document_id": document["id"]}), flush=True)
        previous = None
        while args.wait:
            rows = client.get(
                f"/api/v1/knowledge-bases/{base['id']}/documents",
            ).raise_for_status().json()["items"]
            document = next(row for row in rows if row["id"] == document["id"])
            state["document"] = document
            args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            snapshot = json.dumps({"status": document["status"], "progress": document.get("progress"),
                                   "error": document.get("error_code")})
            if snapshot != previous:
                print(snapshot, flush=True)
                previous = snapshot
            if document["status"] == "ready":
                break
            if document["status"] == "failed":
                raise RuntimeError("ACG import failed: " + str(document.get("error_code")))
            time.sleep(10)


if __name__ == "__main__":
    main()
