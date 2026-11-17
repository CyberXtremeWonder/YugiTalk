# 工具代码入口

## 后台种子下载

`http/torrent_contract.py` 定义 `download_torrent` 的 start/status 参数，
`http/torrent.py` 通过固定 aria2 RPC 提交磁力链接或 HTTPS `.torrent` 文件并查询进度。
任务执行层注入用户身份和调用标识，不接受模型指定保存目录或 RPC 凭据。
需要配置下载服务并在助手中显式授权，文件保存在下载服务器。
配置与使用见 [种子下载工具](../../../docs/TORRENT_TOOL.md)。

## 文件交付工具

- `builtin/archives.py`：`create_zip` 参数和 ZIP 打包；文本文件支持相对子目录，
  可用 `image_urls` 将 HTTPS 图片直链一起打包，不接受服务器本地路径。
- `http/pictures_contract.py`：`fetch_picture` 参数。
- `http/pictures.py`：图片下载和静态网页 `<img src>` / `data-src` 抓取。
  支持 PNG/JPEG/WebP/GIF，最多 6 张、单张 5 MiB、合计 15 MiB、整次网络调用 30 秒。
  `as_zip: true` 将抓取结果合为一个压缩包。不会执行 JavaScript，也不生成图片。
- `artifacts.py`：将工具产物保存为当前用户的聊天附件，复用现有受保护下载接口。
  二进制不进入模型上下文；提交前复核任务租约和工具权限，成功结果由工具账本复用。

使用前在助手的“工具权限”中勾选“生成压缩包”或“抓取图片”，并发布新版本。
例如：“把这段内容保存为说明.txt 并打包下载”“下载 https://网站/图片.png”
或“抓取这个网页的前三张图片并打包”。不需要绘图 API 或新增密钥。

先按工具访问的资源找目录：

```text
tools/
├── builtin/        本机内置工具
│   ├── contracts.py 参数类型：TextStats、Report、ReadFiles
│   ├── text.py      text_stats / format_report
│   └── files.py     read_files；文本、Word 正文/表格及读取边界
├── http/           声明式 HTTPS 查询
│   ├── contracts.py 工具地址、参数和网络配置契约
│   ├── defaults.py  天气、路况默认声明
│   ├── search.py    网页搜索声明和结果规范化
│   └── runtime.py   DNS、TLS、超时、响应容量及执行
├── mcp/            远程 MCP 协议
│   ├── models.py   用户服务器、版本、加密凭证
│   ├── client.py   SSE / Streamable HTTP 客户端
│   ├── service.py  注册、绑定复核和执行
│   ├── router.py   连接与工具同步接口
│   └── errors.py   公开错误码和提示
├── registry.py     给模型看的工具名称、说明和参数 Schema
├── runner.py       本地内置工具的固定分发入口
├── models.py       共享工具目录、版本和审核记录
├── service.py      目录、版本冻结、启停与角色授权
├── router.py       目录管理接口
└── review_router.py 自定义 HTTP 工具审核接口
```

## 添加一个本地工具

1. 在 `builtin/contracts.py` 定义调用参数。
2. 在 `builtin/` 对应资源模块编写函数。
3. 在 `registry.py` 注册名称、说明和参数 Schema，在 `service.py` 的 `TITLES` 补中文名称。
4. 在 `runner.py` 增加固定分发，并在 Agent / Skill 中显式授权。

HTTP 和 MCP 工具通过各自目录发布与绑定，不在 `runner.py` 中实现网络请求。

## read_files 与工作空间

用户在设置中保存工作空间，数据和目录配置接口位于 `app/workspaces/`。
模型只提交相对路径，例如 `notes/guide.md`，不能选择用户 ID 或授权根目录。
任务执行层读取当前用户的配置，在查成功账本之前复核工作空间、版本和路径。
浏览器导入的目录由 `workspaces/imports.py` 从本人快照清单定位 S3 对象，校验大小及摘要后，
复用 `builtin/files.py` 的文本与 Word 解析。对象键由服务器生成，不接受模型或浏览器指定。
保留的服务器目录模式将根目录传给 `execute_tool(..., workspace=root)`。
没有启用工作空间时默认拒绝；重导入、清空或停用后旧任务绑定和成功缓存也失效。

`builtin/files.py` 支持 `.txt` / `.md` 的 UTF-8 原文和 `.docx` 的正文、表格，
返回文本最多 64 KiB。Word 二进制输入最多 10 MiB，ZIP 总展开最多 32 MiB、
单成员最多 8 MiB、最多 1000 个成员，表格累计展开最多 10000 个单元格；
本地解析不调用 Docling 或执行宏，不识别图片文字。
旧 `.doc` 返回 `file_format_unsupported`，需先转换为 `.docx`，不会当作 UTF-8 文本读取。
执行层负责账号权限、工作空间版本、停用以及恢复时的结果复核。`resolve()` 检查不等于
完整文件沙箱：部署时只开放可信、只读的目录，避免不可信进程并发替换目录链接。
