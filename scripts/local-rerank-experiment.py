"""仅供本机对照实验的重排服务，加载固定本地权重，不发送资料到外部。"""

import os
import secrets
import threading
import time
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoModelForSequenceClassification, AutoTokenizer

LOCK = threading.Lock()
STATE = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时加载本地固定模型；服务监听由调用方限制在本机。"""
    path = os.environ["RERANK_MODEL_PATH"]
    STATE["tokenizer"] = AutoTokenizer.from_pretrained(path, local_files_only=True)
    STATE["model"] = (
        AutoModelForSequenceClassification.from_pretrained(
            path,
            local_files_only=True,
            torch_dtype=torch.float16,
        )
        .to("cuda:0")
        .eval()
    )
    yield
    STATE.clear()


app = FastAPI(lifespan=lifespan)


class Request(BaseModel):
    """模型输入按现有重排协议受限，不接受任意模型或无限文本。"""

    query: str = Field(min_length=1, max_length=1000)
    documents: list[str] = Field(min_length=1, max_length=48)
    model: str = "BAAI/bge-reranker-v2-m3"
    top_n: int = Field(default=48, ge=1, le=48)


@app.get("/health")
def health():
    """公开模型就绪状态，不返回令牌和输入原文。"""
    return {"ready": "model" in STATE, "device": "cuda:0", "max_length": 1024}


@app.post("/rerank")
def rerank(data: Request, authorization: str = Header(default="")):
    """逐对推理返回原始相关性分数，禁止跨请求共享输入或记录原文。"""
    expected = "Bearer " + os.environ["RERANK_TEST_KEY"]
    if not secrets.compare_digest(authorization, expected):
        raise HTTPException(401)
    if (
        any(not text or len(text) > 2000 for text in data.documents)
        or sum(map(len, data.documents)) > 24000
    ):
        raise HTTPException(422)
    started = time.monotonic()
    scores = []
    with LOCK, torch.inference_mode():
        for start in range(0, len(data.documents), 8):
            inputs = STATE["tokenizer"](
                [[data.query, text] for text in data.documents[start : start + 8]],
                padding=True,
                truncation=True,
                max_length=1024,
                return_tensors="pt",
            ).to("cuda:0")
            scores.extend(
                STATE["model"](**inputs).logits.reshape(-1).float().cpu().tolist()
            )
    return {
        "results": [
            {"index": index, "relevance_score": score}
            for index, score in enumerate(scores)
        ],
        "inference_seconds": time.monotonic() - started,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
