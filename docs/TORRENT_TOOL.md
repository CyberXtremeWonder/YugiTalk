# 种子下载工具

`download_torrent` 将磁力链接或 HTTPS `.torrent` 直链提交给 aria2 下载内容，
随后用同一个工具查询进度。文件保存在 **aria2 所在机器** 的磁盘，
不写入聊天附件，不自动导入知识库。无需添加 Python 依赖或数据库迁移。

## 配置与启用

先准备启用了 JSON-RPC 的 aria2 服务。若后端和 aria2 都直接运行在 Windows 本机，
可以在安装 aria2 后创建 `aria2.conf`，例如：

```ini
enable-rpc=true
rpc-listen-all=false
rpc-listen-port=6800
rpc-max-request-size=8M
rpc-secret=替换为随机长密钥
dir=E:/Downloads/YugiTalk
seed-time=0
max-concurrent-downloads=3
max-download-result=10000
```

启动：`aria2c --conf-path=aria2.conf`。如需重启后继续未完成下载，
按 aria2 文档配置 `input-file`、`save-session` 和 `save-session-interval`，
并预先创建会话文件。

在项目根目录 `.env` 配置：

```dotenv
ARIA2_RPC_URL=http://127.0.0.1:6800/jsonrpc
ARIA2_RPC_SECRET=与aria2.conf中的rpc-secret相同
TORRENT_DOWNLOAD_ROOT=E:/Downloads/YugiTalk
```

重启 API 和 worker。在“工具”页面确认“种子下载”已就绪，
在助手的工具权限中勾选“种子下载”并发布新版本。

Compose 已向 API/worker 传递这三个变量。容器中的 `127.0.0.1` 指向容器自身，
应改用 worker 可达的 aria2 地址；例如另一容器的 `http://aria2:6800/jsonrpc`，
根目录则填写该 aria2 容器内的绝对目录（如 `/downloads`）。
上面的 Windows 本机配置只监听回环地址，不能直接用于容器访问。
API/worker 不必挂载下载目录。

RPC 地址和密钥属于部署配置，不接收模型传参；不要将 RPC 端口暴露到公网。
下载进程会连接种子声明的 tracker、peer 及 web seed，公网 HTTPS 检查只覆盖
获取 `.torrent` 文件及其重定向，**不覆盖 aria2 的 P2P 流量**。
部署时为下载进程设置独立可写目录、磁盘配额和网络边界，避免访问内部服务。

## 调用示例

提交磁力链接（支持 BT v1 info hash）：

```json
{"action":"start","source":"magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"}
```

提交种子文件直链：

```json
{"action":"start","source":"https://example.org/file.torrent"}
```

`.torrent` 文件通过现有公网 HTTPS 下载器取得，限制为 5 MiB、30 秒、最多四跳，
由 aria2 解析；HTML 下载页面不是文件直链。暂不接受本地路径或上传的二进制种子。

返回 `download_id`、`status: submitted` 和 `save_directory`。
ID 带有用户签名，须原样保存；其他用户不能使用该 ID 查询。

查询：

```json
{"action":"status","download_id":"提交时返回的完整ID"}
```

返回状态、总字节数、已完成字节数、百分比及每秒下载字节数。
总大小未知时百分比为 `null`，磁力链接解析元数据后会自动跟踪内容下载任务。
`submitted` 只表示已提交，`complete` 才表示完成；`error` 时 `error_code`
为 aria2 下载错误码。查询失败时不要将它解释成下载完成。

## 执行与恢复边界

- 调用前、账本复用前及结果提交前均经过项目现有的权限和任务租约检查。
- 网络请求不占用数据库事务；每次工具调用只提交或查询一次，不循环等待下载结束。
- 已成功落账的相同调用复用结果；新的 status 调用读取新进度。
- 相同任务步骤和调用 ID 使用相同 aria2 GID。提交超时可能已经开始下载，
  错误结果仍返回签名 ID，应先查询，避免另建任务重复下载。
- 这不保证跨 aria2 重启、记录清理的严格恰好一次。aria2 清理根 GID 后，
  旧 ID 可能无法查询；服务密钥更换后旧签名 ID 失效。
- 取消聊天只停止当前聊天执行，**不会停止已提交的后台下载**。
  如需停止下载，通过 aria2 管理端处理。下载中仍参与 BT 上传，完成后不继续做种。

实现依据：[aria2 官方 RPC 文档](https://aria2.github.io/manual/en/html/aria2c.html#rpc-interface)。

## 真实下载验收

`scripts/check-torrent-runtime.py` 启动真实 aria2 做种端和下载端、自建 tracker、
带证书校验的本地 HTTPS 种子服务。分别通过 `.torrent` 和磁力链接下载 512 KiB
随机文件，验证完成状态、文件 SHA-256、重复 GID 拒绝及跨用户查询拒绝。
同时用未经替换的网络检查确认私网种子 URL 被拒绝。
测试仅为 fixture 域名替换 DNS 到回环地址，不放宽产品网络策略。

在项目根目录运行以下 PowerShell 命令（需 Docker 和项目后端镜像）：

```powershell
New-Item -ItemType Directory -Force .cache/torrent-live | Out-Null
docker build -f scripts/torrent-smoke.Dockerfile -t yugitalk-torrent-smoke:local scripts
docker run --rm --network none --name yugitalk-torrent-smoke `
  --mount "type=bind,source=$($PWD.Path)/backend,target=/work,readonly" `
  --mount "type=bind,source=$($PWD.Path)/scripts/check-torrent-runtime.py,target=/probe.py,readonly" `
  --mount "type=bind,source=$($PWD.Path)/.cache/torrent-live,target=/evidence" `
  --workdir /work -e PYTHONPATH=/work yugitalk-torrent-smoke:local `
  python /probe.py /evidence/result.json
```

测试进程、下载文件及临时证书随容器退出清理，不连接生产数据库，
不修改 `.env`。报告保存在 `.cache/torrent-live/result.json`；
本次实测结果归档于 `docs/evaluation/TORRENT_SMOKE.json`。
该测试证明本地可控 BT 下载链路可用，不代表公网 tracker、NAT 穿透或生产部署已验证。
