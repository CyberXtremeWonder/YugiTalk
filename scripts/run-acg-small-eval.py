"""Import the prepared small corpus into an isolated KB, then evaluate through normal APIs."""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/acg_simpleqa_small_20261029"


def main():
    private = dotenv_values(ROOT / "backend/.env.rag-test")
    with httpx.Client(
        base_url=private["RAG_TEST_API_URL"], timeout=120, trust_env=False
    ) as client:

        def login():
            response = client.post(
                "/api/v1/auth/login",
                headers={"Origin": private["RAG_TEST_ORIGIN"]},
                json={
                    "email": private["RAG_TEST_ACCOUNT"],
                    "password": private["RAG_TEST_PASSWORD"],
                },
            )
            response.raise_for_status()
            return {
                "Origin": private["RAG_TEST_ORIGIN"],
                "X-CSRF-Token": response.json()["csrf_token"],
            }

        headers = login()
        state_path = OUT / "import.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
        else:
            base = (
                client.post(
                    "/api/v1/me/knowledge-bases",
                    headers=headers,
                    json={"name": "ACG-SimpleQA 小语料评测 200题 20261029"},
                )
                .raise_for_status()
                .json()
            )
            state = {
                "knowledge_base_id": base["id"],
                "agent_version_id": private["RAG_TEST_AGENT_VERSION_ID"],
            }
            state_path.write_text(json.dumps(state, indent=2))
        if "document_id" not in state:
            with (OUT / "corpus.jsonl").open("rb") as source:
                document = (
                    client.post(
                        f"/api/v1/knowledge-bases/{state['knowledge_base_id']}/documents",
                        params={
                            "filename": "ACG-small-reference-and-distractors.jsonl",
                            "chunk_strategy": "auto",
                        },
                        content=source,
                        headers={**headers, "Content-Type": "application/octet-stream"},
                    )
                    .raise_for_status()
                    .json()
                )
            state["document_id"] = document["id"]
            state_path.write_text(json.dumps(state, indent=2))
        while True:
            response = client.get(
                f"/api/v1/knowledge-bases/{state['knowledge_base_id']}/documents"
            )
            if response.status_code == 401:
                headers = login()
                continue
            response.raise_for_status()
            document = next(
                d for d in response.json()["items"] if d["id"] == state["document_id"]
            )
            state["document"] = document
            state_path.write_text(json.dumps(state, indent=2))
            print(
                json.dumps(
                    {
                        "stage": "indexing",
                        "status": document["status"],
                        "progress": document["progress"],
                    }
                ),
                flush=True,
            )
            if document["status"] == "ready":
                break
            if document["status"] == "failed":
                raise RuntimeError(
                    "Small corpus import failed: " + str(document.get("error_code"))
                )
            time.sleep(3)
    # The corpus is retrieval-only. Cancel only this new document's graph build, never other KBs.
    program = """import sys
from sqlalchemy.orm import Session
from app.tasks.database import database
from app.rag.models import Document
from app.rag.graph_models import GraphBuild
engine,_=database.get()
with Session(engine) as db:
    doc=db.get(Document,sys.argv[1])
    assert doc and doc.knowledge_base_id==sys.argv[2] and doc.status=='ready'
    build=db.get(GraphBuild,doc.id)
    if build:
        build.status='disabled'
        build.token=None
        build.lease_until=None
        db.commit()
print('isolated_graph_disabled')
"""
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "worker",
            "python",
            "-c",
            program,
            state["document_id"],
            state["knowledge_base_id"],
        ],
        cwd=ROOT,
        check=True,
    )
    command = [
        sys.executable,
        str(ROOT / "scripts/evaluate-acg-simpleqa.py"),
        "--directory",
        str(OUT),
        "--workers",
        "2",
    ]
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
