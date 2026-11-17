# RAG 提示词按需读取

提示词位于 `backend/app/rag/prompts/`，每次功能调用只读取对应的 UTF-8 Markdown 文件，不预加载、不缓存。文件内容直接作为指令，不需要额外的 Markdown 标题或代码围栏。

| 文件 | 读取时机 |
| --- | --- |
| plan.md | 调用模型规划查询时 |
| grade.md | 调用模型判断证据和路由时 |
| rewrite.md | 调用模型改写查询时 |
| graph.md | 调用模型抽取图谱时 |
| answer_none.md | 无有效证据时组装回答指令 |
| answer_partial.md | 证据部分支持时组装回答指令 |
| answer_sufficient.md | 证据充分时组装回答指令 |

`StructuredModel.call()` 使用 `load_prompt(purpose)`；`answer_instruction()` 根据证据覆盖程度选择回答文件。全局安全规则、JSON Schema 和格式纠正逻辑继续由代码组合，输出和权限仍由代码校验。提示词提取没有改变原有文字。

本机 Python 下修改文件后，下次对应功能调用读取新内容。已组装好的请求不会中途替换指令；同一任务的后续调用可能读取新版本，因此做对比实验时应在每组运行期间保持文件不变。

开发 Compose 已将目录只读挂载到后端服务。首次应用此次代码及挂载变化需要构建后端并重新创建容器；此后仅修改这些 Markdown 无需重建或重启。生产环境仍随镜像发布，没有新增热更新挂载。

名称采用固定映射，不接受任意路径。文件缺失或无法读取会直接报错，不静默退回旧提示词；当前没有接入 Langfuse Prompt Management，也没有自动上报提示词版本。
