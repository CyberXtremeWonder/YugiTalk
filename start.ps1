# Windows PowerShell 一键启动开发环境，不依赖 WSL 或 Bash。
$ErrorActionPreference = 'Stop'

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw '请先安装并启动 Docker Desktop，使用 Linux 容器模式。'
}

Push-Location -LiteralPath $PSScriptRoot
try {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Docker 尚未就绪，请先启动 Docker Desktop。' }
    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { throw '未找到 Docker Compose 插件，请升级 Docker Desktop。' }

    if (-not (Test-Path -LiteralPath '.env' -PathType Leaf)) {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
        throw '已从模板创建 .env，请先配置模型路径、服务地址和密钥，再重新运行。'
    }

    & (Join-Path $PSScriptRoot 'scripts/init-project-data.ps1')
    Write-Output '检查开发配置……'
    docker compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw '开发配置检查失败，请检查 .env。' }

    Write-Output '第一步：构建镜像，下面会显示依赖下载进度；首次构建可能需要几分钟……'
    docker compose --progress plain build
    if ($LASTEXITCODE -ne 0) { throw '镜像构建失败，请查看上方具体下载或安装错误。' }

    Write-Output '第二步：启动服务并等待健康检查……'
    docker compose up -d --wait --wait-timeout 600
    if ($LASTEXITCODE -ne 0) {
        throw '启动失败，请执行 docker compose logs --tail 100 migrate api worker scheduler frontend 查看原因。'
    }

    Write-Output ''
    Write-Output '启动完成。'
    Write-Output '前端：http://127.0.0.1:8080/'
    Write-Output 'API 文档（默认端口）：http://127.0.0.1:18000/docs'
    Write-Output '查看状态：docker compose ps'
    Write-Output '停止服务：docker compose stop'
}
finally {
    Pop-Location
}
