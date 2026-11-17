# 本机 Ollama

开发环境的 Agent 使用 OpenAI 兼容供应商，URL 填
`http://host.docker.internal:11434/v1`，API Key 填 `ollama`。
模型名称复制 `ollama list` 的完整 NAME 列。

## 工具返回后发生模板错误

Qwen3.5 的上下文窗口过小时，Ollama 可能在加入较长工具回包后裁掉用户问题，
继而报 `No user query found in messages.`。增加应用的字节上限不能改变 Ollama 的窗口。

在 Modelfile 中设置 `PARAMETER num_ctx 16384`，或通过 `/api/create` 从现有模型继承并覆盖
`parameters: {"num_ctx": 16384}`。使用 `/api/ps` 检查已加载模型的 `context_length`。
重新执行 `ollama pull` 可能重新写入发布者的模型配置；升级后需再次核对窗口。
扩大窗口会增加显存占用，仍需按实际硬件和文件长度控制。

本机已用相同的普通文档工具回包验证：4096 窗口出现模板错误，16384 窗口完成回复。

## 会话摘要达到输出上限

历史对话较长时，系统先调用模型生成会话摘要，内部上限为 1024 Token，
与助手设置的正文输出额度分开。Ollama 默认思考可能耗尽摘要额度而未返回正文。
对本机 Ollama（本地地址、11434 端口）的摘要调用，应用通过其 OpenAI 兼容接口
传入 `reasoning_effort: "none"`；普通问答和其他服务的请求参数保持原有行为。
摘要未完整结束时不会保存半截摘要，界面使用独立的摘要超限提示。

使用普通项目历史实测：默认模式 1024 Token 后以 `length` 结束、正文为空；
关闭摘要思考后以 `stop` 结束，返回 533 字正文、输出 312 Token。
接口参数依据 [Ollama 官方兼容说明](https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx)。
