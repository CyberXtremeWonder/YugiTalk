# 阶段 4：知识库、检索引用与个人记忆

日期：2026-10-10。状态：用户已确认“阶段4验收通过”；阶段 5 已开始。

## 交付范围

- TXT、Markdown、文本 PDF 的异步导入、同库去重、重建索引、失败重试和删除。
- 文件夹选择支持递归导入子目录中的 TXT、Markdown、PDF；每文件最多 20 MB，一次最多 200 个支持文件。不支持格式跳过，单文件上传失败继续，权限或资料数上限错误停止后续上传。
- 页面左右布局：上传入口、批次统计和竖向流程在左侧，文件列表与检索在右侧；窄屏改为上下排列。全部索引成功后清空流程，保留文件；失败流程继续显示，下次上传重新计数。删除成功立即从文件列表与流程隐藏，后台清理失败可单独重试。
- 导入流程每 2 秒读取真实状态：等待处理 → 读取原文 → 解析与分块 → 准备索引 → Embedding 编码 → 写入 Milvus → 完成。编码/写入按最多 16 块一批交替处理，分别显示完成数量；失败保留具体步骤、安全原因和成功数量。重试开启新索引版本并清零进度。
- 原文保存至 MinIO/S3；元数据与状态保存至 PostgreSQL；1024 维向量保存至 Milvus。
- 本地缓存 BGE-M3 离线编码及服务器 OpenAI 兼容 Embeddings 接口。
- 对话选择知识库、检索授权分块、文件及页码/分块来源预览、历史引用恢复。来源是检索证据，不代表自动证明每句话均正确。
- 有来源的抽取式历史摘要；用户主动确认的个人偏好与修改、删除、关闭。

上传最多 20 MB，UTF-8 文本最多 50 万字符，PDF 最多 200 页，分块最多 1000 个。解析运行在独立进程，设置时间与输出上限，Linux 额外限制内存和 CPU。当前固定 700 字符窗口、100 字符重叠。扫描或加密 PDF 明确拒绝，OCR 留作后续扩展。

空间权限在检索之前转换为 Milvus 标量条件；SQL 再校验文档可用状态、索引版本和成员资格。任务重复投递通过文档状态、版本与确定性分块 ID 去重。删除立即停止召回，异步清理原文和向量，失败允许重试。不存在跨 PostgreSQL、S3、Milvus 的共享事务。

## 本机与服务器模型配置

聊天模型的 `MODEL_*` 保持独立；DeepSeek 可以继续用于生成回答。`EMBEDDING_*` 专用于文档与查询的向量编码。

Windows `.env` 已设置：

```dotenv
COMPOSE_PROFILES=local-embedding
BGE_MODEL_PATH=E:/Code/HUGGINGFACE_CACHE/models--BAAI--bge-m3
```

默认模型 BAAI/bge-m3、维度 1024；服务只读挂载缓存，通过 `refs/main` 找到 snapshot，也支持直接指向包含 `config.json` 的完整模型目录。模型不存在时明确失败，不下载、不静默使用 Mock。首次构建嵌入服务会下载 CPU 推理依赖，Docker 安装使用 `--no-cache-dir`。

```powershell
docker compose up --build -d --wait --wait-timeout 180 embedding
```

Windows 本机 API 默认访问 `http://127.0.0.1:18081/v1`；容器 API 默认访问 `http://embedding:8000/v1`。模型进程只加载一份，API 和 Worker 共享它。CPU 推理内存上限 6 GB，具体延迟取决于机器。

Linux 服务器同样设置 `COMPOSE_PROFILES=local-embedding`，把 `BGE_MODEL_PATH` 换成服务器实际路径，例如 `/srv/models/models--BAAI--bge-m3`，确保容器用户可读取。无需使用 Windows 路径。

服务器已提供模型接口时：

```dotenv
COMPOSE_PROFILES=
EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_BASE_URL=https://embedding.example.com/v1
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSION=1024
EMBEDDING_API_KEY=替换为服务器密钥
```

接口必须实现 `POST /v1/embeddings`，接受文本数组并返回按 index 对应的有限数值向量。部署前检查实际维度；切换模型、修订或维度后点击“重建索引”，新旧模型使用不同集合，旧索引不会混用。远程接口协议已实现，用户自己的远程服务器仍需使用实际凭证联调。

## 自动测试与证据

```powershell
pwsh -NoProfile -File scripts/test-stage4.ps1 -IncludeContainers -IncludeUI
```

静态检查覆盖 Ruff、格式、mypy、ESLint、TypeScript 和构建。本地单测：后端 105 通过、3 跳过（阶段 0 独立容器测试）；前端 21 通过。

浏览器覆盖阶段 2–4 共 20 个场景：身份权限、多轮工具、取消、真实 BGE-M3 导入/去重/检索、引用刷新、删除传播、个人偏好和手机布局。隔离测试使用随机数据库、S3 桶和 Milvus 集合，结束后清理。

真实检索评测固定 5 份中文文档、10 个问题，门槛 Top-1 ≥ 80%、Recall@5 ≥ 90%。本次 Top-1 90%、Recall@5 100%；详细结果读取 [检索评测记录](./artifacts/stage4-rag-evaluation.json)，不能据此推断通用知识库准确率。生成回答的自动测试使用 Mock；当前开发环境的真实聊天模型需要下面的人工验证。

其他证据：[后端](./artifacts/stage4-backend-tests.xml)、[前端](./artifacts/stage4-frontend-tests.xml)、[浏览器](./artifacts/stage4-ui-results.json)、[隔离清理](./artifacts/stage4-integration.json)、[缓存检查](./artifacts/stage4-cache-cleanup.json)。

## 人工验收

1. 在知识库页创建知识库，上传自己的中文 Markdown 或文本 PDF，观察每个文件的导入流程和分块进度，等待“可用”。重复上传应保留一条；损坏 PDF 应标明“失败步骤：解析与分块”，扫描 PDF 应明确提示需要 OCR。重试后检查版本增加、旧计数清零。
2. 新建对话，选择该知识库和实际聊天助手，提问文档里的问题。检查回答、展开来源、核对文件及页码/片段；问一个资料不包含的问题，确认不会把常识伪装成文档事实。
3. 刷新并按名称打开该对话，来源仍可查看；删除文档后重新检索，应不再召回，旧来源请求应提示不可用。
4. 在设置页保存一个已确认的偏好，检查修改与删除；关闭记忆后表单隐藏，不向模型注入偏好。不同账号和空间不得串用。
5. 检查 `docker compose ps`、API 就绪、缓存清理记录，确认 C 盘依赖缓存已检查，E 盘模型仍存在。

## 边界及后续工作

当前摘要是有界抽取，不是模型生成的语义总结；用户关键约束超限会停止任务。个人偏好保存需确认，敏感词拦截不等同于完整 DLP。当前不支持 OCR、稀疏/多向量检索或 reranker。

索引失败可重试；Worker 在解析中突然退出后的自动租约回收属于阶段 5。完整生产配置、HTTPS、备份、性能与安全发布门槛属于阶段 6。本阶段通过不代表可正式上线。

## 缓存收尾

每阶段必须执行 `scripts/clean-stage-cache.ps1`。已清理本阶段新增的 boto3、botocore、jmespath、pypdf、s3transfer 的 C 盘 uv 下载缓存，首次释放约 25 MiB；后续 uv/pip/npm 缓存转至项目 `.cache/`。不清理虚拟环境、已安装依赖、模型和数据卷。重复清理为幂等操作，JSONL 保留每次收尾记录。
