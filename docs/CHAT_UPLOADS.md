# 对话文件上传

在对话输入框左下角点击“上传文件”，选择文件后随消息发送，也可只发送附件。
每条消息最多 3 个文件，单个最多 20 MiB。支持 Word `.docx`、PDF、TXT、Markdown、CSV、JSON、LOG、RST。
旧 `.doc` 需转换；扫描 PDF 需先进行文字识别。

服务端提取正文，单文件超过 60 KB 文本时明确标记截取；每条消息的附件正文合计最多 90 KB。
文件原始二进制不持久保存；发送后的提取文本随聊天消息保存，任务历史裁剪不删除聊天附件。
旧版本升级需应用 `0026_message_uploads` 迁移，从仍存在的任务快照回填附件；已清理的快照无法恢复。
历史消息压缩时，附件正文也会交给摘要模型，后续对话使用该摘要保留必要信息。
附件作为用户级资料进入上下文，不具有系统指令权限。

- `POST /api/v1/chat/uploads`：原始文件字节，携带登录 Cookie、CSRF 和 URL 编码的 `X-Filename`。
  返回 `{filename,size,text,truncated}`；只解析成功后才可随消息提交。
- `POST /api/v1/runs`：可选 `uploaded_files` 数组，最多 3 项；加入幂等内容校验。
- 实现：[解析接口](../backend/app/chat/uploads.py)、[上传按钮](../frontend/src/chat/ChatView.tsx)。

设置页的目录工作空间已移除；聊天附件不需要 Docker 目录挂载或 `read_files` 工具授权。
