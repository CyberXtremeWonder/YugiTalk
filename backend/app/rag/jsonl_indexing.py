"""大型 JSONL 按记录读取、按批写入，并在有界任务之间保存字节检查点。"""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from threading import Event, Lock, local
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import httpx
from sqlalchemy import Engine, delete, insert, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.rag.chunking import requested_strategy, strategy_version
from app.rag.embeddings import Embeddings, VectorIndex
from app.rag.graph import clear_graph, queue_graph
from app.rag.hierarchy import attach_parents
from app.rag.indexing import dispatch_document, mark_progress, owns_document
from app.rag.limits import MAX_FILE_BYTES
from app.rag.models import Chunk, Document, ParentChunk
from app.rag.parser import parse
from app.rag.storage import ObjectStorage

SLICE_SECONDS = 60
RECORD_BATCH_CHUNKS = 128


class ImportTimings:
    """累计各阶段实际工作时间；嵌套阶段不重复计时。"""

    def __init__(self, previous: dict[str, float]) -> None:
        self.seconds = dict(previous)
        self.local = local()
        self.lock = Lock()

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        started = time.perf_counter()
        if not hasattr(self.local, "stack"):
            self.local.stack = []
        stack = self.local.stack
        stack.append(0.0)
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            nested = stack.pop()
            with self.lock:
                self.seconds[stage] = self.seconds.get(stage, 0) + elapsed - nested
            if stack:
                stack[-1] += elapsed

    def iterate(self, items: Iterator[Any], stage: str) -> Iterator[Any]:
        while True:
            with self.measure(stage):
                try:
                    item = next(items)
                except StopIteration:
                    return
            yield item

    def snapshot(self) -> dict[str, float]:
        with self.lock:
            return {key: round(value, 6) for key, value in self.seconds.items()}


def prefetch(
    items: Iterator[Any], pool: ThreadPoolExecutor, stopped: Event,
) -> Iterator[Any]:
    """每阶段只预取一批；异常在消费时传播，退出时不再发出新工作。"""
    def advance() -> Any:
        return None if stopped.is_set() else next(items, None)

    pending = pool.submit(advance)
    while not stopped.is_set():
        item = pending.result()
        if item is None:
            return
        pending = pool.submit(advance)
        yield item


def records(blocks: Iterator[bytes], offset: int = 0) -> Iterator[tuple[int, bytes]]:
    """按 LF 分隔记录并返回下一字节位置；跨网络块的单行仍受大小限制。"""
    pending = bytearray()
    for block in blocks:
        pending.extend(block)
        start = 0
        while (end := pending.find(b"\n", start)) >= 0:
            if end + 1 - start > MAX_FILE_BYTES:
                raise ValueError("jsonl_record_too_large")
            line = bytes(pending[start:end + 1])
            offset += len(line)
            yield offset, line
            start = end + 1
        del pending[:start]
        if len(pending) > MAX_FILE_BYTES:
            raise ValueError("jsonl_record_too_large")
    if pending:
        yield offset + len(pending), bytes(pending)


def record_batches(
    blocks: Iterator[bytes], strategy: str, offset: int,
    *, optimize_jsonl: bool = True,
) -> Iterator[tuple[int, int, list[dict[str, Any]]]]:
    """合并相邻记录的独立分块以填满模型批次，父块边界保持分离。"""
    batch: list[dict[str, Any]] = []
    count, lines, group_offset = 0, 0, 0
    next_offset = offset
    for next_offset, raw in records(blocks, offset):
        lines += 1
        try:
            nonempty = bool(raw.decode("utf-8-sig").strip())
        except UnicodeDecodeError:
            raise ValueError("invalid_text") from None
        if nonempty:
            parts = parse(raw, ".jsonl", strategy, optimize_jsonl=optimize_jsonl)
            highest_group = 0
            for part in parts:
                if "parent_group" in part:
                    group = cast(int, part["parent_group"])
                    highest_group = max(highest_group, group)
                    part["parent_group"] = group + group_offset
                else:
                    part["section"] = count
            group_offset += highest_group
            batch.extend(parts)
            count += 1
        if len(batch) >= RECORD_BATCH_CHUNKS or lines >= 128:
            yield next_offset, count, batch
            batch, count, lines, group_offset = [], 0, 0, 0
    if lines:
        yield next_offset, count, batch


def index_jsonl_slice(
    engine: Engine, settings: Settings, key: str, generation: int, token: str,
) -> None:
    """单次最多工作约一分钟；记录全部成功后才推进持久检查点。"""
    # 连接池只活在当前任务内；不跨 Worker 进程或不同模型配置共享认证上下文。
    with (
        httpx.Client(timeout=settings.embedding_timeout_seconds, trust_env=False) as model_client,
        httpx.Client(timeout=30, trust_env=False) as vector_client,
    ):
        _index_jsonl_slice(
            engine, settings, key, generation, token,
            Embeddings(settings, client=model_client), VectorIndex(settings, client=vector_client),
        )


def _index_jsonl_slice(
    engine: Engine, settings: Settings, key: str, generation: int, token: str,
    embeddings: Embeddings, vectors: VectorIndex,
) -> None:
    """在有界连接池中处理当前切片，退出或异常均由调用方关闭连接。"""
    deadline = time.monotonic() + min(SLICE_SECONDS, settings.document_index_timeout_seconds / 2)
    with Session(engine) as db:
        doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
        if not owns_document(doc, generation, token) or doc is None:
            return
        offset = cast(int, doc.progress.get("jsonl_offset", 0))
        completed = cast(int, doc.progress.get("committed_chunks", 0))
        parents = cast(int, doc.progress.get("committed_parents", 0))
        record_count = cast(int, doc.progress.get("processed_records", 0))
        parser_version = cast(int, doc.progress.get("jsonl_parser_version", 1 if offset else 2))
        timings = ImportTimings(cast(dict[str, float], doc.progress.get("stage_seconds", {})))
        doc.progress = {**doc.progress, "jsonl_parser_version": parser_version}
        object_key, workspace, base, collection, size = (
            doc.object_key, doc.account_id, doc.knowledge_base_id, doc.collection_name, doc.size,
        )
        strategy = requested_strategy(doc.chunk_strategy)
        if not offset:
            clear_graph(db, key)
            db.execute(delete(Chunk).where(Chunk.document_id == key))
            db.execute(delete(ParentChunk).where(ParentChunk.document_id == key))
        doc.status = "indexing"
        doc.chunk_strategy = strategy_version(strategy, "records") if strategy == "auto" else (
            strategy_version(strategy)
        )
        db.commit()
    if not mark_progress(engine, key, generation, "preparing", token):
        return
    with timings.measure("preparing"):
        vectors.ensure(embeddings)
    if not mark_progress(engine, key, generation, "parsing", token):
        return
    with (
        closing(ObjectStorage(settings).chunks(object_key, offset)) as blocks,
        ExitStack() as workers,
    ):
        parsing_pool = workers.enter_context(ThreadPoolExecutor(max_workers=1))
        encoding_pool = workers.enter_context(ThreadPoolExecutor(max_workers=1))
        stopped = Event()
        database_lock = Lock()
        workers.callback(stopped.set)
        batches = record_batches(
            timings.iterate(blocks, "reading"), strategy, offset,
            optimize_jsonl=parser_version >= 2,
        )
        parsed = prefetch(timings.iterate(batches, "parsing"), parsing_pool, stopped)

        def encode_batches() -> Iterator[Any]:
            for next_byte, increment, parsed_parts in parsed:
                encoded = []
                batch_size = settings.embedding_batch_size
                for position in range(0, len(parsed_parts), batch_size):
                    if stopped.is_set():
                        return
                    with database_lock, Session(engine) as guard_db:
                        if not owns_document(guard_db.get(Document, key), generation, token):
                            return
                    with database_lock:
                        if not mark_progress(engine, key, generation, "embedding", token):
                            return
                    with timings.measure("embedding"):
                        encoded.extend(embeddings.encode([
                            part["content"] for part in parsed_parts[position:position + batch_size]
                        ]))
                yield next_byte, increment, parsed_parts, encoded

        for next_offset, record_increment, parts, encoded_parts in prefetch(
            encode_batches(), encoding_pool, stopped,
        ):
            for number, part in enumerate(parts, completed + 1):
                part["ordinal"] = number
            parent_count = 0
            if parts:
                with database_lock, timings.measure("sql"), Session(engine) as db:
                    doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
                    if not owns_document(doc, generation, token):
                        return
                    attach_parents(db, key, generation, parts, parents)
                    parent_count = len({part["parent_id"] for part in parts})
                    db.commit()
            for start in range(0, len(parts), RECORD_BATCH_CHUNKS):
                batch = parts[start:start + RECORD_BATCH_CHUNKS]
                encoded = encoded_parts[start:start + len(batch)]
                with database_lock:
                    if not mark_progress(engine, key, generation, "writing", token):
                        return
                with database_lock, timings.measure("sql"), Session(engine) as db:
                    doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
                    if not owns_document(doc, generation, token) or doc is None:
                        return
                    chunks, rows = [], []
                    for part, vector in zip(batch, encoded, strict=True):
                        chunk_id = str(uuid5(
                            NAMESPACE_URL, f"{key}:{generation}:{part['ordinal']}",
                        ))
                        chunks.append({
                            "id": chunk_id, "document_id": key, "ordinal": part["ordinal"],
                            "page": None, "content": part["content"], "index_version": generation,
                            "parent_id": part["parent_id"],
                        })
                        rows.append({
                            "id": chunk_id, "vector": vector, "account_id": workspace,
                            "knowledge_base_id": base, "document_id": key, "chunk_id": chunk_id,
                            "index_version": generation, "embedding_version": embeddings.version,
                            "text": part["content"],
                        })
                    with timings.measure("writing"):
                        vectors.upsert(collection, rows)
                    if not owns_document(doc, generation, token):
                        db.rollback()
                        return
                    db.execute(insert(Chunk), chunks)
                    count = completed + start + len(batch)
                    doc.progress = {
                        **doc.progress, "embedded_chunks": count, "indexed_chunks": count,
                    }
                    db.commit()
            completed += len(parts)
            parents += parent_count
            record_count += record_increment
            offset = next_offset
            with database_lock, Session(engine) as db:
                doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
                if not owns_document(doc, generation, token) or doc is None:
                    return
                doc.progress = {
                    **doc.progress, "stage": "parsing", "jsonl_offset": offset,
                    "embedded_chunks": completed, "indexed_chunks": completed,
                    "committed_chunks": completed,
                    "committed_parents": parents, "processed_records": record_count,
                    "processed_bytes": offset, "source_bytes": size, "streaming": True,
                    "stage_seconds": timings.snapshot(),
                }
                if time.monotonic() >= deadline and offset < size:
                    doc.status = "queued"
                    doc.attempts = 0
                    doc.lease_token, doc.lease_until, doc.last_dispatched_at = None, None, None
                    doc.progress = {**doc.progress, "stage": "queued"}
                    db.commit()
                    # 持久排队意图已提交，投递失败由调度器补投。
                    try:
                        dispatch_document(key, generation)
                    except Exception:
                        pass
                    return
                db.commit()
    if offset != size:
        raise ValueError("document_storage_read_failed")
    if not completed:
        raise ValueError("empty_document")
    with Session(engine) as db:
        doc = db.scalar(select(Document).where(Document.id == key).with_for_update())
        if not owns_document(doc, generation, token) or doc is None:
            return
        doc.status = "ready"
        doc.lease_token, doc.lease_until = None, None
        doc.progress = {**doc.progress, "stage": "ready", "total_chunks": completed,
                        "stage_seconds": timings.snapshot()}
        if settings.rag_graph_enabled and size <= MAX_FILE_BYTES:
            queue_graph(db, doc)
        db.commit()
