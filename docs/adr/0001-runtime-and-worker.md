# ADR 0001：运行时、编排与队列

日期：2026-10-09。状态：待阶段 0 用户验收。

## 决策

- Python 固定 3.12 系列，前端 Node 最低 22.12；当前机器 Node 26.10.0。
- 后端依赖以 `backend/uv.lock` 为准，前端以 `frontend/package-lock.json` 为准；不依赖自动升级的范围重新解析生产包。
- 首次本地验证使用 Python 3.12.14；容器先采用 Python 3.12.12 slim，验证后记录实际镜像摘要。生产镜像在阶段 6 再按安全扫描结果锁定。
- 选用 Celery + Redis，Worker 在 Linux 容器运行，数据库检查点使用 LangGraph PostgreSQL Saver。
- 通过应用接口封装编排对象；首次建表与应用启动分开，避免多实例启动竞争。
- React + TypeScript + Vite，Radix 提供无样式可访问交互原语，自有设计 Token 提供简洁科技感。

## 原因与限制

本机只有 Python 3.10 和 3.14；项目安装独立 3.12 到 `.runtimes`，避免修改系统环境。Celery 官方不支持 Windows，不能将原生 Windows Worker 测试当作生产平台验证。

框架检查点解决状态持久化，不能独自保证外部写操作恰好执行一次。后续需要数据库唯一约束、外部幂等键与结果核对共同保证正确性。

阶段 0 使用 Mock 工具调用契约，不访问付费模型；没有真实供应商凭证，真实工具调用兼容性留到阶段 3，不写成已通过。

## 官方依据

- [Celery 运行环境](https://docs.celeryq.dev/en/stable/getting-started/introduction.html)
- [LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Vite 环境要求](https://vite.dev/guide/)
- [Compose 合并行为](https://docs.docker.com/reference/compose-file/merge/)

开发 Compose 与生产覆盖文件会在相应阶段提供；阶段 0 的隔离验证 Compose 不替代正式部署配置。
