# PDF 导入失败原因与修复

2026-10-13：HGP PDF 正常提取出 128 个分块，但提取文本中含有 5 个不可见 NUL（U+0000）字符。写入 PostgreSQL 父级分块时触发 `DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`，因此 Embedding 和 Milvus 写入均未开始。

原先“准备索引”覆盖了集合校验和父级分块入库，异常统一显示 `vector_prepare_failed`，误导为 Milvus 或维度问题。实际检查集合通过，向量维度为 1024；本次失败发生在分块入库。

修复在 `backend/app/rag/parser.py` 中对 PDF 提取文本清除 NUL，再生成分块；保留原 PDF 文件及其他文本。TXT/Markdown 原有非法文本检查保持不变。`backend/app/rag/indexing.py` 将分块数据库错误独立标记为 `document_chunk_storage_failed`，集合不兼容、服务错误和超时也保留各自安全原因码；前端显示对应说明，不公开数据库语句、文档正文或凭据。

回归测试覆盖 PDF NUL 清理、数据库与向量错误区分及现有知识库导入流程，共 24 项通过。修复已更新到当前服务，原文件通过已有重试入口重新索引，128 个分块完成 Embedding 与 Milvus 写入，状态为 ready，无需重新上传。
