# ADR 0002：Milvus 向量存储

日期：2026-10-09。状态：用户明确要求切换，基础设施已实现，等待本轮验收。

PostgreSQL 保留身份、资料、任务、审批与检查点，Milvus 负责向量。开发采用 standalone 2.5.14、etcd 3.5.18，复用 MinIO 服务并使用独立 bucket，固定镜像摘要；接口通过现有 httpx 调用 REST v2，避免新增 SDK 依赖。

PostgreSQL 改为普通 16 镜像并保留数据卷。迁移 0002 移除旧扩展，不使用 CASCADE；旧 0001 加入扩展可用性判断，兼容全新普通数据库，已执行版本不会重跑。切换前备份数据库；没有业务向量表，因此没有向量数据搬迁任务。历史迁移保留扩展名称用于兼容，当前运行依赖、镜像与检索不使用 pgvector。

开发端口 29530 为 REST/gRPC，29091 为健康端口，仅绑定本机。生产认证、TLS、资源与备份策略在阶段 6 验证。业务过滤与索引一致性在阶段 4 实现，本轮不宣称已完成 RAG。

依据：[官方 standalone Compose](https://raw.githubusercontent.com/milvus-io/milvus/v2.5.14/deployments/docker/standalone/docker-compose.yml)、[REST 集合创建](https://milvus.io/api-reference/restful/v2.5.x/v2/Collection%20(v2)/Create.md)、[REST 向量检索](https://milvus.io/api-reference/restful/v2.5.x/v2/Vector%20(v2)/Search.md)。
