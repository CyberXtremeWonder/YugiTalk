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
    if ($LASTEXITCODE -ne 0) { throw "阶段 5 验收失败：$Executable $($TaskArguments -join ' ')（退出码 $LASTEXITCODE）" }
}
Push-Location $projectRoot
try {
    $env:PYTHONUTF8 = '1'
    Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/check-code-comments.py')
    Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'run', 'check:comments')
    Invoke-Checked 'backend/.venv/Scripts/ruff.exe' @('check', 'backend', 'embedding', 'scripts/check-stage2-integration.py', 'scripts/check-stage4-rag.py', 'scripts/check-stage5-recovery.py')
    Invoke-Checked 'backend/.venv/Scripts/ruff.exe' @('format', '--check', 'backend', 'embedding', 'scripts/check-stage2-integration.py', 'scripts/check-stage4-rag.py', 'scripts/check-stage5-recovery.py')
    Invoke-Checked 'backend/.venv/Scripts/mypy.exe' @('--config-file', 'backend/pyproject.toml', 'backend/app', 'backend/start.py', 'backend/probes')
    Push-Location 'backend'
    try {
        Invoke-Checked '.venv/Scripts/python.exe' @('-m', 'pytest', '-q', '--basetemp=../.cache/pytest-stage5', '--junitxml=../docs/stages/artifacts/stage5-backend-tests.xml')
        Invoke-Checked '.venv/Scripts/python.exe' @('start.py', '--check')
        Invoke-Checked '.venv/Scripts/python.exe' @('-m', 'alembic', 'upgrade', 'head', '--sql')
    } finally { Pop-Location }
    Invoke-Checked 'docker' @('compose', 'config', '--quiet')
    foreach ($command in @('lint', 'typecheck', 'format:check', 'build')) {
        Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'run', $command)
    }
    Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'run', 'test', '--', '--reporter=default', '--reporter=junit', '--outputFile=../docs/stages/artifacts/stage5-frontend-tests.xml')
    if ($IncludeContainers) {
        Invoke-Checked 'docker' @('compose', 'up', '--build', '-d', '--wait', '--wait-timeout', '180')
        Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/check-stage1-infra.py')
    }
    if ($IncludeUI) {
        # 独立临时数据库与应用容器；测试结束自动清理，不添加演示账号到开发数据库。
        Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/check-stage2-integration.py', '--stage5')
    }
    Write-Output '阶段 5 已选测试全部通过；容器与浏览器完整验收请同时添加 -IncludeContainers -IncludeUI。'
} finally {
    Pop-Location
    & (Join-Path $PSScriptRoot 'clean-stage-cache.ps1') -Stage 5
}


