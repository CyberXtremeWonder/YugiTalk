param([switch]$IncludeProduction, [switch]$IncludeRestore, [switch]$IncludeUI, [switch]$IncludeSoak, [switch]$IncludeSecurity)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'set-project-cache.ps1')
function Invoke-Checked {
    <# .SYNOPSIS
    执行有界阶段检查，发生失败时停止后续操作，凭证不进入输出。
    #>
    param([string]$Executable, [string[]]$TaskArguments)
    & $Executable @TaskArguments
    if ($LASTEXITCODE -ne 0) { throw "阶段6检查失败：$Executable（退出码 $LASTEXITCODE）" }
}
Push-Location $projectRoot
try {
    $env:PYTHONUTF8 = '1'
    if (($IncludeRestore -or $IncludeUI -or $IncludeSoak -or $IncludeSecurity) -and -not $IncludeProduction) {
        throw '恢复、浏览器、完整压测或镜像安全验收需要同时添加 -IncludeProduction。'
    }
    Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/check-code-comments.py')
    Invoke-Checked 'backend/.venv/Scripts/ruff.exe' @('check', 'backend', 'scripts/init-production.py', 'scripts/production-data.py', 'scripts/deploy-production.py', 'scripts/check-stage6-production.py', 'scripts/check-stage6-restore.py', 'scripts/check-stage6-latency.py', 'scripts/stage6-model-fixture.py', 'scripts/check-production-s3.py', 'scripts/check-stage6-soak.py', 'scripts/audit-stage6-images.py', 'scripts/build-production-images.py')
    Invoke-Checked 'backend/.venv/Scripts/ruff.exe' @('format', '--check', 'backend', 'scripts/init-production.py', 'scripts/production-data.py', 'scripts/deploy-production.py', 'scripts/check-stage6-production.py', 'scripts/check-stage6-restore.py', 'scripts/check-stage6-latency.py', 'scripts/stage6-model-fixture.py', 'scripts/check-production-s3.py', 'scripts/check-stage6-soak.py', 'scripts/audit-stage6-images.py', 'scripts/build-production-images.py')
    Invoke-Checked 'backend/.venv/Scripts/mypy.exe' @('--config-file', 'backend/pyproject.toml', 'backend/app', 'backend/start.py', 'backend/probes')
    Invoke-Checked 'backend/.venv/Scripts/python.exe' @('-m', 'pytest', 'backend/tests', '-q', '--basetemp=.cache/pytest-stage6-script', '-o', 'cache_dir=.cache/pytest-cache-stage6-host', '--junitxml=docs/stages/artifacts/stage6-backend-tests.xml')
    Invoke-Checked 'docker' @('compose', 'config', '--quiet')
    foreach ($command in @('check:comments', 'lint', 'typecheck', 'format:check', 'build')) {
        Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'run', $command)
    }
    Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'test', '--', '--reporter=default', '--reporter=junit', '--outputFile=../docs/stages/artifacts/stage6-frontend-tests.xml')
    if ($IncludeProduction) {
        Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/build-production-images.py')
        Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/check-stage6-production.py', '--keep')
        if ($IncludeRestore) {
            Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/check-stage6-restore.py')
        }
        Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/check-stage6-latency.py')
        if ($IncludeSecurity) {
            Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/audit-stage6-images.py')
        }
        if ($IncludeSoak) {
            Invoke-Checked 'backend/.venv/Scripts/python.exe' @('scripts/check-stage6-soak.py', '--seconds', '1800')
        }
        if ($IncludeUI) {
            # 密码仅在子进程环境传递，不输出测试私有环境文件。
            $env:E2E_PASSWORD = & backend/.venv/Scripts/python.exe -c 'from dotenv import dotenv_values; print(dotenv_values(".cache/stage6-production.env")["E2E_PASSWORD"])'
            try { Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'run', 'test:stage6') }
            finally { Remove-Item Env:E2E_PASSWORD -ErrorAction SilentlyContinue }
        }
    }
    Write-Output '已选检查通过；阶段6仍需核对全部门槛和用户验收，未选检查不能记为通过。真实模型评测单独授权执行。'
} finally {
    Pop-Location
    & (Join-Path $PSScriptRoot 'clean-stage-cache.ps1') -Stage 6
}
