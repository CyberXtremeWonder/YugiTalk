# 已完成阶段的 Docker 配置归档

阶段0–5配置从项目根目录移入这里，避免与当前开发、网关及生产入口混用。相关阶段测试脚本已更新路径，仍可复现；归档不删除业务数据或备份。

在项目根目录使用这些文件时，必须添加 `--project-directory .`，确保挂载和构建上下文仍指向项目根目录。例如：

```powershell
docker compose --project-directory . -f deploy/archive/stages/docker-compose.stage0.yml config --quiet
```

阶段2–5需要测试脚本生成隔离数据库和凭证，不直接手工启动。阶段6仍在验收，配置继续保留在根目录。
