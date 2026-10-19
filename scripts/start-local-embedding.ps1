param([switch]$Cpu, [switch]$LocalOnly, [switch]$Rebuild)
$ErrorActionPreference = 'Stop'
$embeddingProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $embeddingProjectRoot
try {
    # 无 GPU 时不加载设备预留配置，避免 Docker 在应用启动前报错。
    $composeArgs = @('-p', 'yugitalk-dev', '-f', 'docker-compose.yml')
    if (-not $LocalOnly) { $composeArgs += @('-f', 'docker-compose.gateway.yml') }
    $cpuImage = & docker image ls --filter 'reference=yugitalk-embedding:0.1.0' --quiet
    if ($Rebuild -or -not $cpuImage) {
        & docker compose @composeArgs --profile local-embedding build embedding
        if ($LASTEXITCODE -ne 0) { throw 'CPU 嵌入镜像构建失败' }
    }
    $useGpu = $false
    if (-not $Cpu) {
        try {
            & docker run --rm --gpus all --entrypoint nvidia-smi yugitalk-embedding:0.1.0 --query-gpu=name --format=csv,noheader 2>$null
            $useGpu = $LASTEXITCODE -eq 0
        } catch { $useGpu = $false }
    }
    if ($useGpu) {
        $gpuArgs = $composeArgs + @('-f', 'docker-compose.gpu.yml')
        try {
            $gpuImage = & docker image ls --filter 'reference=yugitalk-embedding:0.1.0-cuda' --quiet
            if ($Rebuild -or -not $gpuImage) {
                & docker compose @gpuArgs --profile local-embedding build embedding
            }
            if ($LASTEXITCODE -eq 0) {
                & docker run --rm --gpus all --entrypoint python yugitalk-embedding:0.1.0-cuda -c 'import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)'
                $useGpu = $LASTEXITCODE -eq 0
            } else { $useGpu = $false }
        } catch { $useGpu = $false }
        if ($useGpu) { $composeArgs = $gpuArgs }
    }
    Write-Host $(if ($useGpu) { '嵌入模型优先使用独立显卡，CUDA 不可用时降级 CPU。' } else { '使用 CPU 嵌入模型。' })
    # 切换模型服务前等待现有任务完成，避免中断分批编码。
    $workerRunning = [bool](& docker ps --filter 'name=^/yugitalk-dev-worker-1$' --quiet)
    if ($workerRunning) {
        & docker compose @composeArgs stop -t 1810 worker
        if ($LASTEXITCODE -ne 0) { throw '工作进程未能平稳停止' }
    }
    try {
        & docker compose @composeArgs --profile local-embedding up -d --no-deps --wait --wait-timeout 300 embedding
        if ($LASTEXITCODE -ne 0) { throw '嵌入服务启动失败，请查看 embedding 日志' }
    } finally {
        if ($workerRunning) {
            & docker compose @composeArgs up -d --no-deps worker
            if ($LASTEXITCODE -ne 0) { throw '工作进程重启失败' }
        }
    }
    Invoke-RestMethod http://127.0.0.1:18081/health | ConvertTo-Json
} finally { Pop-Location }
