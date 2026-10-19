"""离线 BGE-M3 嵌入服务；兼容本机、Linux 服务器和容器部署。"""

import logging
import os
import secrets
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import torch
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer
from starlette.responses import Response

MODEL_ID = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
LOCK = threading.Lock()
model: SentenceTransformer | None = None
logger = logging.getLogger("uvicorn.error")


def selected_device() -> str:
    """自动选择可用 CUDA 设备；缺少显卡或 CUDA 运行时则使用 CPU。"""
    requested = os.getenv("BGE_DEVICE", "auto")
    if requested == "auto":
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    return requested


def local_model_path() -> Path:
    """显式路径优先；否则仅搜索本地 Hugging Face 缓存，不触发下载。"""
    cache = Path(os.getenv("HF_HUB_CACHE", Path.home() / ".cache/huggingface/hub"))
    root = Path(os.getenv("BGE_MODEL_PATH", cache / "models--BAAI--bge-m3")).resolve()
    if (root / "config.json").is_file():
        return root
    reference = root / "refs/main"
    if not reference.is_file():
        raise RuntimeError("本地 BGE-M3 缓存缺少 refs/main，请设置 BGE_MODEL_PATH 为完整模型目录")
    snapshot = (root / "snapshots" / reference.read_text().strip()).resolve()
    if not snapshot.is_relative_to(root) or not (snapshot / "config.json").is_file():
        raise RuntimeError("本地 BGE-M3 模型快照不完整")
    return snapshot


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """仅加载本地模型缓存，校验维度并在退出时释放模型引用。"""
    global model
    device = selected_device()
    try:
        model = SentenceTransformer(
            str(local_model_path()),
            device=device,
            local_files_only=True,
            trust_remote_code=False,
        )
    except RuntimeError as error:
        if os.getenv("BGE_DEVICE", "auto") != "auto" or not device.startswith("cuda"):
            raise
        logger.warning("CUDA 模型初始化失败，改用 CPU：%s", type(error).__name__)
        torch.cuda.empty_cache()
        model = SentenceTransformer(
            str(local_model_path()),
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
        )
    model.max_seq_length = 1024
    if model.get_sentence_embedding_dimension() != 1024:
        raise RuntimeError("BGE-M3 维度必须为 1024")
    logger.info("BGE-M3 实际运行设备：%s", model.device)
    yield
    model = None


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def monitor_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """记录独立推理服务的请求耗时与异常类型，不记录输入文本和认证头。"""
    request_id = uuid4().hex
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as error:
        logger.error(
            "embedding_failure request_id=%s error_type=%s",
            request_id,
            type(error).__name__,
        )
        response = JSONResponse(
            {
                "code": "embedding_service_failed",
                "message": "嵌入服务暂不可用",
                "request_id": request_id,
            },
            status_code=500,
        )
    route = getattr(request.scope.get("route"), "path", "unmatched")
    log = logger.warning if response.status_code >= 400 else logger.info
    log(
        "embedding_request request_id=%s route=%s status=%s duration_ms=%.2f",
        request_id,
        route,
        response.status_code,
        (time.monotonic() - started) * 1000,
    )
    response.headers["X-Request-ID"] = request_id
    return response


class EmbeddingInput(BaseModel):
    """限定嵌入服务的模型标识与批量输入数量。"""

    model: str = MODEL_ID
    input: list[str] = Field(min_length=1, max_length=16)


@app.get("/health")
def health() -> dict[str, object]:
    """报告模型加载状态、模型标识与向量维度。"""
    if model is None:
        raise HTTPException(503)
    device = str(model.device)
    return {
        "status": "ok",
        "model": MODEL_ID,
        "dimension": 1024,
        "device": device,
        "gpu_name": torch.cuda.get_device_name(model.device) if device.startswith("cuda") else None,
    }


@app.post("/v1/embeddings")
def embeddings(data: EmbeddingInput, authorization: str = Header(default="")) -> dict[str, Any]:
    """验证认证与输入后串行调用本地模型，返回标准嵌入响应。"""
    expected = "Bearer " + os.getenv("EMBEDDING_API_KEY", "dev-embedding-key")
    if not secrets.compare_digest(authorization, expected):
        raise HTTPException(401)
    if data.model != MODEL_ID or any(not text.strip() or len(text) > 2000 for text in data.input):
        raise HTTPException(422)
    # 模型推理串行，避免并发批次把本机或服务器内存耗尽。
    with LOCK:
        if model is None:
            raise HTTPException(503)
        try:
            vectors = model.encode(data.input, batch_size=4, normalize_embeddings=True).tolist()
        except torch.cuda.OutOfMemoryError:
            if os.getenv("BGE_DEVICE", "auto") != "auto":
                raise
            logger.warning("CUDA 显存不足，嵌入模型切换 CPU 并重试当前批次")
            model.to("cpu")
            torch.cuda.empty_cache()
            vectors = model.encode(data.input, batch_size=4, normalize_embeddings=True).tolist()
    return {
        "object": "list",
        "model": MODEL_ID,
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
    }
