#!/usr/bin/env bash
# 一键启动 Docker 开发环境；保留已有配置和数据，不启动本机热更新进程。
set -Eeuo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

trap 'printf "启动失败。请在项目目录执行 docker compose logs --tail 100 migrate api worker scheduler frontend 查看原因。\n" >&2' ERR

if ! command -v docker >/dev/null 2>&1; then
  printf '请先安装 Docker 和 Docker Compose 插件。\n' >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  printf 'Docker 尚未就绪，请先启动 Docker（Windows 使用 Docker Desktop 的 Linux 容器模式）。\n' >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  printf '未找到 Docker Compose 插件，请先安装或升级 Docker。\n' >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  printf '已从模板创建 .env，请先配置模型路径、服务地址和密钥，再重新运行本脚本。\n'
  exit 1
fi

# 与现有目录初始化脚本保持一致，拒绝符号链接，避免写入项目外部。
for directory in data data/postgres data/redis data/minio data/etcd data/milvus \
  data/stage0 data/stage0/postgres data/stage0/redis .cache .cache/models data/backups; do
  if [[ -L "$directory" ]]; then
    printf '持久目录不能是符号链接：%s\n' "$directory" >&2
    exit 1
  fi
  mkdir -p -- "$directory"
done

printf '检查开发配置……\n'
docker compose config --quiet
printf '第一步：构建镜像，下面会显示依赖下载进度；首次构建可能需要几分钟……\n'
docker compose --progress plain build
printf '第二步：启动服务并等待健康检查……\n'
docker compose up -d --wait --wait-timeout 600

printf '\n启动完成。\n'
printf '前端：http://127.0.0.1:8080/\n'
printf 'API 文档（默认端口）：http://127.0.0.1:18000/docs\n'
printf '查看状态：docker compose ps\n'
printf '停止服务：docker compose stop\n'
