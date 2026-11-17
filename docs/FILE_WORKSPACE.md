# 文件工作空间与 read_files

> 此设置界面已移除，用户文件改由[对话附件上传](CHAT_UPLOADS.md)提供。
> 以下保留旧目录模式的兼容说明，不再是前端操作入口。

旧设置界面的“工作空间”曾用于选择本机文件目录，默认目录为空、读取关闭。
下面记录旧浏览器目录导入和仍保留的服务端兼容机制，不是当前页面操作步骤。

## 旧目录导入机制

- 点击“选择本机文件夹”，在系统窗口选择任意本机文件夹，确认文件清单后点击“导入工作空间”。
- 导入支持 TXT、Markdown 和 Word `.docx`，保留所选目录内的相对路径。
  每次最多 100 个文件、总计 50 MiB；文本文件最多 64 KiB，Word 输入最多 10 MiB，
  Word 提取文本最多 64 KiB。旧 `.doc` 需要先转换成 `.docx`。
- 导入后检查目录名称及文件清单，再启用文件读取并保存。可以随时更换目录或重新导入。
- 保存的是导入时的文件快照，本机原文件更新后需重新导入；浏览器不能提供真实绝对目录路径，
  界面因此显示文件夹名称。关闭网页后，已导入快照仍可由任务读取。
- 清空会关闭读取、移除清单并尽力删除旧快照对象，不修改本机原文件。

导入验证登录用户、来源和 CSRF。文件保存在现有 S3 兼容对象存储，
对象键按用户及本次导入随机生成，浏览器和模型不能指定对象键。
先验证全部文件，再上传并一次提交清单；文件无效或上传失败时保留原工作空间。
提交确认丢失时返回“不确定”状态，请刷新查看结果；此时保留可能已生效的新对象。
旧对象清理失败不会恢复已撤销的读取权限。

### 可选的服务器目录模式

已有服务器目录接口保留给部署者主动开放的目录，与浏览器导入互不依赖：

- 本机运行后端时，`WORKSPACE_ALLOWED_ROOTS` JSON 列表指定允许范围。
- Docker 默认不挂载任何用户目录；如需旧服务器目录模式，可设置 `.env` 的
  `WORKSPACE_HOST_ROOT`，再启动可选覆盖配置：

```powershell
docker compose -f docker-compose.yml -f docker-compose.workspace.yml up -d --no-deps api worker
```

该覆盖文件把指定本机目录只读挂载到 API 和 Worker 的 `/workspaces`，并仅开放这个入口。
服务器目录 API 此时使用 `/workspaces` 或子目录，不能把保存宿主机地址当成容器已能读取。
启用时检查实际目录和允许范围，不自动创建目录。
部署允许目录是已登录用户的可选范围，用户的配置只能缩小范围。

## 工具实现位置

工具按本地、HTTP、MCP 分类，完整目录见 [工具入口说明](../backend/app/tools/README.md)。
`read_files` 的参数位于 `tools/builtin/contracts.py`，实际读取位于 `tools/builtin/files.py`。
参数只有工作空间内的相对路径；用户身份、文件清单或根目录及配置版本由执行层注入。
导入与对象存储定位位于 `workspaces/imports.py`，文本及 Word 提取复用内置文件工具。
还需在 Agent 中勾选 `read_files` 并发布版本；绑定 Skill 时，其工具许可也需要包含它。

工作空间首次用于文件读取后会绑定到任务；更换目录、重新导入、关闭读取或撤销允许范围后，
旧任务的成功缓存和恢复过程也必须重新校验，不能自动转到新目录。已发送的历史答复不因此删除。
支持 UTF-8 TXT / Markdown 和 Word `.docx` 正文、表格，提取文本最多 64 KiB，
Word 输入最多 10 MiB；旧 `.doc` 需要先转换成 `.docx`。不提供文件写入。

## API 与迁移

- `GET /api/v1/me/workspace`：本人配置；无记录时返回空目录与关闭状态。
- `PUT /api/v1/me/workspace`：携带 CSRF 保存 `{directory, enabled}`；清空时传空目录和 `false`。
- `POST /api/v1/me/workspace/import`：携带 CSRF 的 `multipart/form-data`；
  `directory` 是选定文件夹显示名称，`files` 为重复文件字段，每个上传文件名是去掉根目录后的相对路径。
  返回 `source: "imported"`、`file_count`、`total_bytes`、`imported_at`、`files: [{path, size}]`，
  不返回对象键。导入后默认关闭；`PUT` 传同一显示名称和 `enabled: true` 可显式启用。
- `GET /api/v1/me/workspace/directories?path=...`：只浏览已开放范围内的目录。

需要迁移 `0024_user_workspace`、`0025_workspace_import`。
配置按当前认证用户保存，接口不接受前端指定用户 ID 或对象清单。
