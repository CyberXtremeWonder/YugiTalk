param([switch]$IncludeContainers, [switch]$IncludeUI)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'set-project-cache.ps1')
# 在全新检出中先创建绑定挂载目录，不覆盖已有数据。
& (Join-Path $PSScriptRoot 'init-project-data.ps1')

function Invoke-Checked {
    <# .SYNOPSIS
    执行验收命令并检查退出码，失败时立即终止后续检查。
    #>
    param([string]$Executable, [string[]]$TaskArguments)
    & $Executable @TaskArguments
    if ($LASTEXITCODE -ne 0) {
        $failedExitCode = $LASTEXITCODE
        $commandLabel = "$Executable $($TaskArguments -join ' ')"
        if ($Executable -eq 'docker') {
            # 保留原始 Compose 错误，并补充状态以区分构建、端口与健康故障。
            Write-Output "Docker 命令失败：$commandLabel"
            & docker compose ps -a
        }
        throw "命令执行失败：$commandLabel（退出码 $failedExitCode），具体原因请查看上方输出。"
    }
}

Push-Location $projectRoot
try {
    # 失败立即停止；测试产物保存在项目内，便于阶段验收和复查。
    $env:PYTHONUTF8 = '1'
    Invoke-Checked 'backend/.venv/Scripts/ruff.exe' @('check', 'backend')
    Invoke-Checked 'backend/.venv/Scripts/ruff.exe' @('format', '--check', 'backend')
    Invoke-Checked 'backend/.venv/Scripts/mypy.exe' @('--config-file', 'backend/pyproject.toml', 'backend/app', 'backend/start.py', 'backend/probes')
    Push-Location 'backend'
    try {
        Invoke-Checked '.venv/Scripts/python.exe' @('-m', 'pytest', '-q', '--basetemp=../.cache/pytest-stage1', '--junitxml=../docs/stages/artifacts/stage1-backend-tests.xml')
        Invoke-Checked '.venv/Scripts/python.exe' @('start.py', '--check')
        Invoke-Checked '.venv/Scripts/python.exe' @('-m', 'alembic', 'upgrade', 'head', '--sql')
    } finally { Pop-Location }
    Invoke-Checked 'docker' @('compose', 'config', '--quiet')
    foreach ($command in @('lint', 'typecheck', 'format:check', 'test', 'build')) {
        Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'run', $command)
    }
    if ($IncludeContainers) {
        Invoke-Checked 'docker' @('compose', 'up', '--build', '-d', '--wait', '--wait-timeout', '180')
        # 故障演练仅操作 yugitalk-dev，恢复动作由 Python 脚本的 finally 保障。
        Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/check-stage1-infra.py', '--fault')
    } else { Write-Output '未复跑容器冷启动和故障演练；可添加 -IncludeContainers。' }
    if ($IncludeUI) {
        # 阶段 2 已引入登录门禁，历史 UI 回归在隔离账号环境中复跑。
        Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/check-stage2-integration.py')
    } else { Write-Output '未复跑浏览器测试；服务启动后可添加 -IncludeUI。' }
} finally {
    Pop-Location
    & (Join-Path $PSScriptRoot 'clean-stage-cache.ps1') -Stage 1
}
