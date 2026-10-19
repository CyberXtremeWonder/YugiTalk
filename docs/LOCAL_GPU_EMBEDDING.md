# 本机独立显卡运行嵌入模型

在项目 PowerShell 中执行：

```powershell
cd E:\Code\YugiTalk
.\scripts\start-local-embedding.ps1
```

脚本先探测 Docker 的 NVIDIA GPU 支持，验证 CUDA PyTorch 可用后加载 `docker-compose.gpu.yml`。未检测到 GPU、Docker 不支持 GPU 或 CUDA 镜像构建失败时使用 CPU 配置。首次 GPU 构建需要下载 CUDA PyTorch 及运行库，模型继续读取现有本地 BGE-M3 缓存，不下载模型。Windows Docker Desktop 需使用 WSL 2 后端。

日常启动复用已有镜像，不重复下载依赖。修改嵌入服务代码后添加 `-Rebuild` 重建镜像。

默认沿用本项目网关覆盖配置；仅使用基础开发配置时添加 `-LocalOnly`。强制使用 CPU：

```powershell
.\scripts\start-local-embedding.ps1 -Cpu
```

切换前脚本会让 Worker 完成正在执行的任务后停止，更新嵌入服务，再启动 Worker。排队任务继续处理。启动完整应用仍按原开发启动文档；本脚本只选择并更新嵌入服务。

`BGE_DEVICE=auto` 在服务内优先选择 CUDA；无可用 CUDA 时使用 CPU。CUDA 模型初始化失败或推理显存不足时也降级 CPU，日志明确记录。显存不足后的 CPU 模式持续到服务重启，不在每个请求反复尝试 GPU。显式设置 `BGE_DEVICE=cuda:0` 时不做自动降级。

查看实际设备与日志：

```powershell
Invoke-RestMethod http://127.0.0.1:18081/health
docker logs --tail 100 -f yugitalk-dev-embedding-1
nvidia-smi
```

健康信息 `device=cuda:0` 表示 GPU；`device=cpu` 表示 CPU，`gpu_name` 显示实际显卡名称。模型、1024 维输出、归一化方式与索引版本保持一致，已有文件无需因设备切换而重新索引。普通 Compose 启动不附带 GPU 覆盖文件时使用 CPU 镜像；要自动选择显卡，请使用上述脚本。

参考：[Docker Desktop GPU 支持](https://docs.docker.com/desktop/features/gpu/)、[PyTorch 2.8 CUDA 安装](https://pytorch.org/get-started/previous-versions/)。

CUDA 镜像默认使用上海交大的 PyTorch wheel 镜像，固定 PyTorch 2.8.0+cu128 与 SHA256；其余 CUDA 依赖和 uv 使用清华 PyPI 镜像。基础 CPU 镜像已有的依赖继续复用，不改全局 pip 配置。镜像源通过 Docker 构建参数 `PIP_INDEX_URL` 和 `CUDA_TORCH_WHEEL_URL` 调整。参考：[上海交大 PyTorch 镜像](https://mirrors.sjtug.sjtu.edu.cn/docs/pytorch-wheels)、[清华 PyPI 镜像](https://mirror.tuna.tsinghua.edu.cn/help/pypi/)。

2026-10-13 本机验证：RTX 4060 Ti 16GB、PyTorch 2.8.0+cu128、CUDA 12.8，模型实际设备为 cuda:0。16 个测试分块约 1.02 秒完成，返回 16 个归一化的 1024 维向量（单次测量，不作为通用速度保证）。未分配 GPU 的 CUDA 容器选择 CPU；隔离测试覆盖 CUDA 初始化失败与显存不足的 CPU 降级。自动启动脚本完成验证，应用所有依赖恢复健康。此前 732 分块的超时文件已完成索引。
