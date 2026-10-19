# 大文件索引超时

2026-10-13：732 个分块的 PDF 在 272 块时失败，Worker 日志记录 `Soft time limit (300s) exceeded` 和 `SoftTimeLimitExceeded`。原实现将文件索引与普通对话共用 300 秒的 Celery 执行时限。本地 BGE-M3 推理串行运行，多个文件并发任务会排队，长文件总耗时超过五分钟后被中断；不是模型服务退出或向量维度不匹配。

`backend/app/core/config.py` 新增 `document_index_timeout_seconds`，默认 1800 秒，允许 60～7200 秒。`backend/app/tasks/worker.py` 给 `yugitalk.knowledge.index` 单独配置软时限及软时限加 10 秒的硬时限。普通对话时限不变，单次 Embedding HTTP 请求时限也不变。

在本地 `.env` 配置 `DOCUMENT_INDEX_TIMEOUT_SECONDS=1800` 后重新创建 Worker 即可调整开发索引时限；正式 Compose 使用 `.env.production`。超时原因码为 `document_index_timeout`，前端明确说明任务执行超时，避免误报模型服务不可用。

失败文件通过“重试”重新索引；当前重试实现会创建新的索引版本并从头编码，并非断点续算。原文件无需重新上传。25 项知识库回归测试通过。
